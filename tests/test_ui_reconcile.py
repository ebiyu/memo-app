"""NoteFrame.reconcile の動作 (表示はしない)。"""

import pytest

wx = pytest.importorskip("wx")

from memo.desktop.app import NoteFrame  # noqa: E402
from memo.desktop.store import LocalStore  # noqa: E402


class FakeApp:
    def __init__(self, store):
        self.store = store
        self.requests = 0
        self.closed = []
        self.sync = self
        self.engine = self
        self.held = set()
        self.forced = []

    def hold(self, slug):
        self.held.add(slug)

    def release(self, slug):
        self.held.discard(slug)

    def force(self, slug):
        self.forced.append(slug)

    def request(self):
        self.requests += 1

    def next_cascade_pos(self):
        return wx.Point(0, 0)

    def remember_rect(self, frame):
        pass

    def remember_on_top(self, slug, on_top):
        self.on_top = (slug, on_top)

    def close_note(self, slug):
        self.closed.append(slug)


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


def test_typing_is_saved(frame):
    frame.text.SetValue("1\n2\n3\nnew\n")
    frame.flush()
    assert frame.app.store.read("m") == "1\n2\n3\nnew\n"
    assert frame.app.requests == 1


def test_external_change_replaces_unedited_text(frame):
    frame.app.store.write("m", "remote\n")
    frame.reconcile()
    assert frame.text.GetValue() == "remote\n"
    assert frame.app.requests == 0


def test_external_change_merges_with_unsaved_typing(frame):
    frame.text.ChangeValue("1 local\n2\n3\n")  # 未保存の入力
    frame.app.store.write("m", "1\n2\n3 remote\n")  # 同期スレッドが書いた
    frame.reconcile()
    assert frame.text.GetValue() == "1 local\n2\n3 remote\n"
    assert frame.app.store.read("m") == "1 local\n2\n3 remote\n"


def test_conflict_turns_title_warning(frame):
    frame.text.ChangeValue("X\n2\n3\n")
    frame.app.store.write("m", "Y\n2\n3\n")
    frame.reconcile()
    assert "<<<<<<< local" in frame.text.GetValue()
    assert frame.GetTitle().startswith("⚠")


def test_remote_delete_closes_unedited(frame):
    frame.app.store.delete("m")
    frame.reconcile()
    wx.GetApp().ProcessPendingEvents()
    wx.Yield()
    assert frame.app.closed == ["m"]


def _activate(frame, active):
    frame._on_activate(wx.ActivateEvent(wx.wxEVT_ACTIVATE, active))


def test_activation_holds_and_inactivity_schedules_release(frame):
    _activate(frame, True)
    assert frame.app.held == {"m"}
    assert not frame._release_timer.IsRunning()
    _activate(frame, False)
    assert frame._release_timer.IsRunning()
    assert frame.app.held == {"m"}  # 5 分経つまでは保留のまま
    frame._release_hold()  # タイマー満了
    assert frame.app.held == set()
    _activate(frame, True)
    assert not frame._release_timer.IsRunning()



def test_ctrl_s_flushes_and_forces(frame):
    frame.text.ChangeValue("edited\n")
    frame._save_timer.Start(10_000)
    frame.sync_now()
    assert frame.app.store.read("m") == "edited\n"
    assert frame.app.forced == ["m"]


def test_unsynced_marker(frame):
    from memo.desktop.store import SyncState

    frame.app.store.set_state("m", SyncState(1, "1\n2\n3\n"))
    frame.refresh_look()
    assert frame.GetTitle() == "m"
    frame.text.SetValue("changed")
    assert frame.GetTitle() == "● m"


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
