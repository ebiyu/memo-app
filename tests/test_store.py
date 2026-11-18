"""ローカル保存: slug とファイルパスのキー、文字コード・改行の保持。"""

import pytest

from memo.desktop.store import LocalStore, is_file_key


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "data")


def test_slug_is_stored_in_default_folder(store):
    store.write("m", "x\n")
    assert store.path("m") == store.notes_dir / "m.txt"
    assert store.read("m") == "x\n"
    assert store.slugs() == ["m"]


def test_file_key_is_absolute_path(store, tmp_path):
    path = tmp_path / "a.md"
    key = store.key_for(path)
    assert is_file_key(key) and not is_file_key("m")
    store.write(key, "hello\n")
    assert path.read_bytes() == b"hello\n"
    assert store.title(key) == "a.md"
    assert store.slugs() == []  # 既定のフォルダのメモ一覧には入らない


def test_opening_file_in_default_folder_maps_to_slug(store):
    store.write("m", "")
    assert store.key_for(store.notes_dir / "m.txt") == "m"
    assert is_file_key(store.key_for(store.notes_dir / "Not A Slug.txt"))


def test_keeps_crlf_and_bom(store, tmp_path):
    path = tmp_path / "w.txt"
    path.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    key = store.key_for(path)
    assert store.read(key) == "a\nb\n"
    store.write(key, "a\nb\nc\n")
    assert path.read_bytes() == b"\xef\xbb\xbfa\r\nb\r\nc\r\n"


def test_new_file_is_lf_without_bom(store, tmp_path):
    key = store.key_for(tmp_path / "new.txt")
    store.write(key, "a\nb\n")
    assert (tmp_path / "new.txt").read_bytes() == b"a\nb\n"


def test_non_utf8_raises(store, tmp_path):
    path = tmp_path / "sjis.txt"
    path.write_bytes("日本語".encode("cp932"))
    with pytest.raises(UnicodeDecodeError):
        store.read(store.key_for(path))


def test_missing_file(store, tmp_path):
    key = store.key_for(tmp_path / "none.txt")
    assert store.read(key) is None
    assert store.stat(key) is None
    assert not store.exists(key)
