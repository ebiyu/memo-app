"""サーバーとの同期。

方針:
- サーバーは version による楽観ロックのみ (不一致なら 409)。
- 409 を受けたらクライアントが 3-way merge (base=最後に一致した内容)。
- 自動マージできなければ、コンフリクトマーカー入りの本文をローカルに書き出し、
  ユーザーがマーカーを消すまで push しない。ただし Ctrl+S (force) された場合は
  マーカー入りのまま push する。
- 通信中はロックを持たない。通信後、ローカルが変わっていたら書き込まず次回に回す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
from typing import Callable

import httpx

from memo.core import has_conflict_markers, merge3way

from .store import LocalStore, SyncState

log = logging.getLogger(__name__)

# (slug, 新しいローカル本文 or None=削除) -- 同期スレッドから呼ばれる
OnLocalChange = Callable[[str, str | None], None]


class SyncError(Exception):
    pass


@dataclass
class SyncReport:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


class SyncEngine:
    def __init__(
        self,
        store: LocalStore,
        client: httpx.Client,
        on_local_change: OnLocalChange | None = None,
    ):
        self.store = store
        self.client = client
        self.on_local_change = on_local_change or (lambda slug, text: None)
        # held: 手元の変更を自動 push しない slug (編集中の付箋)
        # forced: held でも次の同期で push する slug (Ctrl+S)
        self._held: set[str] = set()
        self._forced: set[str] = set()
        self._hold_lock = threading.Lock()

    # --- 保留 / 強制 push (UI スレッドから呼ばれる) ---
    def hold(self, slug: str) -> None:
        with self._hold_lock:
            self._held.add(slug)

    def release(self, slug: str) -> None:
        with self._hold_lock:
            self._held.discard(slug)

    def force(self, slug: str) -> None:
        with self._hold_lock:
            self._forced.add(slug)

    def _is_forced(self, slug: str) -> bool:
        with self._hold_lock:
            return slug in self._forced

    def _unforce(self, slug: str) -> None:
        with self._hold_lock:
            self._forced.discard(slug)

    def _is_held(self, slug: str) -> bool:
        with self._hold_lock:
            return slug in self._held and slug not in self._forced

    # --- HTTP ---
    def _req(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = self.client.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise SyncError(f"{method} {url}: {e}") from e
        if r.status_code >= 400 and r.status_code not in (404, 409):
            raise SyncError(f"{method} {url}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def _remote_index(self) -> dict[str, dict]:
        r = self._req("GET", "/api/notes", params={"include_deleted": "true"})
        return {m["slug"]: m for m in r.json()}

    # --- ローカル書き込み (変わっていなければ) ---
    def _write_local_if_unchanged(self, slug: str, expected: str | None, new: str | None) -> bool:
        with self.store.lock:
            if self.store.read(slug) != expected:
                return False
            if new is None:
                self.store.delete(slug)
            else:
                self.store.write(slug, new)
        if new != expected:
            self.on_local_change(slug, new)
        return True

    # --- メイン ---
    def sync_all(self) -> SyncReport:
        report = SyncReport()
        with self._hold_lock:
            forced = set(self._forced)
        remote = self._remote_index()
        for slug in sorted(self.store.all_known_slugs() | remote.keys()):
            try:
                self.sync_one(slug, remote.get(slug), report)
            except SyncError:
                raise
            except Exception:
                log.exception("sync failed for %s", slug)
        # 通信に成功した回でだけ forced を消費する (失敗時は次回に持ち越し)
        with self._hold_lock:
            self._forced -= forced
        return report

    def sync_one(self, slug: str, rmeta: dict | None, report: SyncReport) -> None:
        with self.store.lock:
            local = self.store.read(slug)
            st = self.store.get_state(slug)

        rv = rmeta["version"] if rmeta else 0
        rdeleted = bool(rmeta and rmeta["deleted"])

        # サーバーに無い (= 新規 or サーバーがリセットされた): base_version 0 で push
        bv = st.version if (st and rmeta) else 0
        base = st.base if st else None

        if local is not None:
            changed = st is None or rmeta is None or local != base
            if changed:
                if st and st.conflict and has_conflict_markers(local) and not self._is_forced(slug):
                    return  # ユーザーが解消するか、Ctrl+S でマーカーごと保存するまで待つ
                if self._is_held(slug):
                    return  # 編集中: Ctrl+S か非アクティブ 5 分まで待つ (pull もしない)
                self._push(slug, local, bv, base, report)
                return
        elif st is not None:
            # ローカルで削除された
            if rmeta is None or rdeleted:
                self.store.set_state(slug, None)
                return
            r = self._req("DELETE", f"/api/notes/{slug}", params={"base_version": bv})
            if r.status_code == 200:
                self.store.set_state(slug, None)
                report.deleted.append(slug)
                return
            # 削除中にリモートが更新されていた -> 編集を優先して復活させる
            self._pull(slug, None, report)
            return

        # ローカルは未変更。リモートが進んでいれば取り込む。
        if rmeta and (st is None or rv > st.version):
            if rdeleted:
                if local is not None and self._write_local_if_unchanged(slug, local, None):
                    report.deleted.append(slug)
                self.store.set_state(slug, None)
            else:
                self._pull(slug, local, report)

    def _pull(self, slug: str, expected_local: str | None, report: SyncReport) -> None:
        r = self._req("GET", f"/api/notes/{slug}")
        if r.status_code == 404:
            return
        note = r.json()
        if note["deleted"]:
            return
        if self._write_local_if_unchanged(slug, expected_local, note["content"]):
            self.store.set_state(slug, SyncState(note["version"], note["content"]))
            report.pulled.append(slug)

    def _push(
        self, slug: str, local: str, bv: int, base: str | None, report: SyncReport
    ) -> None:
        r = self._req("PUT", f"/api/notes/{slug}", json={"content": local, "base_version": bv})
        if r.status_code == 200:
            note = r.json()
            self.store.set_state(slug, SyncState(note["version"], local))
            report.pushed.append(slug)
            return

        # 409: 3-way merge
        current = r.json()["current"]
        remote_text = "" if current["deleted"] else current["content"]
        result = merge3way(base or "", local, remote_text)

        if not result.conflict:
            r2 = self._req(
                "PUT",
                f"/api/notes/{slug}",
                json={"content": result.text, "base_version": current["version"]},
            )
            if r2.status_code != 200:
                return  # また競合。次回の同期で再試行
            note = r2.json()
            self.store.set_state(slug, SyncState(note["version"], result.text))
            self._write_local_if_unchanged(slug, local, result.text)
            report.merged.append(slug)
            return

        # 自動マージ不可: マーカー入りで書き出し、base はリモートに進める
        if self._write_local_if_unchanged(slug, local, result.text):
            # この回の Ctrl+S はマーカーを見る前のもの。マーカー入り push は次の Ctrl+S から
            self._unforce(slug)
            self.store.set_state(
                slug, SyncState(current["version"], remote_text, conflict=True)
            )
            report.conflicts.append(slug)


class BackgroundSync:
    """定期 + 要求時に同期するスレッド。"""

    def __init__(
        self,
        engine: SyncEngine,
        interval: float = 30.0,
        on_status: Callable[[str, SyncReport | None], None] | None = None,
    ):
        self.engine = engine
        self.interval = interval
        self.on_status = on_status or (lambda status, report: None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="memo-sync", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                report = self.engine.sync_all()
                self.on_status("conflict" if report.conflicts else "ok", report)
            except SyncError as e:
                log.warning("sync error: %s", e)
                self.on_status("offline", None)
            self._wake.wait(self.interval)
