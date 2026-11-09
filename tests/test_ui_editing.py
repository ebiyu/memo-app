"""Alt+↑/↓ の行移動 (表示はしない)。"""

import pytest

wx = pytest.importorskip("wx")

from test_ui_reconcile import FakeApp  # noqa: E402

from memo.desktop.app import NoteFrame, shift_lines  # noqa: E402
from memo.desktop.store import LocalStore  # noqa: E402


@pytest.fixture(scope="module")
def wxapp():
    app = wx.App(False)
    yield app
    app.Destroy()


@pytest.fixture
def frame(wxapp, tmp_path):
    store = LocalStore(tmp_path)
    store.write("m", "a\nbb\nccc\n")
    f = NoteFrame(FakeApp(store), "m", None)
    yield f
    f.Destroy()


def caret(frame) -> int:
    """キャレット位置を GetValue() の添字で返す (MSW の CRLF 位置を吸収する)。"""
    to_index, _ = frame._index_conv(frame.text.GetValue())
    return to_index(frame.text.GetInsertionPoint())


def set_caret(frame, idx: int) -> None:
    _, to_pos = frame._index_conv(frame.text.GetValue())
    frame.text.SetInsertionPoint(to_pos(frame.text.GetValue(), idx))


def select(frame, start: int, end: int) -> None:
    value = frame.text.GetValue()
    _, to_pos = frame._index_conv(value)
    frame.text.SetSelection(to_pos(value, start), to_pos(value, end))


# --- 行の入れ替えそのもの ---

def test_move_down_swaps_with_next_line():
    assert shift_lines("a\nbb\nccc", 0, 0, down=True) == ("bb\na\nccc", 3, 3)


def test_move_up_swaps_with_previous_line():
    assert shift_lines("a\nbb\nccc", 2, 4, down=False) == ("bb\na\nccc", 0, 2)


def test_cursor_offset_in_line_is_kept():
    # "ccc" の 2 文字目にカーソル -> 入れ替え後も "ccc" の 2 文字目
    text, start, end = shift_lines("a\nbb\nccc", 6, 6, down=False)
    assert (text, start, end) == ("a\nccc\nbb", 3, 3)
    assert text[start:start + 2] == "cc"


def test_selection_block_moves_together():
    assert shift_lines("a\nb\nc\nd", 0, 3, down=True) == ("c\na\nb\nd", 2, 5)


def test_selection_ending_at_next_line_head_excludes_it():
    # "a\n" だけを選んだ状態。"b" は巻き込まない
    assert shift_lines("a\nb\nc", 0, 2, down=True) == ("b\na\nc", 2, 4)


def test_edges_do_not_move():
    assert shift_lines("a\nb", 0, 0, down=False) is None
    assert shift_lines("a\nb", 2, 2, down=True) is None
    # 末尾の改行は「空の最終行」なので、その 1 つ上の行はまだ下に動かせる
    assert shift_lines("a\nb\n", 2, 2, down=True) == ("a\n\nb", 3, 3)


# --- 付箋に繋いだときの挙動 ---

def test_frame_moves_line_and_saves(frame):
    set_caret(frame, 2)  # "bb" の行頭
    frame.move_lines(down=False)
    assert frame.text.GetValue() == "bb\na\nccc\n"
    assert caret(frame) == 0
    frame.flush()
    assert frame.app.store.read("m") == "bb\na\nccc\n"


def test_frame_keeps_selection(frame):
    select(frame, 0, 4)  # "a\nbb"
    frame.move_lines(down=True)
    assert frame.text.GetValue() == "ccc\na\nbb\n"
    assert frame.text.GetStringSelection().replace("\r\n", "\n") == "a\nbb"
    # 素の EDIT の undo は 1 段だけで中途半端な状態を作るので、履歴は残さない
    assert not frame.text.CanUndo()


def test_frame_at_edge_is_noop(frame):
    set_caret(frame, 0)
    frame.move_lines(down=False)
    assert frame.text.GetValue() == "a\nbb\nccc\n"
    assert caret(frame) == 0


def test_frame_moves_identical_lines(frame):
    frame.text.ChangeValue("x\nx\ny\n")
    set_caret(frame, 0)
    frame.move_lines(down=True)  # 同じ内容の行同士 -> 本文は変わらずキャレットだけ動く
    assert frame.text.GetValue() == "x\nx\ny\n"
    assert caret(frame) == 2
