"""Which recorded jobs Load my jobs adds, Job history filters and the file list."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tensor_desktop_jobs as jobs  # noqa: E402

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def job(state, days=None, ended=None, submission="SUBMITTED", name="a.gjf", job_id="1"):
    submitted = None if days is None else (NOW - timedelta(days=days)).isoformat()
    return {
        "bundle": "a" * 32,
        "name": name,
        "job_id": job_id,
        "submission": submission,
        "state": state,
        "prepared_at": submitted or (NOW - timedelta(days=99)).isoformat(),
        "submitted_at": submitted,
        "ended_at": None
        if ended is None
        else (NOW - timedelta(days=ended)).isoformat(),
    }


@pytest.fixture(autouse=True)
def local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def test_load_set_is_active_jobs_and_jobs_that_ended_recently():
    listing = [
        job("PENDING", days=40),  # Active, however old
        job("RUNNING", days=5),
        job("COMPLETED", days=4, ended=2),
        job("CANCELLED by 7", days=5, ended=1),
        job("COMPLETED", days=10, ended=9),  # Ended too long ago
        job("FAILED", days=5, ended=4),
        job("PREPARED", days=1, submission="PREPARED", job_id=""),  # Never submitted
        job("SUBMISSION_UNKNOWN", days=60, submission="SUBMISSION_UNKNOWN"),
        job("SUBMISSION_UNKNOWN", days=1, submission="SUBMISSION_UNKNOWN"),
        job("UNKNOWN", days=2),  # Accounting unavailable, submitted recently
        job("UNKNOWN", days=20),
    ]
    chosen = jobs.load_set(listing, NOW)
    assert [item["state"] for item in chosen] == [
        "PENDING",
        "RUNNING",
        "COMPLETED",
        "CANCELLED by 7",
        "SUBMISSION_UNKNOWN",
        "UNKNOWN",
    ]
    assert len(jobs.load_set(listing, NOW, days=30)) == 9  # Not the 60-day-old one


def test_history_filters_by_state_period_and_name_or_job_id():
    listing = [
        job("COMPLETED", days=1, name="water.gjf", job_id="41"),
        job("CANCELLED by 7", days=3, name="ethanol.gjf", job_id="42"),
        job("TIMEOUT", days=20, name="methanol.gjf", job_id="43"),
        job("PENDING", days=2, name="acetone.gjf", job_id="44"),
        job("PREPARED", days=1, submission="PREPARED", name="draft.gjf", job_id=""),
    ]

    def names(*args):
        return [item["name"] for item in jobs.history_rows(listing, *args, now=NOW)]

    assert names("Finished and cancelled", "All time") == [
        "water.gjf",
        "ethanol.gjf",
        "methanol.gjf",
    ]
    assert names("Finished and cancelled", "Last 7 days") == [
        "water.gjf",
        "ethanol.gjf",
    ]
    assert names("Cancelled", "All time") == ["ethanol.gjf"]
    assert names("Failed, timed out or out of memory", "All time") == ["methanol.gjf"]
    assert names("Pending or running", "All time") == ["acetone.gjf"]
    assert names("Not submitted", "All time") == ["draft.gjf"]
    assert names("All", "All time", "ACET") == ["acetone.gjf"]
    assert names("All", "All time", "43") == ["methanol.gjf"]
    assert names("All", "All time", "4") == []  # Job IDs match whole, names in part.


def test_index_finds_moves_and_replacements(tmp_path):
    index = jobs.Index()
    path = tmp_path / "water.gjf"
    path.write_text("input")
    receipt = jobs.receipt_for(path)
    receipt.write_text(json.dumps({"bundle": "a" * 32}))
    index.remember(path, "a" * 32)
    assert index.locate("a" * 32) == ("found", path.resolve())
    assert index.locate("b" * 32) == (None, None)
    # Run again wrote a new receipt for the same file.
    receipt.write_text(json.dumps({"bundle": "c" * 32}))
    assert index.locate("a" * 32) == ("replaced", path.resolve())
    receipt.write_text(json.dumps({"bundle": "a" * 32}))
    index.replaced("a" * 32)
    assert index.locate("a" * 32) == ("replaced", path.resolve())
    # A moved or deleted file is no longer on this computer.
    index.remember(path, "d" * 32)
    receipt.unlink()
    assert index.locate("d" * 32) == (None, None)
    index.remember(path, "not-a-bundle")
    assert "not-a-bundle" not in jobs.state_path().read_text()


def test_index_keeps_the_folder_and_a_bounded_list(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_REMEMBERED", 3)
    index = jobs.Index()
    assert index.folder() is None
    index.set_folder(tmp_path / "copies")
    for letter in "abcde":
        index.remember(tmp_path / f"{letter}.gjf", letter * 32)
    saved = json.loads(jobs.state_path().read_text())
    assert sorted(saved["handoffs"]) == ["c" * 32, "d" * 32, "e" * 32]
    assert index.folder() == (tmp_path / "copies").resolve()
    jobs.state_path().write_text("not JSON")
    assert index.folder() is None and index.locate("e" * 32) == (None, None)


def test_copies_get_one_folder_per_job_and_safe_names(tmp_path):
    first = jobs.copy_path(tmp_path, {"name": "water.gjf", "bundle": "a" * 32})
    second = jobs.copy_path(tmp_path, {"name": "water.gjf", "bundle": "b" * 32})
    assert first == tmp_path / "water-aaaaaaaa" / "water.gjf" and first != second
    for name in ("../water.gjf", "C:\\water.gjf", "water.txt", ""):
        with pytest.raises(ValueError, match="unsafe"):
            jobs.copy_path(tmp_path, {"name": name, "bundle": "a" * 32})
    with pytest.raises(ValueError, match="unsafe"):
        jobs.copy_path(tmp_path, {"name": "water.gjf", "bundle": "../x"})


def test_local_time_shows_utc_times_on_this_computer():
    shown = jobs.local_time("2026-11-15T15:00:00+00:00")
    expected = (
        datetime(2026, 11, 15, 15, tzinfo=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )
    assert shown == expected
    assert jobs.local_time(None) == "" and jobs.local_time("Unknown") == ""
