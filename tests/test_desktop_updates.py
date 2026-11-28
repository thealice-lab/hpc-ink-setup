"""Release notices and their cache: no real network requests."""

import io
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tensor_desktop_notify as notify  # noqa: E402
import tensor_desktop_preferences as preferences  # noqa: E402
import tensor_desktop_updates as updates  # noqa: E402


def release(tag, **fields):
    return {"tag_name": tag, "draft": False, **fields}


def test_newest_uses_numeric_desktop_versions_and_includes_previews():
    latest = updates.newest(
        [
            release("desktop-v0.9.2"),
            release(
                "desktop-v0.10.0", prerelease=True, html_url="https://elsewhere.example"
            ),
            release("desktop-v999.0.0", draft=True),
            release("v10.0.0"),
            release("desktop-v0.11.0/other"),
            release("desktop-v0.11.0-rc1"),
            {"tag_name": "desktop-v42.0.0"},
            None,
        ]
    )
    assert latest == {
        "tag": "desktop-v0.10.0",
        "version": "0.10.0",
        "url": f"{updates.RELEASES_URL}/tag/desktop-v0.10.0",
    }
    assert updates.newer(latest, "0.9.2") == latest
    assert updates.newer(latest, "0.10.0") is None
    assert updates.newer(latest, "0.11.0") is None
    assert updates.newer(latest, "unknown") is None
    assert updates.newest([release("v1.0.0")]) is None
    with pytest.raises(ValueError):
        updates.newest({"message": "API rate limit exceeded"})


def test_fetch_requests_the_expected_public_api(monkeypatch):
    calls = []

    def open_response(req, timeout):
        calls.append((req.full_url, timeout, req.get_header("User-agent")))
        assert req.get_header("Accept") == "application/vnd.github+json"
        return io.BytesIO(json.dumps([release("desktop-v1.2.3")]).encode())

    monkeypatch.setattr(updates.request, "urlopen", open_response)
    assert updates.fetch_latest()["version"] == "1.2.3"
    assert calls == [
        (
            updates.API_URL,
            updates.TIMEOUT_SECONDS,
            f"InklyDesktop/{updates.__version__}",
        )
    ]


def test_fetch_refuses_oversized_or_invalid_responses(monkeypatch):
    monkeypatch.setattr(updates, "MAX_BYTES", 16)
    monkeypatch.setattr(
        updates.request, "urlopen", lambda *a, **k: io.BytesIO(b"x" * 17)
    )
    with pytest.raises(ValueError, match="too large"):
        updates.fetch_latest()
    monkeypatch.setattr(
        updates.request, "urlopen", lambda *a, **k: io.BytesIO(b"not json")
    )
    with pytest.raises(ValueError):
        updates.fetch_latest()


@pytest.fixture
def online(monkeypatch):
    monkeypatch.delenv("INKLY_DESKTOP_UPDATE_CHECK", raising=False)
    clock = {"now": 1_000_000}
    calls = []
    monkeypatch.setattr(updates.time, "time", lambda: clock["now"])

    def fetch():
        calls.append(clock["now"])
        return updates.release_info("desktop-v0.10.0")

    monkeypatch.setattr(updates, "fetch_latest", fetch)
    return clock, calls


def test_cache_is_used_for_a_day_and_ignores_a_tampered_url(tmp_path, online):
    clock, calls = online
    latest = updates.check(tmp_path, "0.9.0")
    path = tmp_path / updates.CACHE
    cache = json.loads(path.read_text())
    assert cache == {"checked_at": clock["now"], "latest": "desktop-v0.10.0"}
    cache["url"] = "https://elsewhere.example"
    path.write_text(json.dumps(cache), encoding="utf-8")
    clock["now"] += 3600
    assert updates.check(tmp_path, "0.9.0") == latest
    assert updates.check(tmp_path, "0.10.0") is None
    assert len(calls) == 1
    clock["now"] += updates.INTERVAL_SECONDS
    assert updates.check(tmp_path, "0.9.0") == latest
    assert len(calls) == 2


def test_clock_moving_back_and_invalid_cache_trigger_a_check(tmp_path, online):
    clock, calls = online
    path = tmp_path / updates.CACHE
    path.write_text("not JSON", encoding="utf-8")
    assert updates.check(tmp_path, "0.9.0")
    clock["now"] -= 10
    assert updates.check(tmp_path, "0.9.0")
    assert len(calls) == 2


def test_offline_stays_silent_and_keeps_last_confirmed_answer(
    tmp_path, online, monkeypatch
):
    clock, calls = online
    latest = updates.check(tmp_path, "0.9.0")
    clock["now"] += updates.INTERVAL_SECONDS

    def offline():
        calls.append(clock["now"])
        raise OSError("network offline")

    monkeypatch.setattr(updates, "fetch_latest", offline)
    assert updates.check(tmp_path, "0.9.0") == latest
    assert updates.check(tmp_path, "0.9.0") == latest
    assert len(calls) == 2
    empty = tmp_path / "offline"
    assert updates.check(empty, "0.9.0") is None
    assert updates.check(empty, "0.9.0") is None
    assert len(calls) == 3


def test_preference_defaults_on_and_survives_notification_changes(
    tmp_path, online, monkeypatch
):
    _, calls = online
    assert preferences.load(tmp_path, updates.PREFERENCE)
    notify.save_preference(tmp_path, False)
    preferences.save(tmp_path, updates.PREFERENCE, False)
    assert updates.check(tmp_path, "0.9.0") is None
    assert not calls and not (tmp_path / updates.CACHE).exists()
    notify.save_preference(tmp_path, True)
    assert not preferences.load(tmp_path, updates.PREFERENCE)
    preferences.save(tmp_path, updates.PREFERENCE, True)
    assert notify.load_preference(tmp_path)
    monkeypatch.setenv("INKLY_DESKTOP_UPDATE_CHECK", "0")
    assert updates.check(tmp_path, "0.9.0") is None
    assert not calls
    monkeypatch.delenv("INKLY_DESKTOP_UPDATE_CHECK")
    assert updates.check(tmp_path, "0.9.0")
    assert len(calls) == 1
