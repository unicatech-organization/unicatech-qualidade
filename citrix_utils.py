"""Helpers to locate and focus the remote (Citrix) application window."""
import ctypes
import time

import pygetwindow as gw

try:
    import win32con
    import win32gui
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False


def _make_process_dpi_aware():
    """Without this, Windows scales window/cursor coordinates by the display's
    DPI setting for any process that hasn't opted into per-monitor DPI awareness
    -- so pygetwindow (Win32 GetWindowRect) and pyautogui.position() (GetCursorPos)
    report positions in SCALED ("logical") pixels, while pyautogui/PIL screenshots
    (used for all appearance matching) are always taken in real ("physical")
    pixels. On any display scaled above 100% (125%/150%/etc, extremely common),
    the two disagree on where things are -- every region computed from a window's
    reported bounds then points at the wrong part of the actual screenshot,
    producing exactly the kind of erratic, hard-to-explain detection failures
    this was chasing (false-positive matches near the screen origin, a region
    search consistently missing something an unrestricted search just found).
    Must run before any window/screen query -- this module is imported early
    enough by everything else (automation, calibration, auto_detect) that
    module-level execution here covers every entry point."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # fallback for older Windows
        except Exception:
            pass


_make_process_dpi_aware()


def find_window(title_contains: str):
    """Return the first window whose title contains the given substring (case-insensitive)."""
    title_contains = title_contains.lower()
    for w in gw.getAllWindows():
        if w.title and title_contains in w.title.lower():
            return w
    return None


def stable_title_anchor(full_title: str) -> str:
    """Reduce a window title to a short, stable substring for later matching.
    Titles like 'VIVO Qualidade - \\Remote' can vary in their suffix across
    sessions/reconnects, so we keep only the part before the first ' - '."""
    return full_title.split(" - ")[0].strip()


APP_TITLE_SAFETY_ANCHOR = "vivo qualidade"


def find_window_at_point(x: int, y: int, title_must_contain: str = APP_TITLE_SAFETY_ANCHOR):
    """Return the smallest visible window whose bounding box contains (x, y).

    More reliable than 'active window' for remote/Citrix apps, since it doesn't
    depend on focus-tracking timing -- it just looks at where the mouse is.

    `title_must_contain` is a safety net: image-based matching (see auto_detect.py)
    can occasionally false-positive on some unrelated window (a Windows dialog, another
    app) that happens to look similar enough. Requiring the title to actually mention
    the app -- unless the caller explicitly opts out with None -- turns a wrong-window
    false positive into a clean "not found" instead of clicking/typing into some
    random window on the desktop. Defaults to the app's own name so every call site
    is protected unless it deliberately passes title_must_contain=None.
    """
    best = None
    best_area = None
    for w in gw.getAllWindows():
        if not w.title.strip() or not w.visible:
            continue
        if w.width <= 0 or w.height <= 0:
            continue
        if title_must_contain and title_must_contain.lower() not in w.title.lower():
            continue
        if w.left <= x <= w.left + w.width and w.top <= y <= w.top + w.height:
            area = w.width * w.height
            if best_area is None or area < best_area:
                best = w
                best_area = area
    return best


def focus_window(win) -> bool:
    """Bring a window to the foreground WITHOUT changing its maximized/normal state.

    Never calls minimize+restore as a "trick" -- that resets a maximized window back
    to normal size, which silently invalidates every calibrated coordinate.
    """
    hwnd = getattr(win, "_hWnd", None)
    if _HAS_WIN32 and hwnd:
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            if win32gui.IsIconic(hwnd):  # only restore if truly minimized
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.2)
            return True
        except Exception:
            pass  # fall through to pygetwindow's own activate()

    try:
        win.activate()
        time.sleep(0.2)
        return True
    except Exception:
        return False


def absolute_point(window, offset_x: int, offset_y: int):
    """Translate a (window-relative) offset into current absolute screen coordinates."""
    return window.left + offset_x, window.top + offset_y


def absolute_region(window, rel, pad: int = 8):
    """Translate a window-relative region dict {x1,y1,x2,y2} into absolute coords,
    expanded by `pad` pixels on each side to tolerate tight/imprecise calibration clicks."""
    x1, y1 = absolute_point(window, rel["x1"] - pad, rel["y1"] - pad)
    x2, y2 = absolute_point(window, rel["x2"] + pad, rel["y2"] + pad)
    return x1, y1, x2, y2
