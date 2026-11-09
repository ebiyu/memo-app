"""プロセスの DPI awareness 設定。

wx (というか Win32 ウィンドウ) を作る前に呼ぶ必要がある。設定しないと
125% 等の拡大環境で OS がウィンドウごとビットマップ拡大するため、
文字がぼけて汚くなる。
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable_dpi_awareness() -> None:
    """新しい API から順に試す。どれかが通れば OK (多重設定はエラーになる)。"""
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return
    except Exception:
        pass
    log.warning("failed to enable DPI awareness; text may look blurry on scaled displays")


def scale_factor() -> float:
    """システム (プライマリモニタ) の拡大率。96 DPI = 1.0。

    DPI awareness を設定した後に呼ぶこと。非対応プロセスでは常に 1.0 が返る。
    """
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        log.exception("failed to query system DPI")
        return 1.0
