"""ローカル保存: notes/<slug>.txt と、同期状態 .sync/<slug>.json。

.sync には「最後にサーバーと一致した内容 (base)」と version を持つ。
3-way merge の共通祖先として使う。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import threading

from memo.core import validate_slug


@dataclass
class SyncState:
    version: int
    base: str
    conflict: bool = False


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


class LocalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.notes_dir = self.root / "notes"
        self.sync_dir = self.root / ".sync"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    # --- 本文 ---
    def note_path(self, slug: str) -> Path:
        return self.notes_dir / f"{validate_slug(slug)}.txt"

    def read(self, slug: str) -> str | None:
        p = self.note_path(slug)
        with self.lock:
            if not p.exists():
                return None
            with open(p, encoding="utf-8", newline="") as f:
                return f.read()

    def write(self, slug: str, text: str) -> None:
        with self.lock:
            _atomic_write(self.note_path(slug), text)

    def delete(self, slug: str) -> None:
        with self.lock:
            self.note_path(slug).unlink(missing_ok=True)

    def slugs(self) -> list[str]:
        """ローカルに本文がある slug (同期状態だけ残っているものは含まない)。"""
        return sorted(p.stem for p in self.notes_dir.glob("*.txt"))

    def all_known_slugs(self) -> set[str]:
        return set(self.slugs()) | {p.stem for p in self.sync_dir.glob("*.json")}

    # --- 同期状態 ---
    def _state_path(self, slug: str) -> Path:
        return self.sync_dir / f"{validate_slug(slug)}.json"

    def get_state(self, slug: str) -> SyncState | None:
        p = self._state_path(slug)
        with self.lock:
            if not p.exists():
                return None
            return SyncState(**json.loads(p.read_text(encoding="utf-8")))

    def set_state(self, slug: str, state: SyncState | None) -> None:
        with self.lock:
            p = self._state_path(slug)
            if state is None:
                p.unlink(missing_ok=True)
            else:
                _atomic_write(p, json.dumps(asdict(state), ensure_ascii=False))
