"""Read-only job details: bounded scheduler queries and owned log excerpts."""

import json
import os
import subprocess

import pytest

from inkly.gaussian import job_insights as insights, tensor


def result(args, text="", code=0):
    return subprocess.CompletedProcess(args, code, text, "")


def test_pending_reason_estimate_and_elapsed_from_live_queue(tmp_path):
    calls = []

    def scheduler(args, directory):
        calls.append(args)
        if args[0] == "sacct":
            return result(
                args,
                "42.batch|RUNNING|0:0|None|Unknown|00:01\n42|PENDING|0:0|None|Unknown|00:00:00\n",
            )
        return result(
            args,
            "99|PENDING|Other|Unknown|00:00\n42|PENDING|Priority|2026-11-16T12:30:00|0:00\n",
        )

    response = insights.query_status(tmp_path, {"job_id": "42"}, scheduler)
    assert response["reason"] == "Priority"
    assert response["start_time"] == "2026-11-16T12:30:00"
    assert response["elapsed"] == "0:00"
    assert response["state"] == "PENDING"
    assert response["observed_at"].endswith("+00:00")
    assert [c[0] for c in calls] == ["sacct", "squeue"]
    assert all("42" in c for c in calls)
    assert calls[1][-1] == "%A|%T|%r|%S|%M"


@pytest.mark.parametrize("problem", ["missing", "timeout", "old-format"])
def test_optional_queue_data_never_masks_accounting(tmp_path, problem):
    def scheduler(args, directory):
        if args[0] == "sacct":
            return result(args, "42|PENDING|0:0|Dependency|Unknown|00:00:00\n")
        if problem == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        if problem == "missing":
            raise FileNotFoundError()
        return result(args, "42|PENDING\n")

    response = insights.query_status(tmp_path, {"job_id": "42"}, scheduler)
    assert response["state"] == "PENDING"
    assert response["reason"] == "Dependency"
    assert response["start_time"] is None


def test_queue_fallback_and_unknown_estimate(tmp_path):
    def scheduler(args, _):
        if args[0] == "sacct":
            return result(args, code=1)
        return result(args, "42|PENDING|Resources|N/A|00:00\n")

    response = insights.query_status(tmp_path, {"job_id": "42"}, scheduler)
    assert response["reason"] == "Resources" and response["start_time"] is None


def test_terminal_accounting_never_uses_stale_queue(tmp_path):
    def scheduler(args, _):
        assert args[0] == "sacct"
        return result(args, "42|COMPLETED|0:0|None|2026-11-15T01:00:00|01:02:03\n")

    response = insights.query_status(tmp_path, {"job_id": "42"}, scheduler)
    assert response["state"] == "COMPLETED" and response["elapsed"] == "01:02:03"


@pytest.mark.parametrize("job", ["--all", "../secret", "42;rm", 42])
def test_invalid_recorded_job_never_reaches_scheduler(tmp_path, job):
    def scheduler(*_):
        pytest.fail("Invalid job must not query scheduler")

    with pytest.raises(ValueError, match="job ID"):
        insights.query_status(tmp_path, {"job_id": job}, scheduler)


@pytest.fixture
def bundle(tmp_path):
    if os.name != "posix":
        pytest.skip("Owned HPC bundle is Linux-only")
    root = tmp_path / "bundles"
    directory = root / ("a" * 32)
    directory.mkdir(parents=True)
    receipt = {
        "bundle": directory.name,
        "name": "sample_1.gjf",
        "job_id": "42",
        "submission": "SUBMITTED",
    }
    (directory / "receipt.json").write_text(json.dumps(receipt))
    return root, directory, receipt


def scheduler_state(state):
    return lambda args, _: result(args, f"42|{state}|1:0|None|Unknown|00:01:00\n")


@pytest.mark.parametrize("state", ["FAILED", "TIMEOUT", "OUT_OF_MEMORY"])
def test_failed_details_are_bounded_private_and_read_only(bundle, monkeypatch, state):
    root, directory, receipt = bundle
    log = directory / "sample_1.log"
    # Large output is read by seeking to the end; never returned in full.
    log.write_bytes(
        b"PRIVATE_EARLY_CONTENT\n"
        + b"x\n" * 50000
        + b"Error termination via Lnk1e in l9999.exe\n"
    )
    (directory / "slurm-42.out").write_text("slurmstepd: time or memory limit\n")
    before = {
        p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    }
    monkeypatch.setattr(tensor, "_scheduler", scheduler_state(state))
    response = tensor.handle(
        {"action": "job_details", "bundle": receipt["bundle"], "path": "/ignored"}, root
    )
    assert response["state"] == state
    assert "Error termination" in response["gaussian_error"]
    assert response["logs"][0]["truncated"]
    assert all(len(log["text"]) <= insights.TAIL_BYTES for log in response["logs"])
    assert "PRIVATE_EARLY_CONTENT" not in json.dumps(response)
    assert "time or memory limit" in response["logs"][1]["text"]
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    }
    assert not (directory / "submission.lock").exists()


@pytest.mark.parametrize("kind", ["missing", "symlink", "hardlink", "fifo"])
def test_unsafe_or_absent_logs_report_unavailable(bundle, kind):
    root, directory, receipt = bundle
    log = directory / "sample_1.log"
    if kind in {"symlink", "hardlink"}:
        outside = root.parent / "private.log"
        outside.write_text("PRIVATE_OUTSIDE_DATA")
        if kind == "symlink":
            log.symlink_to(outside)
        else:
            os.link(outside, log)
    elif kind == "fifo":
        os.mkfifo(log)
    response = insights.job_details(root, receipt["bundle"], scheduler_state("FAILED"))
    assert not response["logs"][0]["available"]
    assert "PRIVATE_OUTSIDE_DATA" not in json.dumps(response)


def test_pending_details_never_reads_log_and_does_not_create_root(tmp_path, bundle):
    root, directory, receipt = bundle
    (directory / "sample_1.log").write_bytes(b"PRIVATE_DATA")
    response = insights.job_details(root, receipt["bundle"], scheduler_state("PENDING"))
    assert response["logs"] == [] and response["gaussian_error"] is None
    absent = tmp_path / "absent"
    with pytest.raises(FileNotFoundError):
        insights.job_details(absent, receipt["bundle"], scheduler_state("FAILED"))
    assert not absent.exists()


def test_changed_receipt_cannot_read_outside_bundle(bundle):
    root, directory, receipt = bundle
    receipt["name"] = "../private.gjf"
    (directory / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="filename"):
        insights.job_details(root, directory.name, scheduler_state("FAILED"))


def test_symlink_bundle_is_refused(bundle):
    root, directory, receipt = bundle
    link = root / ("b" * 32)
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(PermissionError):
        insights.job_details(root, link.name, scheduler_state("FAILED"))
