"""NoteFrame の保存・外部変更の取り込み (表示はしない)。"""

import os

import pytest

wx = pytest.importorskip("wx")

from memo.desktop.app import CONFLICT_BG, TRANSPARENT_KEY, NoteFrame  # noqa: E402
from memo.desktop.store import LocalStore  # noqa: E402


class FakeApp:
    def __init__(self, store):
        self.store = store
        self.closed = []
        self.deleted = []

    def next_cascade_pos(self, win):
        return wx.Point(0, 0)

    def remember_rect(self, frame):
        pass

    def remember_on_top(self, key, on_top):
        self.on_top = (key, on_top)

    def remember_translucent(self, key, translucent):
        self.translucent = (key, translucent)

    def close_note(self, key):
        self.closed.append(key)

    def delete_note(self, key):
        self.deleted.append(key)

    def menu_entries(self):
        return [(s, s) for s in self.store.slugs()]


@pytest.fixture(scope="module")
def wxapp():
    app = wx.App(False)
    yield app
    app.Destroy()


@pytest.fixture
def frame(wxapp, tmp_path):
    store = LocalStore(tmp_path)
    store.write("m", "1\n2\n3\n")
    f = NoteFrame(FakeApp(store), "m", None)
    yield f
    f.Destroy()


def external_write(path, data: bytes):
    """外部ツールによる書き込み。mtime を確実に変えるため少し進める。"""
    st = path.stat() if path.exists() else None
    path.write_bytes(data)
    if st:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_typing_is_saved(frame):
    frame.text.SetValue("1\n2\n3\nnew\n")
    frame.flush()
    assert frame.app.store.read("m") == "1\n2\n3\nnew\n"


def test_unchanged_editor_does_not_touch_file(frame):
    path = frame.app.store.path("m")
    before = path.stat().st_mtime_ns
    frame.flush()
    assert path.stat().st_mtime_ns == before


def test_external_change_replaces_unedited_text(frame):
    frame.app.store.write("m", "remote\n")
    frame.reconcile()
    assert frame.text.GetValue() == "remote\n"


def test_external_change_merges_with_unsaved_typing(frame):
    frame.text.ChangeValue("1 local\n2\n3\n")  # 未保存の入力
    frame.app.store.write("m", "1\n2\n3 remote\n")  # Claude Code などが書いた
    frame.reconcile()
    assert frame.text.GetValue() == "1 local\n2\n3 remote\n"
    assert frame.app.store.read("m") == "1 local\n2\n3 remote\n"


def test_conflict_turns_title_warning(frame):
    frame.text.ChangeValue("X\n2\n3\n")
    frame.app.store.write("m", "Y\n2\n3\n")
    frame.reconcile()
    assert "<<<<<<< local" in frame.text.GetValue()
    assert frame.app.store.read("m") == frame.text.GetValue()  # マーカーごと書き出す
    assert frame.GetTitle().startswith("📌 ⚠")
    assert frame.text.GetBackgroundColour() == CONFLICT_BG


def test_conflict_warning_cleared_when_markers_removed(frame):
    frame.text.SetValue("<<<<<<< local\nA\n=======\nB\n>>>>>>> remote\n")
    assert frame.GetTitle().startswith("📌 ⚠")
    frame.text.SetValue("A\n")
    frame.flush()
    assert frame.GetTitle() == "📌 m"
    assert frame.text.GetBackgroundColour() != CONFLICT_BG


def test_external_delete_closes_unedited(frame):
    frame.app.store.delete("m")
    frame.reconcile()
    wx.GetApp().ProcessPendingEvents()
    wx.Yield()
    assert frame.app.closed == ["m"]


def test_external_delete_keeps_unsaved_typing(frame):
    frame.text.ChangeValue("keep\n")
    frame.app.store.delete("m")
    frame.reconcile()
    assert frame.app.store.read("m") == "keep\n"
    assert frame.app.closed == []


