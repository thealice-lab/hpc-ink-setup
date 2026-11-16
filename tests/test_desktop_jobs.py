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


def handoff(folder, name, bundle):
    """An input with its Desktop receipt, as Desktop leaves them."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("input")
    jobs.receipt_for(path).write_text(json.dumps({"bundle": bundle}))
    return path


def recorded(name, bundle):
    return {"name": name, "bundle": bundle}


def test_remembered_folders_include_inputs_and_added_folders(tmp_path, monkeypatch):
    # A list written by 0.5.0 has no saved folders: its inputs' folders count.
    water = handoff(tmp_path / "a", "water.gjf", "a" * 32)
    jobs.state_path().parent.mkdir(parents=True)
    jobs.state_path().write_text(
        json.dumps(
            {
                "version": 1,
                "folder": None,
                "handoffs": {"a" * 32: {"input": str(water), "seen": "2026-11-15"}},
            }
        )
    )
    index = jobs.Index()
    assert index.folders() == [tmp_path / "a"]
    monkeypatch.setattr(jobs, "MAX_FOLDERS", 3)
    index.remember_folders([tmp_path / "b", tmp_path / "c"])
    index.remember_folders([tmp_path / "d"])
    saved = json.loads(jobs.state_path().read_text())
    assert len(saved["folders"]) == 3  # Bounded, newest kept.
    assert index.folders()[0] == (tmp_path / "d").resolve()
    assert len(index.folders()) == 3


def test_jobs_are_found_beside_their_receipts_in_remembered_folders(tmp_path):
    index = jobs.Index()
    water = handoff(tmp_path / "project", "water.gjf", "a" * 32)
    handoff(tmp_path / "project", "ethanol.gjf", "c" * 32)  # A newer job
    (tmp_path / "project" / "lost.gjf.inkly.json").write_text(
        json.dumps({"bundle": "d" * 32})
    )  # Receipt without its input
    found, missing = jobs.find_in_folders(
        [
            recorded("water.gjf", "a" * 32),
            recorded("ethanol.gjf", "b" * 32),
            recorded("lost.gjf", "d" * 32),
            recorded("../water.gjf", "a" * 32),
        ],
        [tmp_path / "elsewhere", tmp_path / "project"],
        index,
    )
    assert found == [water]
    assert [job["name"] for job in missing] == [
        "ethanol.gjf",
        "lost.gjf",
        "../water.gjf",
    ]
    assert index.locate("a" * 32) == ("found", water.resolve())


def test_search_looks_through_subfolders_within_its_bounds(tmp_path, monkeypatch):
    index = jobs.Index()
    root = tmp_path / "research"
    deep = handoff(root / "2026" / "november" / "batch", "water.gjf", "a" * 32)
    handoff(root / ".hidden", "ethanol.gjf", "b" * 32)
    handoff(root / "node_modules", "methanol.gjf", "c" * 32)
    wanted = [
        recorded("water.gjf", "a" * 32),
        recorded("ethanol.gjf", "b" * 32),
        recorded("methanol.gjf", "c" * 32),
    ]
    found, missing, complete = jobs.search(root, wanted, index)
    assert found == [deep] and complete
    assert [job["name"] for job in missing] == ["ethanol.gjf", "methanol.gjf"]
    # Next time the folder is checked without searching.
    assert deep.parent.resolve() in index.folders()
    monkeypatch.setattr(jobs, "SEARCH_DEPTH", 2)
    found, missing, complete = jobs.search(root, wanted[:1], jobs.Index())
    assert found == [] and not complete  # Stopped at the depth limit.
    monkeypatch.setattr(jobs, "SEARCH_DEPTH", 8)
    monkeypatch.setattr(jobs, "SEARCH_ENTRIES", 2)
    found, missing, complete = jobs.search(root, wanted[:1], jobs.Index())
    assert found == [] and not complete  # Stopped at the entry limit.


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks need privileges")
def test_search_never_follows_links_or_reads_other_files(tmp_path):
    outside = handoff(tmp_path / "outside", "water.gjf", "a" * 32)
    root = tmp_path / "research"
    root.mkdir()
    (root / "link").symlink_to(outside.parent, target_is_directory=True)
    (root / "water.gjf.inkly.json").symlink_to(jobs.receipt_for(outside))
    found, missing, complete = jobs.search(
        root, [recorded("water.gjf", "a" * 32)], jobs.Index()
    )
    assert found == [] and len(missing) == 1


def test_search_can_be_stopped(tmp_path):
    handoff(tmp_path / "research" / "a", "water.gjf", "a" * 32)

    def stop():
        raise InterruptedError("stopped")

    with pytest.raises(InterruptedError):
        jobs.search(
            tmp_path / "research", [recorded("water.gjf", "a" * 32)], jobs.Index(), stop
        )
