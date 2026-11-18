"""ローカル保存。

メモは「キー」で指す。キーは次のどちらか:
- slug: 既定のフォルダ notes/<slug>.txt に保存する
- 絶対パス: 「ファイルを開く」で開いたファイルそのものに保存する

ファイルは Claude Code など外部のツールも書き換えるので、読み込んだときの
BOM と改行コード (CRLF / LF) を覚えておき、書き戻すときに揃える。
エディタ側は常に LF で扱う。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading

from memo.core import validate_slug

BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class FileFormat:
    bom: bool = False
    newline: str = "\n"


def is_file_key(key: str) -> bool:
    """slug は英小文字・数字・ハイフンだけなので、絶対パスとは衝突しない。"""
    return os.path.isabs(key)


def file_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def decode(data: bytes) -> tuple[str, FileFormat]:
    """UTF-8 (BOM 有無どちらも) として読む。UTF-8 でなければ UnicodeDecodeError。"""
    bom = data.startswith(BOM)
    text = data[len(BOM):].decode("utf-8") if bom else data.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), FileFormat(bom, newline)


def encode(text: str, fmt: FileFormat) -> bytes:
    if fmt.newline != "\n":
        text = text.replace("\n", fmt.newline)
    return (BOM if fmt.bom else b"") + text.encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    """書きかけの内容を他のツールに読ませないよう、一時ファイルから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.memo-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, path)
    except PermissionError:
        # 他のアプリが削除共有なしで開いていると置き換えられない。その場合は直接書く
        tmp.unlink(missing_ok=True)
        with open(path, "wb") as f:
            f.write(data)


class LocalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.notes_dir = self.root / "notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._formats: dict[Path, FileFormat] = {}

    def path(self, key: str) -> Path:
        if is_file_key(key):
            return Path(key)
        return self.notes_dir / f"{validate_slug(key)}.txt"

    def key_for(self, path: str | Path) -> str:
        """パスに対応するキー。既定のフォルダ内の slug 名の .txt なら slug にする。"""
        p = Path(path).resolve()
        if p.parent == self.notes_dir.resolve() and p.suffix == ".txt":
            try:
                return validate_slug(p.stem)
            except Exception:
                pass
        return str(p)

    @staticmethod
    def title(key: str) -> str:
        return Path(key).name if is_file_key(key) else key

    # --- 本文 ---
    def read(self, key: str) -> str | None:
        """無ければ None。UTF-8 でなければ UnicodeDecodeError。"""
        p = self.path(key)
        with self.lock:
            try:
                data = p.read_bytes()
            except FileNotFoundError:
                return None
            text, fmt = decode(data)
            self._formats[p] = fmt
            return text

    def write(self, key: str, text: str) -> None:
        p = self.path(key)
        with self.lock:
            _atomic_write(p, encode(text, self._formats.get(p, FileFormat())))

    def delete(self, key: str) -> None:
        with self.lock:
            self.path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def stat(self, key: str) -> tuple[int, int] | None:
        """外部からの変更検知用 (mtime_ns, size)。無ければ None。"""
        try:
            st = self.path(key).stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def slugs(self) -> list[str]:
        return sorted(p.stem for p in self.notes_dir.glob("*.txt"))
