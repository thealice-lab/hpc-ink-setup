"""Current-account scheduler listing without bundles or local input files."""

import os
import subprocess

import pytest

from inkly.gaussian import job_list, tensor


def test_listing_scopes_both_commands_to_authenticated_uid_and_merges_queue(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(job_list.os, "getuid", lambda: 1234, raising=False)
    calls = []

    def scheduler(args, directory):
        calls.append(args)
        if args[0] == "sacct":
            return subprocess.CompletedProcess(
                args,
                0,
                "42|sample|PENDING|00:00|0:0\n43|finished|COMPLETED|00:02|0:0\n42.batch|step|RUNNING|00:01|0:0\n",
                "",
            )
        return subprocess.CompletedProcess(
            args, 0, "42|sample|RUNNING|00:01|2026-11-15T12:00:00|None\n", ""
        )

    result = job_list.list_jobs(scheduler, tmp_path)
    assert [row["job_id"] for row in result["jobs"]] == ["43", "42"]
    assert result["jobs"][1]["state"] == "RUNNING"
    assert not result["unavailable"]
    assert result["history_days"] == 7
    assert all("1234" in call for call in calls)
    assert "--uid" in calls[0] and "now-7days" in calls[0]
    assert "--user" in calls[1]
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["returncode", "missing", "timeout"])
def test_partial_failures_are_explicit_not_an_empty_success(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(job_list.os, "getuid", lambda: 1234, raising=False)

    def scheduler(args, directory):
        if args[0] == "sacct":
            if failure == "missing":
                raise FileNotFoundError("PRIVATE_PATH")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
            return subprocess.CompletedProcess(args, 1, "", "PRIVATE_STDERR")
        return subprocess.CompletedProcess(
            args, 0, "42|queued|PENDING|0:00|N/A|Priority\n", ""
        )

    result = job_list.list_jobs(scheduler, tmp_path)
    assert result["unavailable"] == ["accounting"]
    assert result["jobs"][0]["job_id"] == "42"
    assert "PRIVATE_" not in str(result)


def test_limits_keep_queue_jobs_before_recent_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr(job_list.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(job_list, "MAX_JOBS", 3)

    def scheduler(args, directory):
        text = "".join(f"{n}|completed|COMPLETED|00:01|0:0\n" for n in range(100, 106))
        if args[0] == "squeue":
            text = "1|older pending|PENDING|0:00|N/A|Priority\n"
        return subprocess.CompletedProcess(args, 0, text, "")

    result = job_list.list_jobs(scheduler, tmp_path)
    assert result["truncated"]
    assert {row["job_id"] for row in result["jobs"]} == {"1", "104", "105"}


@pytest.mark.skipif(os.name != "posix", reason="Owned HPC receipts")
def test_receipt_catalog_ignores_unrelated_jobs_and_preserves_uncertainty(tmp_path):
    import json

    root = tmp_path / "handoffs"
    for bundle, job, submission in [
        ("a" * 32, "42", "SUBMITTED"),
        ("b" * 32, None, "SUBMISSION_UNKNOWN"),
        ("c" * 32, "99", "SUBMITTED"),
    ]:
        directory = root / bundle
        directory.mkdir(parents=True)
        (directory / "receipt.json").write_text(
            json.dumps(
                {
                    "bundle": bundle,
                    "name": "sample.gjf",
                    "job_id": job,
                    "submission": submission,
                }
            )
        )
    before = {
        p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in root.glob("*/receipt.json")
    }
    calls = []

    def scheduler(args, _):
        calls.append(args)
        if args[0] == "sacct":
            return subprocess.CompletedProcess(
                args,
                0,
                "42|sample|COMPLETED|00:01|0:0\n500|OUTSIDE_JOB|COMPLETED|00:02|0:0\n",
                "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    response = job_list.list_jobs(scheduler, tmp_path, root)
    assert len(response["jobs"]) == 3
    states = {row["bundle"]: row["state"] for row in response["jobs"]}
    assert states == {
        "a" * 32: "COMPLETED",
        "b" * 32: "SUBMISSION_UNKNOWN",
        "c" * 32: "UNKNOWN",
    }
    assert "OUTSIDE_JOB" not in str(response)
    assert all(call[-2:] == ["--jobs", "42,99"] for call in calls)
    assert before == {
        p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in root.glob("*/receipt.json")
    }


@pytest.mark.skipif(os.name != "posix", reason="Owned HPC receipts")
def test_receipt_catalog_skips_links_and_malformed_files(tmp_path):
    import json

    root = tmp_path / "handoffs"
    directory = root / ("a" * 32)
    directory.mkdir(parents=True)
    outside = tmp_path / "private.json"
    outside.write_text(
        json.dumps(
            {"bundle": directory.name, "name": "PRIVATE.gjf", "submission": "PREPARED"}
        )
    )
    (directory / "receipt.json").symlink_to(outside)
    linked = root / ("b" * 32)
    linked.symlink_to(directory, target_is_directory=True)
    broken = root / ("c" * 32)
    broken.mkdir()
    (broken / "receipt.json").write_text("not JSON")
    response = job_list.list_jobs(
        lambda *_: pytest.fail("No valid recorded jobs"), tmp_path, root
    )
    assert response["jobs"] == []
    assert "PRIVATE" not in str(response)


@pytest.mark.skipif(os.name != "posix", reason="HPC endpoint")
def test_jobs_operation_never_creates_bundle_or_uses_payload_user(
    tmp_path, monkeypatch
):
    calls = []

    def scheduler(args, directory):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tensor, "_scheduler", scheduler)
    root = tmp_path / "must-not-create"
    response = tensor.handle(
        {"action": "jobs", "bundle": "a" * 32, "user": "somebody-else"}, root
    )
    assert not root.exists()
    assert response["bundle"] == "a" * 32 and response["jobs"] == []
    assert not calls
    assert "somebody-else" not in str(calls)
