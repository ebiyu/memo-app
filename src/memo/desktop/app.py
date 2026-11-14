"""付箋 UI (wxPython)。

- メモ 1 つ = 小さな通常ウィンドウ 1 枚 (最前面の有無にかかわらずタスクバー・Alt+Tab に出す)
- 入力欄は Win32 の素の EDIT コントロール。書式は一切持たない
- タスクトレイに常駐。メニューから新規作成・開く・同期・終了
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime
import hashlib
import json
import logging
from pathlib import Path

import httpx
import wx
import wx.adv

from memo.core import InvalidSlug, has_conflict_markers, merge3way, validate_slug

from .dpi import scale_factor
from .store import LocalStore
from .sync import BackgroundSync, SyncEngine, SyncReport

log = logging.getLogger(__name__)

NOTE_BG = wx.Colour(255, 248, 196)
CONFLICT_BG = wx.Colour(255, 214, 214)
DEFAULT_SIZE = (240, 180)  # DIP
MIN_SIZE = (120, 60)  # DIP
CASCADE_MARGIN = 40  # DIP
CASCADE_STEP = 24  # DIP
SAVE_DELAY_MS = 700
# 半透明モードの背景の不透明度 (0-255)。文字は不透明のまま
BACKDROP_ALPHA = 140
# 半透明モードで入力欄の背景に塗る色。この色の画素だけを透過させる。
# 文字のふちはこの色と混ざるので、付箋の地色に近い色にしてにじみを目立たなくする
TRANSPARENT_KEY = wx.Colour(255, 248, 197)
# 最前面 (pin) 中の付箋のタイトルバー・枠・タイトル文字の色 (Windows 11 の DWM で塗る)
PIN_CAPTION = wx.Colour(255, 234, 140)
PIN_BORDER = wx.Colour(224, 168, 32)
PIN_TEXT = wx.Colour(70, 56, 16)
PIN_MARK = "📌 "
# 編集中の付箋が非アクティブになってから、手元の変更を自動 push するまでの時間
INACTIVE_PUSH_DELAY_MS = 5 * 60 * 1000


GWL_STYLE = -16
WS_VSCROLL = 0x00200000
# SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
SWP_FRAMECHANGED_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0020

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
LWA_COLORKEY = 0x1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
RDW_INVALIDATE, RDW_ERASE, RDW_FRAME, RDW_ALLCHILDREN = 0x1, 0x4, 0x400, 0x80
WM_WINDOWPOSCHANGED = 0x0047
WM_NCDESTROY = 0x0082
WM_CONTEXTMENU = 0x007B
WM_MOUSEWHEEL = 0x020A
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
MK_LBUTTON = 0x0001
DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR = 34, 35, 36
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                               wintypes.BYTE, wintypes.DWORD]
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

_dwmapi = ctypes.WinDLL("dwmapi")
_dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD]

_SUBCLASSPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_ssize_t,
                                   ctypes.c_size_t, ctypes.c_size_t)
_comctl32 = ctypes.WinDLL("comctl32")
_comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, _SUBCLASSPROC, ctypes.c_size_t,
                                        ctypes.c_size_t]
_comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, _SUBCLASSPROC, ctypes.c_size_t]
_comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
_comctl32.DefSubclassProc.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t,
                                      ctypes.c_ssize_t]


def _ex_style(hwnd: int) -> int:
    return _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)


def _set_ex_style(hwnd: int, style: int) -> None:
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)


def _colorref(c: wx.Colour) -> int:
    return c.Red() | c.Green() << 8 | c.Blue() << 16


def _set_dwm_colour(hwnd: int, attr: int, value: int) -> None:
    """Windows 10 以前では未対応の属性なので失敗するが、見た目が変わらないだけで無害。"""
    v = wintypes.DWORD(value)
    _dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))


def _makelparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


class ThinScrollBar(wx.Window):
    """付箋の背景に溶け込む細いスクロールバー。

    素の EDIT のスクロールバーは灰色のトラックが付いて太く見えるので、
    EDIT 側のスクロールバーは消し、隣にこれを並べる。
    普段は細い線、ホバー・ドラッグ中は少し太くなる。
    """

    # DIP (96 DPI 基準)。実際の描画には __init__ で実 px に変換したものを使う
    WIDTH = 10
    THIN, THICK = 3, 6
    MIN_THUMB = 16
    PAD = 2
    EM_GETFIRSTVISIBLELINE = 0x00CE
    EM_LINESCROLL = 0x00B6

    def __init__(self, parent: wx.Window, target: wx.TextCtrl):
        cls = ThinScrollBar
        width = parent.FromDIP(cls.WIDTH)
        super().__init__(parent, size=(width, -1), style=wx.BORDER_NONE)
        self.width = width
        self.thin = self.FromDIP(cls.THIN)
        self.thick = self.FromDIP(cls.THICK)
        self.min_thumb = self.FromDIP(cls.MIN_THUMB)
        self.pad = self.FromDIP(cls.PAD)
        self.target = target
        self._hide_native_scrollbar()
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((width, -1))
        self._state: tuple[int, int, int] = (0, 0, 0)  # (first, visible, total)
        self._hover = False
        self._wheel_acc = 0
        self._drag: tuple[int, int] | None = None  # (開始 y, 開始時の first)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda e: self._end_drag())
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._set_hover(True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(False))
        # WS_VSCROLL の無い EDIT はホイールを無視するので、こちらで処理する
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        target.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        # EDIT のスクロールは wx のイベントにならないので軽くポーリングする
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.sync())
        self._timer.Start(80)
        # wx.Timer はオーナーと一緒に止まらない。破棄済みウィンドウに EVT_TIMER が
        # 届くとヒープが壊れてプロセスごと落ちるので、破棄時に必ず止める。
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

    def _on_destroy(self, evt: wx.WindowDestroyEvent) -> None:
        evt.Skip()
        if evt.GetEventObject() is self:  # 子から伝播してきたものは無視
            self._timer.Stop()

    def _hide_native_scrollbar(self) -> None:
        """EDIT から WS_VSCROLL だけを外す。

        wx.TE_NO_VSCROLL で作ると ES_AUTOVSCROLL まで外れ、表示範囲の最下行より
        下に入力できなくなる (Enter が効かない)。ES_AUTOVSCROLL は作成時にしか
        付けられないので、普通に作ってからスクロールバーのスタイルだけ外す。
        """
        user32 = ctypes.windll.user32
        hwnd = self.target.GetHandle()
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_VSCROLL)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FRAMECHANGED_ONLY)

    def _query(self) -> tuple[int, int, int]:
        t = self.target
        total = max(t.GetNumberOfLines(), 1)
        first = ctypes.windll.user32.SendMessageW(t.GetHandle(), self.EM_GETFIRSTVISIBLELINE, 0, 0)
        line_h = max(t.GetCharHeight(), 1)
        visible = max(t.GetClientSize().height // line_h, 1)
        return first, visible, total

    def sync(self) -> None:
        if self.IsBeingDeleted():
            return
        state = self._query()
        if state != self._state:
            self._state = state
            self.Refresh()

    def _thumb(self) -> tuple[int, int] | None:
        """つまみの (top, height)。スクロール不要なら None。"""
        first, visible, total = self._state
        if total <= visible:
            return None
        h = self.GetClientSize().height
        thumb_h = max(h * visible // total, self.min_thumb)
        max_first = total - visible
        top = (h - thumb_h) * min(first, max_first) // max_first
        return top, thumb_h

    def _on_paint(self, _evt) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        thumb = self._thumb()
        if thumb is None:
            return
        top, thumb_h = thumb
        active = self._hover or self._drag is not None
        w = self.thick if active else self.thin
        alpha = 150 if active else 100
        x = (self.width - w) // 2
        gc = wx.GraphicsContext.Create(dc)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, alpha)))
        gc.DrawRoundedRectangle(x, top + self.pad, w, thumb_h - 2 * self.pad, w / 2)

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self.Refresh()

    def _scroll_to(self, first: int) -> None:
        cur, visible, total = self._query()
        first = max(0, min(first, total - visible))
        if first != cur:
            ctypes.windll.user32.SendMessageW(
                self.target.GetHandle(), self.EM_LINESCROLL, 0, first - cur)
        self.sync()

    def _on_wheel(self, evt: wx.MouseEvent) -> None:
        if evt.GetWheelAxis() != wx.MOUSE_WHEEL_VERTICAL or not evt.GetWheelDelta():
            evt.Skip()
            return
        # タッチパッドの細かい回転量は端数を持ち越して積算する
        self._wheel_acc += -evt.GetWheelRotation() * evt.GetLinesPerAction()
        lines = int(self._wheel_acc / evt.GetWheelDelta())
        self._wheel_acc -= lines * evt.GetWheelDelta()
        if lines:
            self._scroll_to(self._query()[0] + lines)

    def _on_down(self, evt: wx.MouseEvent) -> None:
        thumb = self._thumb()
        if thumb is None:
            return
        top, thumb_h = thumb
        y = evt.GetY()
        first, visible, _ = self._state
        if top <= y < top + thumb_h:
            self._drag = (y, first)
            self.CaptureMouse()
            self.Refresh()
        else:  # トラッククリックは 1 ページ送り
            self._scroll_to(first + (visible if y >= top + thumb_h else -visible))

    def _on_motion(self, evt: wx.MouseEvent) -> None:
        if self._drag is None:
            return
        thumb = self._thumb()
        if thumb is None:
            return
        start_y, start_first = self._drag
        _, visible, total = self._state
        track = self.GetClientSize().height - thumb[1]
        if track > 0:
            self._scroll_to(start_first + (evt.GetY() - start_y) * (total - visible) // track)

    def _on_up(self, _evt) -> None:
        self._end_drag()

    def _end_drag(self) -> None:
        if self._drag is None:
            return
        self._drag = None
        if self.HasCapture():
            self.ReleaseMouse()
        self.Refresh()


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("failed to read config")
        self.data.setdefault("windows", {})
        self.data.setdefault("open", [])
        self.data.setdefault("not_on_top", [])  # 最前面を解除した slug
        self.data.setdefault("translucent", [])  # 半透明にした slug
        self._migrate_dpi()

    def _migrate_dpi(self) -> None:
        """DPI 非対応だった頃に保存した矩形を実座標へ引き伸ばす (1 回だけ)。

        当時の GetRect() は 96 DPI 換算の仮想座標を返していたので、そのまま
        SetRect() すると拡大環境で付箋が拡大率ぶん小さくなってしまう。
        """
        if self.data.get("dpi_migration") == 1:
            return
        self.data["dpi_migration"] = 1
        scale = scale_factor()
        if scale == 1.0 or not self.data["windows"]:
            return
        self.data["windows"] = {
            slug: [round(v * scale) for v in rect]
            for slug, rect in self.data["windows"].items()
        }
        log.info("rescaled %d window rects by %.2f for DPI awareness",
                 len(self.data["windows"]), scale)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_icon() -> wx.Icon:
    bmp = wx.Bitmap(32, 32)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(0, 0, 0)))
    dc.Clear()
    dc.SetPen(wx.Pen(wx.Colour(180, 150, 40)))
    dc.SetBrush(wx.Brush(wx.Colour(255, 226, 92)))
    dc.DrawPolygon([(3, 3), (28, 3), (28, 20), (20, 28), (3, 28)])
    dc.SetBrush(wx.Brush(wx.Colour(230, 190, 60)))
    dc.DrawPolygon([(28, 20), (20, 20), (20, 28)])
    dc.SetPen(wx.Pen(wx.Colour(150, 120, 30)))
    for y in (10, 15, 20):
        dc.DrawLine(8, y, 18 if y == 20 else 23, y)
    dc.SelectObject(wx.NullBitmap)
    bmp.SetMaskColour(wx.Colour(0, 0, 0))
    icon = wx.Icon()
    icon.CopyFromBitmap(bmp)
    return icon


def shift_lines(text: str, start: int, end: int, down: bool) -> tuple[str, int, int] | None:
    """[start, end] を含む論理行の塊を、1 行上/下の行と入れ替える。

    戻り値は (新しい本文, 移動後の選択開始, 移動後の選択終了)。端の行で動かせないときは None。
    """
    lines = text.split("\n")
    # 選択が次の行頭で終わっているときは、その行は塊に含めない
    stop = end - 1 if end > start and text[end - 1] == "\n" else end
    first = text.count("\n", 0, start)
    last = text.count("\n", 0, stop)
    if down:
        if last + 1 >= len(lines):
            return None
        lines[first:last + 2] = [lines[last + 1]] + lines[first:last + 1]
        delta = len(lines[first]) + 1
    else:
        if first == 0:
            return None
        lines[first - 1:last + 1] = lines[first:last + 1] + [lines[first - 1]]
        delta = -(len(lines[last]) + 1)
    return "\n".join(lines), start + delta, end + delta


class Backdrop(wx.Frame):
    """半透明モードで付箋の入力欄のすぐ後ろに敷く、半透明の背景ウィンドウ。

    付箋本体は TRANSPARENT_KEY 色の画素だけを完全に透過させ (文字は不透明のまま)、
    その下にこのウィンドウを置いて半透明の地色を見せる。透過した画素へのクリックは
    こちらに届くので、付箋の入力欄・スクロールバーへ転送する。
    """

    def __init__(self, note: "NoteFrame"):
        super().__init__(None, style=wx.FRAME_NO_TASKBAR | wx.FRAME_TOOL_WINDOW | wx.BORDER_NONE)
        self.note = note
        hwnd = self.GetHandle()
        # クリックされても自分はアクティブにならない (付箋本体をアクティブにする)
        _set_ex_style(hwnd, _ex_style(hwnd) | WS_EX_NOACTIVATE)
        self.SetTransparent(BACKDROP_ALPHA)
        self.SetBackgroundColour(NOTE_BG)
        self.Bind(wx.EVT_LEFT_DOWN, lambda e: self._forward(e, WM_LBUTTONDOWN, MK_LBUTTON))
        self.Bind(wx.EVT_LEFT_DCLICK, lambda e: self._forward(e, WM_LBUTTONDBLCLK, MK_LBUTTON))
        self.Bind(wx.EVT_LEFT_UP, lambda e: self._forward(e, WM_LBUTTONUP, 0))
        self.Bind(wx.EVT_RIGHT_UP, self._on_right_up)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self.Bind(wx.EVT_MOTION, self._on_motion)

    def set_colour(self, colour: wx.Colour) -> None:
        if self.GetBackgroundColour() != colour:
            self.SetBackgroundColour(colour)
            self.Refresh()

    def _target(self, evt: wx.MouseEvent) -> tuple[wx.Window | None, wx.Point]:
        pt = self.ClientToScreen(evt.GetPosition())
        return self.note.child_at(pt), pt

    def _forward(self, evt: wx.MouseEvent, msg: int, mk: int) -> None:
        target, pt = self._target(evt)
        if target is None:
            return
        if msg != WM_LBUTTONUP and not self.note.IsActive():
            self.note.Raise()
        local = target.ScreenToClient(pt)
        _user32.SendMessageW(target.GetHandle(), msg, mk, _makelparam(local.x, local.y))

    def _on_right_up(self, evt: wx.MouseEvent) -> None:
        target, pt = self._target(evt)
        if target is self.note.text:
            self.note.Raise()
            target.SetFocus()
            _user32.SendMessageW(target.GetHandle(), WM_CONTEXTMENU, target.GetHandle(),
                                 _makelparam(pt.x, pt.y))

    def _on_wheel(self, evt: wx.MouseEvent) -> None:
        target, pt = self._target(evt)
        if target is None or evt.GetWheelAxis() != wx.MOUSE_WHEEL_VERTICAL:
            return
        wparam = (evt.GetWheelRotation() & 0xFFFF) << 16
        _user32.SendMessageW(target.GetHandle(), WM_MOUSEWHEEL, wparam, _makelparam(pt.x, pt.y))

    def _on_motion(self, evt: wx.MouseEvent) -> None:
        target, _ = self._target(evt)
        cursor = wx.CURSOR_IBEAM if target is self.note.text else wx.CURSOR_ARROW
        self.SetCursor(wx.Cursor(cursor))


class NoteFrame(wx.Frame):
    # タスクバー・Alt+Tab に出すため wx.FRAME_TOOL_WINDOW / wx.FRAME_NO_TASKBAR は使わない
    # (後者は隠しオーナー付きで作られ、Alt+Tab に出なくなる)。
    BASE_STYLE = wx.CAPTION | wx.CLOSE_BOX | wx.RESIZE_BORDER
    ON_TOP_STYLE = wx.STAY_ON_TOP

    def __init__(self, app: "MemoApp", slug: str, rect: list[int] | None, on_top: bool = True,
                 translucent: bool = False):
        style = self.BASE_STYLE | (self.ON_TOP_STYLE if on_top else 0)
        super().__init__(None, title=slug, style=style)
        self.app = app
        self.slug = slug
        self.SetMinSize(self.FromDIP(wx.Size(*MIN_SIZE)))

        self.text = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_NOHIDESEL | wx.BORDER_NONE)
        self.text.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL,
                                  wx.FONTWEIGHT_NORMAL, faceName="Yu Gothic UI"))
        self.scrollbar = ThinScrollBar(self, self.text)
        sizer = wx.BoxSizer()
        sizer.Add(self.text, 1, wx.EXPAND)
        sizer.Add(self.scrollbar, 0, wx.EXPAND)
        self.SetSizer(sizer)

        self.disk_text = app.store.read(slug) or ""
        self.text.ChangeValue(self.disk_text)
        self._save_timer = wx.CallLater(SAVE_DELAY_MS, self.reconcile)
        self._save_timer.Stop()
        self._release_timer = wx.CallLater(INACTIVE_PUSH_DELAY_MS, self._release_hold)
        self._release_timer.Stop()
        self.translucent = False
        self.backdrop: Backdrop | None = None
        self._subclass_proc = None
        self._frame_pinned: bool | None = None  # DWM で塗った状態のキャッシュ
        self._update_look()
        if translucent:
            self.set_translucent(True, remember=False)

        if rect:
            self.SetRect(wx.Rect(*rect))
        else:
            self.SetSize(self.FromDIP(wx.Size(*DEFAULT_SIZE)))
            self.SetPosition(app.next_cascade_pos(self))

        self.text.Bind(wx.EVT_TEXT, self._on_text)
        self.text.Bind(wx.EVT_KEY_DOWN, self._on_key_down)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.Bind(wx.EVT_MOVE_END, lambda e: (app.remember_rect(self), e.Skip()))
        self.Bind(wx.EVT_SIZE, lambda e: (app.remember_rect(self), e.Skip()))
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)

        self._ids = ids = {name: wx.NewIdRef()
                           for name in ("new", "close", "top", "delete", "sync", "alpha", "quit")}
        self._open_menu: wx.Menu | None = None
        self._open_item_ids: list[wx.WindowIDRef] = []
        self.Bind(wx.EVT_MENU, lambda e: app.quit(), id=ids["quit"])
        self.Bind(wx.EVT_MENU_OPEN, self._on_menu_open)
        self.Bind(wx.EVT_MENU, lambda e: app.new_note_dialog(), id=ids["new"])
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=ids["close"])
        self.Bind(wx.EVT_MENU, lambda e: self.toggle_on_top(), id=ids["top"])
        self.Bind(wx.EVT_MENU, lambda e: app.delete_note(self.slug), id=ids["delete"])
        self.Bind(wx.EVT_MENU, lambda e: self.sync_now(), id=ids["sync"])
        self.Bind(wx.EVT_MENU, lambda e: self.toggle_translucent(), id=ids["alpha"])
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord("N"), ids["new"]),
            (wx.ACCEL_CTRL, ord("W"), ids["close"]),
            (wx.ACCEL_CTRL, ord("T"), ids["top"]),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("T"), ids["alpha"]),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_DELETE, ids["delete"]),
            (wx.ACCEL_CTRL, ord("S"), ids["sync"]),
        ]))
        self._update_menu_bar()

    # --- メニューバー (pin 中は付箋らしく見せるため出さない) ---
    def _build_menu_bar(self) -> wx.MenuBar:
        # ショートカットはフレームのアクセラレータテーブルが担うので、ラベルの
        # "	" 以降は表示用。編集系 (Ctrl+C など) は行単位コピーと衝突するので載せない
        ids = self._ids
        file_menu = wx.Menu()
        file_menu.Append(ids["new"], "新規メモ…	Ctrl+N")
        self._open_menu = wx.Menu()  # 中身は開くたびに _fill_open_menu で作り直す
        file_menu.AppendSubMenu(self._open_menu, "メモを開く")
        file_menu.Append(ids["sync"], "今すぐ同期	Ctrl+S")
        file_menu.AppendSeparator()
        file_menu.Append(ids["delete"], "削除…	Ctrl+Shift+Delete")
        file_menu.AppendSeparator()
        file_menu.Append(ids["close"], "閉じる	Ctrl+W")
        file_menu.Append(ids["quit"], "アプリを終了")
        view_menu = wx.Menu()
        view_menu.AppendCheckItem(ids["top"], "最前面に固定 (pin)	Ctrl+T")
        view_menu.AppendCheckItem(ids["alpha"], "背景を半透明	Ctrl+Shift+T").Check(self.translucent)
        bar = wx.MenuBar()
        bar.Append(file_menu, "ファイル(&F)")
        bar.Append(view_menu, "表示(&V)")
        return bar

    def _on_menu_open(self, evt: wx.MenuEvent) -> None:
        evt.Skip()
        # 「ファイル」を開いた時点で作っておく (サブメニュー単体の open 通知と
        # wrapper の同一性に頼らないため、どのメニューでも作り直す)
        if self._open_menu is not None:
            self._fill_open_menu()

    def _fill_open_menu(self) -> None:
        """「メモを開く」をメモ一覧で作り直す。開いているものにはチェックを付ける。"""
        menu = self._open_menu
        for item in list(menu.GetMenuItems()):
            menu.Delete(item)
        for id_ in self._open_item_ids:
            self.Unbind(wx.EVT_MENU, id=id_)
        self._open_item_ids = []
        slugs = self.app.store.slugs()
        for slug in slugs:
            id_ = wx.NewIdRef()
            self._open_item_ids.append(id_)
            menu.AppendCheckItem(id_, slug).Check(slug in self.app.frames)
            # 開いていれば前面に出す (トレイと違い、選んでも閉じない)
            self.Bind(wx.EVT_MENU, lambda e, s=slug: self.app.open_note(s), id=id_)
        if not slugs:
            menu.Append(wx.ID_ANY, "(メモなし)").Enable(False)

    def _update_menu_bar(self) -> None:
        want = not self.on_top
        bar = self.GetMenuBar()
        if want and bar is None:
            self.SetMenuBar(self._build_menu_bar())
        elif not want and bar is not None:
            self.SetMenuBar(None)
            bar.Destroy()
            self._open_menu = None
        else:
            return
        self.place_backdrop()  # クライアント領域が変わるので半透明の下敷きを合わせ直す

    # --- 行単位の編集: 選択なしの Ctrl+C / Ctrl+X、Alt+↑/↓、Ctrl+Enter ---
    def _on_key_down(self, evt: wx.KeyEvent) -> None:
        key = evt.GetKeyCode()
        mods = evt.GetModifiers()
        if mods == wx.MOD_CONTROL and key in (ord("C"), ord("X")):
            start, end = self.text.GetSelection()
            if start == end:
                self._copy_line(cut=key == ord("X"))
                return
        if mods == wx.MOD_ALT and key in (wx.WXK_UP, wx.WXK_DOWN,
                                          wx.WXK_NUMPAD_UP, wx.WXK_NUMPAD_DOWN):
            self.move_lines(down=key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN))
            return
        if mods == wx.MOD_CONTROL and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.open_line_below()
            return
        evt.Skip()

    def _index_conv(self, value: str):
        """MSW の EDIT は位置を CRLF 2 文字で数えるので、GetValue() の添字と相互変換する。"""
        crlf = self.text.GetLastPosition() != len(value)

        def to_index(pos: int) -> int:
            if not crlf:
                return pos
            return len(value.replace("\n", "\r\n")[:pos].replace("\r\n", "\n"))

        def to_pos(text: str, idx: int) -> int:
            return idx + text.count("\n", 0, idx) if crlf else idx

        return to_index, to_pos

    def _copy_line(self, cut: bool) -> None:
        """カーソルのある論理行 (折り返しではなく改行区切り) を改行付きでコピーする。"""
        value = self.text.GetValue()
        to_index, to_pos = self._index_conv(value)
        idx = to_index(self.text.GetInsertionPoint())
        s = value.rfind("\n", 0, idx) + 1
        e = value.find("\n", idx)
        if e == -1:
            e = len(value)
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(value[s:e] + "\n"))
            finally:
                wx.TheClipboard.Close()
        if not cut:
            return
        if e < len(value):
            rs, re_ = s, e + 1  # 行末の改行ごと消す -> カーソルは次の行頭
            target = s
        elif s > 0:
            rs, re_ = s - 1, e  # 最終行は直前の改行ごと消す -> カーソルは新しい最終行の行頭
            target = value.rfind("\n", 0, s - 1) + 1
        else:
            rs, re_ = s, e  # 1 行だけのとき
            target = 0
        if rs == re_:
            return
        self.text.Remove(to_pos(value, rs), to_pos(value, re_))
        self.text.SetInsertionPoint(to_pos(self.text.GetValue(), target))

    def open_line_below(self) -> None:
        """キャレットが行のどこにあっても、その行の下に空行を作ってそこへ移る。"""
        value = self.text.GetValue()
        to_index, to_pos = self._index_conv(value)
        idx = to_index(self.text.GetInsertionPoint())
        e = value.find("\n", idx)
        if e == -1:
            e = len(value)
        self.text.SetInsertionPoint(to_pos(value, e))  # 選択は解除される
        self.text.WriteText("\n")  # EVT_TEXT 経由で自動保存に乗り、Ctrl+Z でも戻せる
        new_value = self.text.GetValue()
        self.text.SetInsertionPoint(to_pos(new_value, e + 1))

    def move_lines(self, down: bool) -> None:
        """カーソル行 (選択があればその範囲の行) を 1 行上/下へ入れ替える。"""
        value = self.text.GetValue()
        to_index, to_pos = self._index_conv(value)
        sel_start, sel_end = self.text.GetSelection()
        moved = shift_lines(value, to_index(sel_start), to_index(sel_end), down)
        if moved is None:
            return  # 端の行なので動かせない
        new_value, new_start, new_end = moved
        if new_value != value:
            # 素の EDIT の undo は 1 段だけで、消す・入れるの 2 操作に分けると
            # Ctrl+Z が中途半端な状態を作る。全体を入れ替えて undo 履歴を捨てる
            self.text.SetValue(new_value)  # EVT_TEXT 経由で自動保存に乗る
        self.text.SetSelection(to_pos(new_value, new_start), to_pos(new_value, new_end))

    # --- 保存とローカル変更の取り込み ---
    def _on_text(self, _evt) -> None:
        self._update_look()
        self._save_timer.Start(SAVE_DELAY_MS)

    def reconcile(self) -> None:
        """エディタ・ディスク・最後に見たディスク内容の 3 者を突き合わせる。

        同期スレッドがディスクを書き換えていた場合、未保存の入力と 3-way merge する。
        """
        if self.IsBeingDeleted():
            return
        store = self.app.store
        wrote = False
        with store.lock:
            disk = store.read(self.slug)
            editor = self.text.GetValue()
            if disk is None:
                if editor == self.disk_text:
                    # リモートで削除され、手元も未編集 -> 閉じる
                    wx.CallAfter(self.app.close_note, self.slug)
                    return
                disk = self.disk_text  # 編集中なら書き戻して残す
            if disk == self.disk_text:
                if editor != disk:
                    store.write(self.slug, editor)
                    self.disk_text = editor
                    wrote = True
            elif editor == self.disk_text:
                self._replace_text(disk)
                self.disk_text = disk
            else:
                merged = merge3way(self.disk_text, editor, disk).text
                store.write(self.slug, merged)
                self._replace_text(merged)
                self.disk_text = merged
                wrote = True
        self._update_look()
        if wrote:
            self.app.sync.request()

    # --- 同期タイミング ---
    # アクティブな間は手元の変更を push しない (Ctrl+S で即時)。
    # 非アクティブになって 5 分経ったら push。手元が未変更なら pull は常に自動。
    def _on_activate(self, evt) -> None:
        evt.Skip()
        if not self or self.IsBeingDeleted():
            return
        if evt.GetActive():
            self.app.last_active = self.slug
            self._release_timer.Stop()
            self.app.engine.hold(self.slug)
        else:
            self.flush()
            self._release_timer.Start(INACTIVE_PUSH_DELAY_MS)

    def _release_hold(self) -> None:
        if not self:
            return
        self.app.engine.release(self.slug)
        self.app.sync.request()

    def sync_now(self) -> None:
        self.flush()
        self.app.engine.force(self.slug)
        self.app.sync.request()

    def flush(self) -> None:
        if self._save_timer.IsRunning():
            self._save_timer.Stop()
        self.reconcile()

    def _replace_text(self, text: str) -> None:
        pos = self.text.GetInsertionPoint()
        self.text.ChangeValue(text)
        self.text.SetInsertionPoint(min(pos, self.text.GetLastPosition()))

    def _update_look(self) -> None:
        text = self.text.GetValue()
        st = self.app.store.get_state(self.slug)
        # Ctrl+S でマーカーごとサーバーに保存済み (base にマーカーがあり、未解決の
        # コンフリクトでもない) なら、意図的に残したものとして警告しない
        accepted = st is not None and not st.conflict and has_conflict_markers(st.base)
        conflict = has_conflict_markers(text) and not accepted
        unsynced = st is None or st.base != text
        mark = "⚠ " if conflict else "● " if unsynced else ""
        title = (PIN_MARK if self.on_top else "") + mark + self.slug
        if self.GetTitle() != title:
            self.SetTitle(title)
        self._update_frame_colours()
        bg = CONFLICT_BG if conflict else NOTE_BG
        if self.backdrop:
            self.backdrop.set_colour(bg)
            bg = TRANSPARENT_KEY
        if self.text.GetBackgroundColour() != bg:
            self.text.SetBackgroundColour(bg)
            self.text.Refresh()
            self.scrollbar.SetBackgroundColour(bg)
            self.scrollbar.Refresh()

    def _update_frame_colours(self) -> None:
        """pin 中はタイトルバーと枠を付箋らしい黄色に、解除中は OS 標準に戻す。"""
        pinned = self.on_top
        if self._frame_pinned == pinned:
            return
        self._frame_pinned = pinned
        hwnd = self.GetHandle()
        for attr, colour in ((DWMWA_CAPTION_COLOR, PIN_CAPTION), (DWMWA_BORDER_COLOR, PIN_BORDER),
                             (DWMWA_TEXT_COLOR, PIN_TEXT)):
            _set_dwm_colour(hwnd, attr, _colorref(colour) if pinned else DWMWA_COLOR_DEFAULT)

    @property
    def on_top(self) -> bool:
        return bool(self.GetWindowStyleFlag() & wx.STAY_ON_TOP)

    def toggle_on_top(self) -> None:
        self.set_on_top(not self.on_top)

    def set_on_top(self, on_top: bool) -> None:
        style = self.GetWindowStyleFlag()
        style = style | self.ON_TOP_STYLE if on_top else style & ~self.ON_TOP_STYLE
        self.SetWindowStyleFlag(style)
        self._update_look()
        self._update_menu_bar()
        self.app.remember_on_top(self.slug, on_top)

    def toggle_translucent(self) -> None:
        self.set_translucent(not self.translucent)

    def set_translucent(self, translucent: bool, remember: bool = True) -> None:
        """背景だけを半透明にする (文字は不透明のまま)。

        付箋本体を TRANSPARENT_KEY 色だけ透過するレイヤードウィンドウにし、
        すぐ後ろに半透明の Backdrop を敷く。
        """
        hwnd = self.GetHandle()
        if translucent and not self.backdrop:
            self._install_subclass()
            self.backdrop = Backdrop(self)
            _set_ex_style(hwnd, _ex_style(hwnd) | WS_EX_LAYERED)
            _user32.SetLayeredWindowAttributes(hwnd, _colorref(TRANSPARENT_KEY), 255, LWA_COLORKEY)
            self._update_look()
            self.place_backdrop()
        elif not translucent and self.backdrop:
            self.backdrop.Destroy()
            self.backdrop = None
            _set_ex_style(hwnd, _ex_style(hwnd) & ~WS_EX_LAYERED)
            self._update_look()
            _user32.RedrawWindow(hwnd, None, None,
                                 RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_ALLCHILDREN)
        self.translucent = translucent
        bar = self.GetMenuBar()
        if bar is not None:
            bar.Check(self._ids["alpha"], translucent)
        if remember:
            self.app.remember_translucent(self.slug, translucent)

    def child_at(self, pt: wx.Point) -> wx.Window | None:
        """スクリーン座標 pt の下にある入力欄・スクロールバー。"""
        for w in (self.text, self.scrollbar):
            if w.GetScreenRect().Contains(pt):
                return w
        return None

    def place_backdrop(self) -> None:
        """Backdrop を付箋のクライアント領域に重ね、Z オーダーで付箋のすぐ後ろに置く。"""
        if not self.backdrop:
            return
        hwnd, back = self.GetHandle(), self.backdrop.GetHandle()
        if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
            self.backdrop.Hide()
            return
        if not self.backdrop.IsShown():
            self.backdrop.ShowWithoutActivating()
        rc = wintypes.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rc))
        origin = wintypes.POINT(0, 0)
        _user32.ClientToScreen(hwnd, ctypes.byref(origin))
        topmost = bool(_ex_style(hwnd) & WS_EX_TOPMOST)
        if bool(_ex_style(back) & WS_EX_TOPMOST) != topmost:
            _user32.SetWindowPos(back, HWND_TOPMOST if topmost else HWND_NOTOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        _user32.SetWindowPos(back, hwnd, origin.x, origin.y, rc.right, rc.bottom,
                             SWP_NOACTIVATE)

    def _install_subclass(self) -> None:
        """付箋の移動・リサイズ・Z オーダー変更・最小化に Backdrop を追従させる。

        wx には WM_WINDOWPOSCHANGED に当たるイベントが無いので、ウィンドウを
        サブクラス化して拾う。
        """
        if self._subclass_proc:
            return

        def proc(hwnd, msg, wparam, lparam, id_, _ref):
            try:
                if msg == WM_WINDOWPOSCHANGED and self.backdrop:
                    self.place_backdrop()
                elif msg == WM_NCDESTROY:
                    _comctl32.RemoveWindowSubclass(hwnd, self._subclass_proc, id_)
            except Exception:
                log.exception("backdrop sync failed")
            return _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)

        self._subclass_proc = _SUBCLASSPROC(proc)  # GC されないよう保持する
        _comctl32.SetWindowSubclass(self.GetHandle(), self._subclass_proc, 1, 0)

    def _on_destroy(self, evt: wx.WindowDestroyEvent) -> None:
        evt.Skip()
        if evt.GetEventObject() is self and self.backdrop:
            self.backdrop.Destroy()
            self.backdrop = None

    def refresh_look(self) -> None:
        self._update_look()

    def _on_close(self, evt) -> None:
        self.flush()
        self._release_timer.Stop()
        self.app.engine.release(self.slug)  # 閉じたら即 push 対象に戻す
        self.app.sync.request()
        self.app.close_note(self.slug)


class MemoTaskBarIcon(wx.adv.TaskBarIcon):
    def __init__(self, app: "MemoApp"):
        super().__init__()
        self.app = app
        self.icon = make_icon()
        self.set_status("起動中")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda e: app.new_note_dialog())
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_UP, lambda e: app.raise_all())

    def set_status(self, status: str) -> None:
        self.SetIcon(self.icon, f"memo — {status}")

    def CreatePopupMenu(self):
        app = self.app
        menu = wx.Menu()
        self._item(menu, "新規メモ…", app.new_note_dialog)

        open_menu = wx.Menu()
        slugs = app.store.slugs()
        for slug in slugs:
            item = open_menu.AppendCheckItem(wx.ID_ANY, slug)
            item.Check(slug in app.frames)
            self.Bind(wx.EVT_MENU, lambda e, s=slug: app.toggle_note(s), item)
        if not slugs:
            open_menu.Append(wx.ID_ANY, "(メモなし)").Enable(False)
        menu.AppendSubMenu(open_menu, "メモ")

        menu.AppendSeparator()
        self._item(menu, "今すぐ同期", app.sync.request)
        self._item(menu, "データフォルダを開く", lambda: wx.LaunchDefaultApplication(str(app.store.root)))
        menu.AppendSeparator()
        self._item(menu, "終了", app.quit)
        return menu

    def _item(self, menu: wx.Menu, label: str, fn) -> None:
        item = menu.Append(wx.ID_ANY, label)
        self.Bind(wx.EVT_MENU, lambda e: fn(), item)


class MemoApp(wx.App):
    def __init__(self, data_dir: Path, server: str | None, interval: float, token: str | None):
        self._data_dir = data_dir
        self._server = server
        self._interval = interval
        self._token = token
        super().__init__(redirect=False)

    def OnInit(self) -> bool:
        self.SetAppName("memo")
        # 終了は quit() で明示的に行う (最後の付箋を閉じたときも quit() 経由)
        self.SetExitOnFrameDelete(False)
        key = hashlib.sha1(str(self._data_dir.resolve()).lower().encode()).hexdigest()[:12]
        self._checker = wx.SingleInstanceChecker(f"memo-{key}")
        if self._checker.IsAnotherRunning():
            wx.MessageBox("memo は既に起動しています。", "memo")
            return False

        self.store = LocalStore(self._data_dir)
        self.config = Config(self._data_dir / "config.json")
        server = self._server or self.config.data.get("server") or "http://127.0.0.1:8765"
        self.config.data["server"] = server
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        self.http = httpx.Client(base_url=server, timeout=10.0, headers=headers)

        self.frames: dict[str, NoteFrame] = {}
        self.last_active: str | None = None
        self._cascade = 0
        self._config_timer = wx.CallLater(1000, self.config.save)
        self._config_timer.Stop()

        engine = SyncEngine(self.store, self.http, self._on_local_change_threaded)
        self.engine = engine
        self.sync = BackgroundSync(engine, interval=self._interval, on_status=self._on_status_threaded)
        self.tray = MemoTaskBarIcon(self)
        self._register_hotkey()

        for slug in list(self.config.data["open"]):
            if self.store.read(slug) is not None:
                self.open_note(slug)
        self.ensure_window()

        self.sync.start()
        return True

    # --- 同期スレッドからの通知 ---
    def _on_local_change_threaded(self, slug: str, _text: str | None) -> None:
        wx.CallAfter(self._on_local_change, slug)

    def _on_local_change(self, slug: str) -> None:
        frame = self.frames.get(slug)
        if frame:
            frame.reconcile()

    def _on_status_threaded(self, status: str, report: SyncReport | None) -> None:
        wx.CallAfter(self._on_status, status, report)

    def _on_status(self, status: str, report: SyncReport | None) -> None:
        label = {"ok": "同期済み", "offline": "オフライン", "conflict": "コンフリクトあり"}[status]
        self.tray.set_status(f"{label} ({self.config.data['server']})")
        for frame in self.frames.values():
            frame.refresh_look()
        if report and report.conflicts:
            for slug in report.conflicts:
                self.open_note(slug)
            self.tray.ShowBalloon(
                "memo", "自動マージできない変更がありました: " + ", ".join(report.conflicts)
            )

    # --- ウィンドウ管理 ---
    def next_cascade_pos(self, win: wx.Window) -> wx.Point:
        area = wx.Display(0).GetClientArea()
        self._cascade = (self._cascade + 1) % 10
        off = win.FromDIP(CASCADE_STEP) * self._cascade
        margin = win.FromDIP(CASCADE_MARGIN)
        width = win.FromDIP(DEFAULT_SIZE[0])
        return wx.Point(area.right - width - margin - off, area.top + margin + off)

    def open_note(self, slug: str, create: bool = False) -> None:
        if slug in self.frames:
            self.frames[slug].Raise()
            return
        if create and self.store.read(slug) is None:
            self.store.write(slug, "")
            self.sync.request()
        frame = NoteFrame(
            self,
            slug,
            self.config.data["windows"].get(slug),
            on_top=slug not in self.config.data["not_on_top"],
            translucent=slug in self.config.data["translucent"],
        )
        self.frames[slug] = frame
        frame.Show()
        if slug not in self.config.data["open"]:
            self.config.data["open"].append(slug)
        self._config_timer.Start(1000)

    def close_note(self, slug: str) -> None:
        deleted = self.store.read(slug) is None  # リモート削除による自動クローズ
        if not deleted and list(self.frames) == [slug]:
            # 最後の付箋を閉じたらアプリごと終了する。付箋は「開いたまま」として
            # 記録されるので、次回起動時に再表示される。
            self.quit()
            return
        frame = self.frames.pop(slug, None)
        if frame:
            self.remember_rect(frame)
            frame.Destroy()
        if slug in self.config.data["open"]:
            self.config.data["open"].remove(slug)
        self._config_timer.Start(1000)
        self.ensure_window()

    def ensure_window(self) -> None:
        """付箋が 1 枚も表示されていなければ、既存メモの先頭 (なければ新規) を開く。"""
        if self.frames:
            return
        slugs = self.store.slugs()
        if slugs:
            self.open_note(slugs[0])
        else:
            self.open_note("scratch", create=True)

    def toggle_note(self, slug: str) -> None:
        if slug in self.frames:
            self.frames[slug].Close()
        else:
            self.open_note(slug)

    def raise_all(self) -> None:
        for f in self.frames.values():
            f.Raise()

    # --- グローバルホットキー ---
    def _register_hotkey(self) -> None:
        # RegisterHotKey にはウィンドウが要るので、表示しない専用フレームを持たせる
        self._hotkey_owner = wx.Frame(None)
        self._hotkey_id = 1  # RegisterHotKey のアプリ用 ID は 0x0000〜0xBFFF
        if self._hotkey_owner.RegisterHotKey(
                self._hotkey_id, wx.MOD_CONTROL | wx.MOD_ALT, ord("M")):
            self._hotkey_owner.Bind(wx.EVT_HOTKEY, lambda e: self.focus_notes(),
                                    id=self._hotkey_id)
        else:
            log.warning("global hotkey Ctrl+Alt+M is already in use")
            self.tray.ShowBalloon("memo", "Ctrl+Alt+M は他のアプリが使用中のため登録できませんでした")

    def focus_notes(self) -> None:
        """全付箋を前面に出し、最後にアクティブだった付箋にフォーカスする。"""
        self.ensure_window()
        for f in self.frames.values():
            if f.IsIconized():
                f.Iconize(False)
            f.Raise()
        target = self.frames.get(self.last_active) or next(iter(self.frames.values()))
        target.Raise()
        target.text.SetFocus()

    def remember_rect(self, frame: NoteFrame) -> None:
        if frame.IsIconized():
            return
        r = frame.GetRect()
        self.config.data["windows"][frame.slug] = [r.x, r.y, r.width, r.height]
        self._config_timer.Start(1000)

    def remember_on_top(self, slug: str, on_top: bool) -> None:
        lst = self.config.data["not_on_top"]
        if on_top and slug in lst:
            lst.remove(slug)
        elif not on_top and slug not in lst:
            lst.append(slug)
        self._config_timer.Start(1000)

    def remember_translucent(self, slug: str, translucent: bool) -> None:
        lst = self.config.data["translucent"]
        if not translucent and slug in lst:
            lst.remove(slug)
        elif translucent and slug not in lst:
            lst.append(slug)
        self._config_timer.Start(1000)

    def new_note_dialog(self) -> None:
        prefix = datetime.date.today().strftime("%y%m%d") + "-"
        dlg = wx.TextEntryDialog(
            None, "slug (英小文字・数字・ハイフン、先頭と末尾の '-' は不可)", "新規メモ", prefix
        )
        text = next((c for c in dlg.GetChildren() if isinstance(c, wx.TextCtrl)), None)
        if text:
            # slug は ASCII のみなので IME を切り離す (半角のまま打てるように)
            imm = ctypes.windll.imm32
            imm.ImmAssociateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            imm.ImmAssociateContext.restype = ctypes.c_void_p
            imm.ImmAssociateContext(text.GetHandle(), None)

        def normalized() -> str:
            # dlg.GetValue() は OK 処理まで入力欄の内容を反映しないので TextCtrl から読む
            return (text or dlg).GetValue().strip().lower()

        def is_valid() -> bool:
            try:
                validate_slug(normalized())
                return True
            except InvalidSlug:
                return False

        # 不正な slug ではダイアログを閉じさせない。閉じて MessageBox → 再表示にすると、
        # MessageBox を Enter で閉じた入力が再表示後のダイアログに漏れて無限ループになる。
        ok_button = dlg.FindWindow(wx.ID_OK)

        def update_ok(event=None) -> None:
            if ok_button:
                ok_button.Enable(is_valid())
            if event:
                event.Skip()

        def on_ok(event) -> None:
            if is_valid():
                event.Skip()
            else:
                wx.Bell()

        dlg.Bind(wx.EVT_BUTTON, on_ok, id=wx.ID_OK)
        if text:
            text.Bind(wx.EVT_TEXT, update_ok)
            # 既定では全選択されるので、表示後にカーソルを末尾へ移す
            wx.CallAfter(text.SetInsertionPointEnd)
        update_ok()
        try:
            if dlg.ShowModal() == wx.ID_OK and is_valid():
                self.open_note(normalized(), create=True)
        finally:
            dlg.Destroy()

    def delete_note(self, slug: str) -> None:
        if wx.MessageBox(f"メモ「{slug}」を削除しますか？", "memo",
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
            return
        frame = self.frames.pop(slug, None)
        if frame:
            frame._save_timer.Stop()
            frame._release_timer.Stop()
            frame.Destroy()
        self.engine.release(slug)
        self.store.delete(slug)
        if slug in self.config.data["open"]:
            self.config.data["open"].remove(slug)
        self.config.data["windows"].pop(slug, None)
        for key in ("not_on_top", "translucent"):
            if slug in self.config.data[key]:
                self.config.data[key].remove(slug)
        self.config.save()
        self.sync.request()
        self.ensure_window()

    def quit(self) -> None:
        open_slugs = list(self.frames)
        for frame in list(self.frames.values()):
            frame.flush()
            self.remember_rect(frame)
            frame.Destroy()
        self.frames.clear()
        self.config.data["open"] = open_slugs
        self.config.save()
        self.sync.stop()
        self._hotkey_owner.UnregisterHotKey(self._hotkey_id)
        self._hotkey_owner.Destroy()
        self.tray.RemoveIcon()
        self.tray.Destroy()
        self.ExitMainLoop()
