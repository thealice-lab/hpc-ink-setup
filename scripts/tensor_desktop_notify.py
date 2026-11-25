"""Windows notifications for watched jobs that return or end without a result.

Desktop shows one notification per check, only while it watches jobs and only
when its window is not in front. The taskbar button also flashes until the
window is brought forward. Everything uses the Windows shell through ctypes, so
there is nothing extra to install; elsewhere the notifier does nothing.
"""

import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile

# Ended without a result, from Slurm's point of view. Cancelled jobs are left
# out: Desktop cancels only when the researcher asks.
FAILED_STATES = {
    "FAILED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
}
DONE_STATES = FAILED_STATES | {"RETURNED", "CANCELLED"}

TITLE_LIMIT = 63  # szInfoTitle holds 64 wide characters with the terminator.
TEXT_LIMIT = 255  # szInfo holds 256.
PREFERENCES = "desktop-preferences.json"


def state_name(state):
    """'CANCELLED by 1000' -> 'CANCELLED'; '' for a missing state."""
    return str(state or "").split(" ", 1)[0].upper()


def outcome(previous, current):
    """'returned', 'failed' or None for one watched row's state change.

    Only a change from an unfinished state counts, so rows that are loaded,
    previewed or shown again in an ended state never notify.
    """
    if previous is None:
        return None
    before, after = state_name(previous), state_name(current)
    if before in DONE_STATES or before == after:
        return None
    if after == "RETURNED":
        return "returned"
    if after in FAILED_STATES:
        return "failed"
    return None


def _names(names, limit=3):
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def message(returned, failed, watching):
    """Title and text for the jobs that finished in one check.

    ``returned`` lists file names; ``failed`` lists (file name, state) pairs;
    ``watching`` counts the jobs still being watched.
    """
    parts = []
    if returned:
        parts.append(f"{len(returned)} returned")
    if failed:
        parts.append(f"{len(failed)} ended without a result")
    title = "Inkly: " + " and ".join(parts) if parts else "Inkly Desktop"
    lines = []
    if returned:
        lines.append("Returned: " + _names(returned))
    if failed:
        lines.append(
            "No result: " + _names([f"{name} ({state})" for name, state in failed])
        )
    lines.append(
        f"Still watching {watching} job(s)."
        if watching
        else "All watched jobs are done."
    )
    text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"
    return title[:TITLE_LIMIT], text


def load_preference(directory):
    """Whether notifications are on (the default) in Desktop's preferences."""
    try:
        data = json.loads((Path(directory) / PREFERENCES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return data.get("notify", True) is not False if isinstance(data, dict) else True


def save_preference(directory, enabled):
    """Store the choice atomically; other preferences in the file are kept."""
    path = Path(directory) / PREFERENCES
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["notify"] = bool(enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".desktop-preferences-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """shellapi.h, Windows Vista and later (976 bytes on 64-bit Windows)."""

    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint32),
        ("uFlags", ctypes.c_uint32),
        ("uCallbackMessage", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint32),
        ("dwStateMask", ctypes.c_uint32),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint32),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint32),
        ("guidItem", GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
        ("uCount", ctypes.c_uint32),
        ("dwTimeout", ctypes.c_uint32),
    ]


NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_ICON, NIF_TIP, NIF_INFO = 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_RESPECT_QUIET_TIME = 0x1, 0x2, 0x80
FLASHW_TRAY, FLASHW_TIMERNOFG = 0x2, 0xC
IDI_INFORMATION = 32516
ICON_ID = 1


class Notifier:
    """Shows Windows notifications for one Desktop window; no-op elsewhere."""

    def __init__(self, root, windll=None, platform=None):
        self.root = root
        self.windows = (platform or sys.platform) == "win32"
        self.windll = windll if windll is not None else getattr(ctypes, "windll", None)
        self.icon = None
        self.added = False

    def hwnd(self):
        """The Tk window's frame, which Windows treats as the application window."""
        return int(self.root.wm_frame(), 16)

    def foreground(self):
        """True when Desktop is the window in front (always True off Windows)."""
        if not self.windows:
            return True
        try:
            user32 = self.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            return user32.GetForegroundWindow() == self.hwnd()
        except Exception:
            return True  # When unsure, stay quiet.

    def data(self):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd()
        data.uID = ICON_ID
        return data

    def shell(self, operation, data):
        shell32 = self.windll.shell32
        shell32.Shell_NotifyIconW.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(NOTIFYICONDATAW),
        ]
        return bool(shell32.Shell_NotifyIconW(operation, ctypes.byref(data)))

    def app_icon(self):
        if self.icon is None:
            shell32, user32 = self.windll.shell32, self.windll.user32
            shell32.ExtractIconW.restype = ctypes.c_void_p
            shell32.ExtractIconW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
            ]
            user32.LoadIconW.restype = ctypes.c_void_p
            user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32 = self.windll.kernel32
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            icon = shell32.ExtractIconW(
                kernel32.GetModuleHandleW(None), sys.executable, 0
            )
            # ExtractIconW returns 1 for a file that is not an executable.
            self.icon = (
                icon
                if icon and icon != 1
                else user32.LoadIconW(None, ctypes.c_void_p(IDI_INFORMATION))
            )
        return self.icon

    def show(self, title, text, warning=False):
        """Notify and flash the taskbar button. Returns True if Windows took it."""
        if not self.windows or self.windll is None:
            return False
        try:
            if not self.added:
                data = self.data()
                data.uFlags = NIF_ICON | NIF_TIP
                data.hIcon = self.app_icon()
                data.szTip = "Inkly Desktop"
                self.added = self.shell(NIM_ADD, data)
            data = self.data()
            data.uFlags = NIF_INFO
            data.szInfoTitle = title[:TITLE_LIMIT]
            data.szInfo = text[:TEXT_LIMIT]
            data.dwInfoFlags = (
                NIIF_WARNING if warning else NIIF_INFO
            ) | NIIF_RESPECT_QUIET_TIME
            shown = self.shell(NIM_MODIFY, data)
            flash = FLASHWINFO()
            flash.cbSize = ctypes.sizeof(FLASHWINFO)
            flash.hwnd = self.hwnd()
            flash.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
            user32 = self.windll.user32
            user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
            user32.FlashWindowEx(ctypes.byref(flash))
            return shown
        except Exception:
            return False  # A notification must never stop the watch.

    def close(self):
        """Remove the notification-area icon when Desktop closes."""
        if not self.added:
            return
        try:
            self.shell(NIM_DELETE, self.data())
        except Exception:
            pass
        self.added = False