def test_check_disk_waits_until_file_settles(frame):
    path = frame.app.store.path("m")
    external_write(path, b"half")
    frame.check_disk()  # 変化に気づいただけ。書きかけかもしれないので読まない
    assert frame.text.GetValue() == "1\n2\n3\n"
    external_write(path, b"done\n")
    frame.check_disk()  # まだ変化が続いている
    assert frame.text.GetValue() == "1\n2\n3\n"
    frame.check_disk()  # 同じ状態が続いたので取り込む
    assert frame.text.GetValue() == "done\n"


def test_own_save_is_not_seen_as_external_change(frame, monkeypatch):
    frame.text.SetValue("mine\n")
    frame.flush()
    calls = []
    monkeypatch.setattr(frame, "reconcile", lambda: calls.append(1))
    frame.check_disk()
    frame.check_disk()
    assert calls == []


def test_unsaved_marker(frame):
    assert frame.GetTitle() == "📌 m"
    frame.text.SetValue("changed")
    assert frame.GetTitle() == "📌 ● m"
    frame.flush()
    assert frame.GetTitle() == "📌 m"


def test_ctrl_s_saves_immediately(frame):
    frame.text.ChangeValue("edited\n")
    frame._save_timer.Start(10_000)
    frame.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, frame._ids["save"].GetId()))
    assert not frame._save_timer.IsRunning()
    assert frame.app.store.read("m") == "edited\n"


# --- 「ファイルを開く」で開いた任意のファイル ---

@pytest.fixture
def file_frame(wxapp, tmp_path):
    path = tmp_path / "docs" / "todo.md"
    path.parent.mkdir()
    path.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")  # BOM 付き CRLF (Windows のエディタ)
    store = LocalStore(tmp_path / "data")
    f = NoteFrame(FakeApp(store), str(path), None)
    yield f, path
    f.Destroy()


def test_file_note_title_is_file_name(file_frame):
    f, path = file_frame
    assert f.GetTitle() == "📌 todo.md"
    assert f.text.GetValue() == "a\nb\n"


def test_file_note_keeps_bom_and_crlf(file_frame):
    f, path = file_frame
    f.text.SetValue("a\nb\nc\n")
    f.flush()
    assert path.read_bytes() == b"\xef\xbb\xbfa\r\nb\r\nc\r\n"
    assert not list(path.parent.glob("*.memo-tmp"))


def test_file_note_merges_external_edit(file_frame):
    f, path = file_frame
    f.text.SetValue("a local\nb\nc\n")
    f.flush()
    f.text.ChangeValue("a local 2\nb\nc\n")  # 未保存の入力
    external_write(path, b"\xef\xbb\xbfa local\r\nb\r\nc remote\r\n")
    f.check_disk()
    f.check_disk()
    assert f.text.GetValue() == "a local 2\nb\nc remote\n"
    assert path.read_bytes() == b"\xef\xbb\xbfa local 2\r\nb\r\nc remote\r\n"


def test_file_note_menu_offers_forget_instead_of_delete(file_frame):
    f, _ = file_frame
    f.toggle_on_top()
    labels = [i.GetItemLabelText() for i in f.GetMenuBar().GetMenu(0).GetMenuItems()]
    assert "一覧から外す" in labels and "削除…" not in labels
    assert "ファイルを開く…" in labels


