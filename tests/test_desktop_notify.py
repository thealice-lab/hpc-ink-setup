"""Notifications for watched jobs: which changes count, the text, the stored
choice and the Windows shell calls (with a stand-in for ctypes.windll)."""

import ctypes
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tensor_desktop_notify as notify  # noqa: E402


@pytest.mark.parametrize(
    "previous, current, expected",
    [
        ("RUNNING", "RETURNED", "returned"),
        ("COMPLETED", "RETURNED", "returned"),
        ("PENDING", "FAILED", "failed"),
        ("RUNNING", "OUT_OF_MEMORY", "failed"),
        ("RUNNING", "TIMEOUT", "failed"),
        ("SUBMITTED", "NODE_FAIL", "failed"),
        # Not a finish the researcher needs telling about.
        ("RUNNING", "COMPLETED", None),
        ("PENDING", "RUNNING", None),
        ("PENDING", "CANCELLED by 1000", None),
        ("PREPARED", "SUBMITTED", None),
        # Shown again in a state it already had, or loaded fresh.
        ("RETURNED", "RETURNED", None),
        ("FAILED", "FAILED", None),
        ("CANCELLED by 1000", "FAILED", None),
        (None, "RETURNED", None),
        (None, "FAILED", None),
    ],
)
def test_only_a_watched_job_finishing_counts(previous, current, expected):
    assert notify.outcome(previous, current) == expected


def test_message_summarises_one_check():
    title, text = notify.message(["a.gjf", "b.gjf"], [], watching=1)
    assert title == "Inkly: 2 returned"
    assert text == "Returned: a.gjf, b.gjf\nStill watching 1 job(s)."
    title, text = notify.message(["a.gjf"], [("c.gjf", "OUT_OF_MEMORY")], watching=0)
    assert title == "Inkly: 1 returned and 1 ended without a result"
    assert text.splitlines() == [
        "Returned: a.gjf",
        "No result: c.gjf (OUT_OF_MEMORY)",
        "All watched jobs are done.",
    ]


def test_message_fits_the_windows_limits():
    names = [f"{'long_name_' * 6}{number}.gjf" for number in range(9)]
    title, text = notify.message(names, [(name, "FAILED") for name in names], 400)
    assert len(title) <= notify.TITLE_LIMIT and len(text) <= notify.TEXT_LIMIT
    assert "and 6 more" in text or text.endswith("…")


def test_preference_defaults_on_and_keeps_other_values(tmp_path):
    assert notify.load_preference(tmp_path) is True
    path = tmp_path / notify.PREFERENCES
    path.write_text("not json", encoding="utf-8")
    assert notify.load_preference(tmp_path) is True
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    notify.save_preference(tmp_path, False)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "other": 1,
        "notify": False,
    }
    assert notify.load_preference(tmp_path) is False
    notify.save_preference(tmp_path, True)
    assert notify.load_preference(tmp_path) is True
    assert sorted(p.name for p in tmp_path.iterdir()) == [notify.PREFERENCES]


class FakeFunction:
    def __init__(self, calls, name, result):
        self.calls, self.name, self.result = calls, name, result

    def __call__(self, *args):
        recorded = []
        for arg in args:
            # ctypes.byref() wraps a structure; copy what was passed.
            obj = getattr(arg, "_obj", arg)
            if isinstance(obj, ctypes.Structure):
                recorded.append({name: getattr(obj, name) for name, _ in obj._fields_})
            else:
                recorded.append(obj)
        self.calls.append((self.name, recorded))
        return self.result(*args) if callable(self.result) else self.result


def fake_windll(foreground=0):
    calls = []

    def library(**functions):
        return SimpleNamespace(
            **{
                name: FakeFunction(calls, name, result)
                for name, result in functions.items()
            }
        )

    windll = SimpleNamespace(
        shell32=library(Shell_NotifyIconW=1, ExtractIconW=1),
        user32=library(GetForegroundWindow=foreground, LoadIconW=0x77, FlashWindowEx=1),
        kernel32=library(GetModuleHandleW=0x400000),
    )
    return windll, calls


ROOT = SimpleNamespace(wm_frame=lambda: "0x1234")


def test_notifier_does_nothing_off_windows():
    windll, calls = fake_windll()
    notifier = notify.Notifier(ROOT, windll=windll, platform="linux")
    assert notifier.foreground() is True
    assert notifier.show("title", "text") is False
    notifier.close()
    assert calls == []


def test_notifier_adds_one_icon_shows_each_message_flashes_and_removes():
    windll, calls = fake_windll(foreground=0x9999)
    notifier = notify.Notifier(ROOT, windll=windll, platform="win32")
    assert notifier.foreground() is False
    assert notifier.show("Inkly: 1 returned", "Returned: a.gjf") is True
    assert notifier.show("Inkly: 1 ended", "No result: b.gjf", warning=True) is True
    shell = [args for name, args in calls if name == "Shell_NotifyIconW"]
    assert [operation for operation, _ in shell] == [
        notify.NIM_ADD,
        notify.NIM_MODIFY,
        notify.NIM_MODIFY,
    ]
    added, first, second = (data for _, data in shell)
    assert added["hWnd"] == 0x1234 and added["uID"] == notify.ICON_ID
    assert added["uFlags"] == notify.NIF_ICON | notify.NIF_TIP
    # ExtractIconW answered 1 (not an executable): the stock icon is used.
    assert added["hIcon"] == 0x77 and added["szTip"] == "Inkly Desktop"
    assert [args for name, args in calls if name == "ExtractIconW"] == [
        [0x400000, sys.executable, 0]
    ]
    assert first["uFlags"] == notify.NIF_INFO
    assert (first["szInfoTitle"], first["szInfo"]) == (
        "Inkly: 1 returned",
        "Returned: a.gjf",
    )
    assert first["dwInfoFlags"] == notify.NIIF_INFO | notify.NIIF_RESPECT_QUIET_TIME
    assert second["dwInfoFlags"] & notify.NIIF_WARNING
    flashes = [args[0] for name, args in calls if name == "FlashWindowEx"]
    assert len(flashes) == 2 and flashes[0]["hwnd"] == 0x1234
    assert flashes[0]["dwFlags"] == notify.FLASHW_TRAY | notify.FLASHW_TIMERNOFG
    notifier.close()
    assert calls[-1][0] == "Shell_NotifyIconW"
    assert calls[-1][1][0] == notify.NIM_DELETE
    notifier.close()  # Nothing left to remove.
    assert sum(1 for name, _ in calls if name == "Shell_NotifyIconW") == 4


def test_notifier_in_front_and_failures_never_raise():
    windll, calls = fake_windll(foreground=0x1234)
    notifier = notify.Notifier(ROOT, windll=windll, platform="win32")
    assert notifier.foreground() is True

    def broken(*args):
        raise OSError("shell unavailable")

    windll.shell32.Shell_NotifyIconW = broken
    assert notifier.show("title", "text") is False
    gone = SimpleNamespace(
        wm_frame=lambda: (_ for _ in ()).throw(RuntimeError("closed"))
    )
    assert notify.Notifier(gone, windll=windll, platform="win32").foreground()


@pytest.mark.skipif(
    sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8,
    reason="64-bit Windows layout",
)
def test_structures_match_the_64_bit_windows_layout():
    assert ctypes.sizeof(notify.NOTIFYICONDATAW) == 976
    assert ctypes.sizeof(notify.FLASHWINFO) == 32
