"""Recorded Inkly handoffs and their Slurm states, without local input files."""

import base64
import hashlib
import json
import os
import subprocess
import time

import pytest

from inkly.gaussian import job_list, tensor

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Owned HPC receipts")
DAY = 24 * 3600


def handoff(root, bundle, job=None, submission="SUBMITTED", name="sample.gjf", age=0):
    directory = root / bundle
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    (directory / name).write_bytes(b"%mem=1GB\n# hf/sto-3g\n\nx\n\n0 1\nH 0 0 0\n\n")
    record = {"bundle": bundle, "name": name, "submission": submission}
    if job:
        record["job_id"] = job
    (directory / "receipt.json").write_text(json.dumps(record))
    moment = time.time() - age
    os.utime(directory / name, (moment, moment))
    return directory


def scheduler_for(accounting="", queue="", calls=None):
    def scheduler(args, directory):
        if calls is not None:
            calls.append(args)
        text = accounting if args[0] == "sacct" else queue
        return subprocess.CompletedProcess(args, 0, text, "")

    return scheduler


def snapshot(root):
    return {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_lists_only_recorded_jobs_for_this_account_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    root = tmp_path / "handoffs"
    handoff(root, "a" * 32, "42", age=3 * DAY)
    handoff(root, "b" * 32, None, "SUBMISSION_UNKNOWN", age=2 * DAY)
    handoff(root, "c" * 32, "99", age=1 * DAY)
    handoff(root, "d" * 32, None, "PREPARED", age=10)
    before = snapshot(root)
    calls = []
    response = job_list.list_jobs(
        scheduler_for(
            "42|COMPLETED|0:0|2026-11-12T10:00:00|2026-11-12T10:05:00|00:05:00\n"
            "500|COMPLETED|0:0|2026-11-12T10:00:00|2026-11-12T10:05:00|00:01:00\n",
            "777|RUNNING|None|2026-11-15T09:00:00|1:00\n",
            calls,
        ),
        tmp_path,
        root,
    )
    states = {row["bundle"][0]: row["state"] for row in response["jobs"]}
    assert states == {
        "a": "COMPLETED",
        "b": "SUBMISSION_UNKNOWN",
        "c": "UNKNOWN",  # Accounting has no record; not proof it never ran.
        "d": "PREPARED",
    }
    # Newest first: by Slurm submit time, else by when the handoff began.
    assert [row["bundle"][0] for row in response["jobs"]] == ["d", "c", "b", "a"]
    # Other jobs on the account (500 in accounting, 777 in the queue) never appear.
    assert {row["job_id"] for row in response["jobs"]} == {"42", "99", ""}
    sacct, squeue = calls
    uid = str(os.stat(tmp_path).st_uid)
    assert sacct[:3] == ["sacct", "--uid", uid] and sacct[-2:] == ["--jobs", "42,99"]
    assert squeue[:4] == ["squeue", "--noheader", "--user", uid]
    assert "--jobs" not in squeue  # One finished ID would make squeue fail.
    assert not response["unavailable"] and not response["truncated"]
    assert before == snapshot(root)


def test_old_jobs_keep_their_state_beyond_a_week(tmp_path, monkeypatch):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    root = tmp_path / "handoffs"
    handoff(root, "a" * 32, "42", age=40 * DAY)
    calls = []
    response = job_list.list_jobs(
        scheduler_for("42|CANCELLED by 1|0:0|Unknown|Unknown|00:00:00\n", "", calls),
        tmp_path,
        root,
    )
    assert response["jobs"][0]["state"] == "CANCELLED by 1"
    since = calls[0][calls[0].index("--starttime") + 1]
    oldest = time.strftime("%Y-%m-%d", time.localtime(time.time() - 41 * DAY))
    assert since <= oldest  # From before the oldest recorded handoff.


def test_queue_is_current_for_live_jobs_but_never_overrides_a_final_state(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    root = tmp_path / "handoffs"
    handoff(root, "a" * 32, "41")
    handoff(root, "b" * 32, "42")
    handoff(root, "c" * 32, "43")
    response = job_list.list_jobs(
        scheduler_for(
            "41|PENDING|0:0|2026-11-15T10:00:00|Unknown|00:00:00\n"
            "42|COMPLETED|0:0|2026-11-15T10:00:00|2026-11-15T10:09:00|00:09:00\n",
            "41|PENDING|Priority|2026-11-16T08:00:00|0:00\n"
            "42|RUNNING|None|2026-11-15T10:01:00|8:00\n"
            "43|RUNNING|None|2026-11-15T10:01:00|3:00\n",
        ),
        tmp_path,
        root,
    )
    rows = {row["job_id"]: row for row in response["jobs"]}
    assert rows["41"]["state"] == "PENDING" and rows["41"]["reason"] == "Priority"
    assert rows["41"]["start_at"]  # Slurm's estimate, as UTC
    assert rows["42"]["state"] == "COMPLETED" and rows["42"]["ended_at"]
    assert rows["43"]["state"] == "RUNNING"  # Not in accounting yet


def test_slurm_times_are_returned_in_utc(tmp_path, monkeypatch):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        root = tmp_path / "handoffs"
        handoff(root, "a" * 32, "42")
        response = job_list.list_jobs(
            scheduler_for(
                "42|COMPLETED|0:0|2026-11-15T10:00:00|2026-11-15T10:30:00|00:30:00\n"
            ),
            tmp_path,
            root,
        )
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    job = response["jobs"][0]
    assert job["submitted_at"] == "2026-11-15T15:00:00+00:00"
    assert job["ended_at"] == "2026-11-15T15:30:00+00:00"


@pytest.mark.parametrize("source", ["accounting", "queue"])
@pytest.mark.parametrize("failure", ["returncode", "missing", "timeout"])
def test_unavailable_sources_are_reported_not_hidden(
    tmp_path, monkeypatch, source, failure
):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    root = tmp_path / "handoffs"
    handoff(root, "a" * 32, "42")
    command = "sacct" if source == "accounting" else "squeue"

    def scheduler(args, directory):
        if args[0] == command:
            if failure == "missing":
                raise FileNotFoundError("PRIVATE_PATH")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
            return subprocess.CompletedProcess(args, 1, "", "PRIVATE_STDERR")
        return subprocess.CompletedProcess(args, 0, "", "")

    response = job_list.list_jobs(scheduler, tmp_path, root)
    assert response["unavailable"] == [source]
    assert response["jobs"][0]["state"] == "UNKNOWN"
    assert "PRIVATE_" not in str(response)


def test_newest_handoffs_are_kept_within_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    monkeypatch.setattr(job_list, "MAX_JOBS", 2)
    root = tmp_path / "handoffs"
    for index, letter in enumerate("abcd"):
        handoff(root, letter * 32, None, "PREPARED", age=index * DAY)
    response = job_list.list_jobs(scheduler_for(), tmp_path, root)
    assert response["truncated"]
    assert [row["bundle"][0] for row in response["jobs"]] == ["a", "b"]


def test_links_malformed_and_foreign_receipts_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(job_list.os, "getuid", lambda: os.stat(tmp_path).st_uid)
    root = tmp_path / "handoffs"
    directory = handoff(root, "a" * 32, None, "PREPARED")
    outside = tmp_path / "private.json"
    outside.write_text(
        json.dumps(
            {"bundle": directory.name, "name": "PRIVATE.gjf", "submission": "PREPARED"}
        )
    )
    (directory / "receipt.json").unlink()
    (directory / "receipt.json").symlink_to(outside)
    (root / ("b" * 32)).symlink_to(directory, target_is_directory=True)
    broken = handoff(root, "c" * 32)
    (broken / "receipt.json").write_text("not JSON")
    renamed = handoff(root, "d" * 32)
    (renamed / "receipt.json").write_text(
        json.dumps({"bundle": "e" * 32, "name": "x.gjf", "submission": "PREPARED"})
    )
    unsafe = handoff(root, "f" * 32)
    (unsafe / "receipt.json").write_text(
        json.dumps({"bundle": "f" * 32, "name": "../x.gjf", "submission": "PREPARED"})
    )
    response = job_list.list_jobs(
        lambda *_: pytest.fail("No valid recorded jobs"), tmp_path, root
    )
    assert response["jobs"] == []
    assert "PRIVATE" not in str(response)


def test_jobs_operation_never_creates_storage_or_uses_a_payload_user(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tensor, "_scheduler", lambda *a: pytest.fail("No recorded jobs to check")
    )
    root = tmp_path / "must-not-create"
    response = tensor.handle(
        {"action": "jobs", "bundle": "a" * 32, "user": "somebody-else"}, root
    )
    assert not root.exists()
    assert response["bundle"] == "a" * 32 and response["jobs"] == []
    capabilities = tensor.handle({"action": "version", "bundle": "a" * 32}, root)
    assert {"jobs", "input"} <= set(capabilities["capabilities"])


def test_input_copy_is_exact_and_refuses_a_changed_file(tmp_path, monkeypatch):
    monkeypatch.setenv("INKLY_CLUSTER_PROFILE_NAME", "cuttlefish")
    root = tmp_path / "handoffs"
    data = b"%nprocshared=2\n%mem=1GB\n# hf/sto-3g\n\nWater\n\n0 1\nH 0 0 0\n\n"
    prepared = tensor.handle(
        {
            "action": "prepare",
            "bundle": "a" * 32,
            "name": "water.gjf",
            "data": base64.b64encode(data).decode(),
            "cpus": 2,
            "memory_mib": 1024,
            "hours": 1,
        },
        root,
    )
    before = snapshot(root)
    copy = tensor.handle({"action": "input", "bundle": "a" * 32}, root)
    assert copy["name"] == "water.gjf"
    assert base64.b64decode(copy["data"]) == data
    assert copy["sha256"] == hashlib.sha256(data).hexdigest() == prepared["sha256"]
    after = snapshot(root)
    # Reading the copy changes nothing but the bundle's own lock file.
    assert {p: v for p, v in after.items() if "lock" not in p.name} == {
        p: v for p, v in before.items() if "lock" not in p.name
    }
    (root / ("a" * 32) / "water.gjf").write_bytes(data + b"edited\n")
    with pytest.raises(ValueError, match="changed on the HPC"):
        tensor.handle({"action": "input", "bundle": "a" * 32}, root)
    with pytest.raises(ValueError, match="Unknown bundle"):
        tensor.handle({"action": "input", "bundle": "b" * 32}, root)
