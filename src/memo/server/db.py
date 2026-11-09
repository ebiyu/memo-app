"""SQLite ストア。version による楽観ロックを提供する。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    slug       TEXT PRIMARY KEY,
    content    TEXT    NOT NULL,
    version    INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Note:
    slug: str
    content: str
    version: int
    updated_at: str
    deleted: bool

    def to_dict(self) -> dict:
        return asdict(self)

    def meta(self) -> dict:
        d = self.to_dict()
        del d["content"]
        return d


class VersionConflict(Exception):
    def __init__(self, current: Note | None):
        super().__init__("version conflict")
        self.current = current


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> Note:
    return Note(r["slug"], r["content"], r["version"], r["updated_at"], bool(r["deleted"]))


class NoteStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def get(self, slug: str) -> Note | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM notes WHERE slug = ?", (slug,)).fetchone()
        return _row(r) if r else None

    def list(self, include_deleted: bool = False) -> list[Note]:
        q = "SELECT * FROM notes"
        if not include_deleted:
            q += " WHERE deleted = 0"
        q += " ORDER BY updated_at DESC"
        with self._lock:
            return [_row(r) for r in self._conn.execute(q)]

    def search(self, query: str, limit: int = 20) -> list[Note]:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notes WHERE deleted = 0 AND "
                "(content LIKE ? ESCAPE '\\' OR slug LIKE ? ESCAPE '\\') "
                "ORDER BY updated_at DESC LIMIT ?",
                (pattern, pattern, limit),
            )
            return [_row(r) for r in rows]

    def put(self, slug: str, content: str, base_version: int, *, deleted: bool = False) -> Note:
        """base_version が現在の version と一致した場合のみ書き込む (新規は 0)。"""
        with self._lock, self._conn:
            r = self._conn.execute("SELECT * FROM notes WHERE slug = ?", (slug,)).fetchone()
            current = _row(r) if r else None
            current_version = current.version if current else 0
            if base_version != current_version:
                raise VersionConflict(current)
            note = Note(slug, content, current_version + 1, _now(), deleted)
            self._conn.execute(
                "INSERT INTO notes (slug, content, version, updated_at, deleted) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET content=excluded.content, "
                "version=excluded.version, updated_at=excluded.updated_at, "
                "deleted=excluded.deleted",
                (note.slug, note.content, note.version, note.updated_at, int(note.deleted)),
            )
            return note
