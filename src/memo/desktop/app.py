"""付箋 UI (wxPython)。

- メモ 1 つ = 小さな通常ウィンドウ 1 枚 (最前面の有無にかかわらずタスクバー・Alt+Tab に出す)
- 入力欄は Win32 の素の EDIT コントロール。書式は一切持たない
- タスクトレイに常駐。メニューから新規作成・開く・同期・終了
"""

from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import logging
from pathlib import Path

import httpx
import wx
import wx.adv

from memo.core import InvalidSlug, has_conflict_markers, merge3way, validate_slug

from .store import LocalStore
from .sync import BackgroundSync, SyncEngine, SyncReport

log = logging.getLogger(__name__)

NOTE_BG = wx.Colour(255, 248, 196)
CONFLICT_BG = wx.Colour(255, 214, 214)
DEFAULT_SIZE = (240, 180)
SAVE_DELAY_MS = 700
# 編集中の付箋が非アクティブになってから、手元の変更を自動 push するまでの時間
INACTIVE_PUSH_DELAY_MS = 5 * 60 * 1000


GWL_STYLE = -16
WS_VSCROLL = 0x00200000
# SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
SWP_FRAMECHANGED_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0020


class ThinScrollBar(wx.Window):
    """付箋の背景に溶け込む細いスクロールバー。

    素の EDIT のスクロールバーは灰色のトラックが付いて太く見えるので、
    EDIT 側のスクロールバーは消し、隣にこれを並べる。
    普段は細い線、ホバー・ドラッグ中は少し太くなる。
    """

    WIDTH = 10
    THIN, THICK = 3, 6
    MIN_THUMB = 16
    EM_GETFIRSTVISIBLELINE = 0x00CE
    EM_LINESCROLL = 0x00B6

    def __init__(self, parent: wx.Window, target: wx.TextCtrl):
        super().__init__(parent, size=(self.WIDTH, -1), style=wx.BORDER_NONE)
        self.target = target
        self._hide_native_scrollbar()
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((self.WIDTH, -1))
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
        thumb_h = max(h * visible // total, self.MIN_THUMB)
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
        w = self.THICK if active else self.THIN
        alpha = 150 if active else 100
        x = (self.WIDTH - w) // 2
        gc = wx.GraphicsContext.Create(dc)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, alpha)))
        gc.DrawRoundedRectangle(x, top + 2, w, thumb_h - 4, w / 2)

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


class NoteFrame(wx.Frame):
    # タスクバー・Alt+Tab に出すため wx.FRAME_TOOL_WINDOW / wx.FRAME_NO_TASKBAR は使わない
    # (後者は隠しオーナー付きで作られ、Alt+Tab に出なくなる)。
    BASE_STYLE = wx.CAPTION | wx.CLOSE_BOX | wx.RESIZE_BORDER
    ON_TOP_STYLE = wx.STAY_ON_TOP

    def __init__(self, app: "MemoApp", slug: str, rect: list[int] | None, on_top: bool = True):
        style = self.BASE_STYLE | (self.ON_TOP_STYLE if on_top else 0)
        super().__init__(None, title=slug, style=style)
        self.app = app
        self.slug = slug
        self.SetMinSize((120, 60))

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
        self._update_look()

        if rect:
            self.SetRect(wx.Rect(*rect))
        else:
            self.SetSize(DEFAULT_SIZE)
            self.SetPosition(app.next_cascade_pos())

        self.text.Bind(wx.EVT_TEXT, self._on_text)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self.Bind(wx.EVT_MOVE_END, lambda e: (app.remember_rect(self), e.Skip()))
        self.Bind(wx.EVT_SIZE, lambda e: (app.remember_rect(self), e.Skip()))

        ids = {name: wx.NewIdRef() for name in ("new", "close", "top", "delete", "sync")}
        self.Bind(wx.EVT_MENU, lambda e: app.new_note_dialog(), id=ids["new"])
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=ids["close"])
        self.Bind(wx.EVT_MENU, lambda e: self.toggle_on_top(), id=ids["top"])
        self.Bind(wx.EVT_MENU, lambda e: app.delete_note(self.slug), id=ids["delete"])
        self.Bind(wx.EVT_MENU, lambda e: self.sync_now(), id=ids["sync"])
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord("N"), ids["new"]),
            (wx.ACCEL_CTRL, ord("W"), ids["close"]),
            (wx.ACCEL_CTRL, ord("T"), ids["top"]),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_DELETE, ids["delete"]),
            (wx.ACCEL_CTRL, ord("S"), ids["sync"]),
        ]))

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
        title = mark + self.slug
        if self.GetTitle() != title:
            self.SetTitle(title)
        bg = CONFLICT_BG if conflict else NOTE_BG
        if self.text.GetBackgroundColour() != bg:
            self.text.SetBackgroundColour(bg)
            self.text.Refresh()
            self.scrollbar.SetBackgroundColour(bg)
            self.scrollbar.Refresh()

    @property
    def on_top(self) -> bool:
        return bool(self.GetWindowStyleFlag() & wx.STAY_ON_TOP)

    def toggle_on_top(self) -> None:
        self.set_on_top(not self.on_top)

    def set_on_top(self, on_top: bool) -> None:
        style = self.GetWindowStyleFlag()
        style = style | self.ON_TOP_STYLE if on_top else style & ~self.ON_TOP_STYLE
        self.SetWindowStyleFlag(style)
        self.app.remember_on_top(self.slug, on_top)

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
        self._cascade = 0
        self._config_timer = wx.CallLater(1000, self.config.save)
        self._config_timer.Stop()

        engine = SyncEngine(self.store, self.http, self._on_local_change_threaded)
        self.engine = engine
        self.sync = BackgroundSync(engine, interval=self._interval, on_status=self._on_status_threaded)
        self.tray = MemoTaskBarIcon(self)

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
    def next_cascade_pos(self) -> wx.Point:
        area = wx.Display(0).GetClientArea()
        self._cascade = (self._cascade + 1) % 10
        off = 24 * self._cascade
        return wx.Point(area.right - DEFAULT_SIZE[0] - 40 - off, area.top + 40 + off)

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

    def new_note_dialog(self) -> None:
        prefix = datetime.date.today().strftime("%y%m%d") + "-"
        dlg = wx.TextEntryDialog(None, "slug (英小文字・数字・ハイフン)", "新規メモ", prefix)
        text = next((c for c in dlg.GetChildren() if isinstance(c, wx.TextCtrl)), None)
        if text:
            # slug は ASCII のみなので IME を切り離す (半角のまま打てるように)
            imm = ctypes.windll.imm32
            imm.ImmAssociateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            imm.ImmAssociateContext.restype = ctypes.c_void_p
            imm.ImmAssociateContext(text.GetHandle(), None)
        try:
            while True:
                # 既定では全選択されるので、表示後にカーソルを末尾へ移す
                if text:
                    wx.CallAfter(text.SetInsertionPointEnd)
                if dlg.ShowModal() != wx.ID_OK:
                    break
                slug = dlg.GetValue().strip().lower()
                try:
                    validate_slug(slug)
                except InvalidSlug:
                    wx.MessageBox("slug は a-z, 0-9, '-' の 1〜64 文字です。", "memo")
                    continue
                self.open_note(slug, create=True)
                return
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
        if slug in self.config.data["not_on_top"]:
            self.config.data["not_on_top"].remove(slug)
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
        self.tray.RemoveIcon()
        self.tray.Destroy()
        self.ExitMainLoop()