def test_note_is_normal_app_window_regardless_of_on_top(frame):
    import ctypes

    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    ex = lambda: u.GetWindowLongPtrW(frame.GetHandle(), -20)  # GWL_EXSTYLE
    WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x8, 0x80

    frame.Show()
    assert frame.on_top and ex() & WS_EX_TOPMOST and not ex() & WS_EX_TOOLWINDOW
    assert u.GetWindow(frame.GetHandle(), 4) == 0  # GW_OWNER: オーナーなし

    frame.toggle_on_top()
    assert not frame.on_top
    assert not ex() & (WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    assert frame.app.on_top == ("m", False)

    frame.toggle_on_top()
    assert frame.on_top and ex() & WS_EX_TOPMOST and not ex() & WS_EX_TOOLWINDOW


def test_pin_mark_and_frame_colour(frame):
    assert frame.on_top and frame.GetTitle() == "📌 m"
    assert frame._frame_pinned is True  # タイトルバー・枠を pin 色で塗った

    frame.toggle_on_top()
    assert frame.GetTitle() == "m"
    assert frame._frame_pinned is False  # OS 標準色に戻した
    frame.text.SetValue("changed")
    assert frame.GetTitle() == "● m"

    frame.toggle_on_top()
    assert frame.GetTitle() == "📌 ● m"
    assert frame._frame_pinned is True


def test_menu_bar_hidden_while_pinned(frame):
    assert frame.on_top and frame.GetMenuBar() is None

    frame.toggle_on_top()
    bar = frame.GetMenuBar()
    assert bar is not None
    assert [bar.GetMenuLabelText(i) for i in range(bar.GetMenuCount())] == ["ファイル", "表示"]
    assert not bar.IsChecked(frame._ids["top"])
    assert not bar.IsChecked(frame._ids["alpha"])

    frame.toggle_translucent()
    assert bar.IsChecked(frame._ids["alpha"])
    frame.toggle_translucent()
    assert not bar.IsChecked(frame._ids["alpha"])

    file_menu = bar.GetMenu(0)
    labels = [i.GetItemLabelText() for i in file_menu.GetMenuItems() if not i.IsSeparator()]
    assert labels[1] == "メモを開く" and labels[-1] == "アプリを終了"

    frame.app.store.write("other", "x")
    frame.app.frames = {"m": frame}
    frame._fill_open_menu()
    frame._fill_open_menu()  # 作り直しても重複しない
    items = frame._open_menu.GetMenuItems()
    assert [(i.GetItemLabelText(), i.IsChecked()) for i in items] == [("m", True), ("other", False)]
    opened = []
    frame.app.open_note = opened.append
    frame.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, items[1].GetId()))
    assert opened == ["other"]

    frame.toggle_on_top()
    assert frame.GetMenuBar() is None

def test_toggle_translucent(frame):
    import ctypes

    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    ex = lambda h: u.GetWindowLongPtrW(h, -20)  # GWL_EXSTYLE
    WS_EX_LAYERED, LWA_COLORKEY = 0x80000, 0x1
    frame.Show()
    assert not frame.translucent and frame.backdrop is None

    frame.toggle_translucent()
    note = frame.GetHandle()
    back = frame.backdrop.GetHandle()
    assert frame.translucent
    assert frame.app.translucent == ("m", True)
    # 付箋本体はキー色だけを透過し、文字は不透明のまま
    assert ex(note) & WS_EX_LAYERED
    key, alpha, flags = ctypes.c_uint32(), ctypes.c_ubyte(), ctypes.c_uint32()
    assert u.GetLayeredWindowAttributes(note, ctypes.byref(key), ctypes.byref(alpha),
                                        ctypes.byref(flags))
    assert flags.value & LWA_COLORKEY
    assert frame.text.GetBackgroundColour() == TRANSPARENT_KEY
    # 背景ウィンドウは半透明で、クライアント領域に重なり、Z オーダーで付箋のすぐ後ろ
    assert frame.backdrop.IsShown()
    assert frame.backdrop.GetScreenRect() == wx.Rect(frame.ClientToScreen((0, 0)), frame.GetClientSize())
    assert u.GetWindow(note, 2) == back  # GW_HWNDNEXT

    # 移動・最前面の切替にも追従する
    frame.Move(frame.GetPosition() + wx.Point(30, 20))
    assert frame.backdrop.GetScreenRect().GetTopLeft() == frame.ClientToScreen((0, 0))
    frame.toggle_on_top()
    assert u.GetWindow(note, 2) == back
    frame.toggle_on_top()

    frame.toggle_translucent()
    assert not frame.translucent and frame.backdrop is None
    assert not ex(note) & WS_EX_LAYERED
    assert frame.text.GetBackgroundColour() != TRANSPARENT_KEY
    assert frame.app.translucent == ("m", False)


def test_translucent_restored_without_remembering(wxapp, tmp_path):
    store = LocalStore(tmp_path)
    store.write("m", "")
    app = FakeApp(store)
    f = NoteFrame(app, "m", None, translucent=True)
    try:
        assert f.translucent
        assert not hasattr(app, "translucent")
    finally:
        f.Destroy()
