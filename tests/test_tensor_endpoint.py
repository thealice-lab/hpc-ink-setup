"""Private file workflow and scheduler failure boundaries for the HPC endpoint."""

import base64
import hashlib
import json
import os
import re
import subprocess

import pytest

from inkly.gaussian import tensor


pytestmark = pytest.mark.skipif(os.name != "posix", reason="HPC endpoint is Linux-only")
INPUT = (
    b"%rwf=sample_1.rwf\n%chk=sample_1.chk\n%nprocshared=32\n%mem=63GB\n"
    b"# hf/sto-3g\n\nsample_1 scaling metadata\n\n0 1\nH 0 0 0\n\n"
    b"--Link1--\n%chk=sample_1.chk\n# hf/sto-3g geom=allcheck guess=read\n\n"
)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("INKLY_CLUSTER_PROFILE_NAME", "cuttlefish")
    root = tmp_path / "bundles"
    request = {
        "action": "prepare",
        "bundle": "a" * 32,
        "name": "sample_1.gjf",
        "data": base64.b64encode(INPUT).decode(),
        "cpus": 2,
        "memory_mib": 1024,
        "hours": 1,
    }
    result = tensor.handle(request, root)
    return root, request, result


def operate(prepared, action, **kwargs):
    root, request, _ = prepared
    return tensor.handle(
        {"bundle": request["bundle"], "action": action, **kwargs}, root
    )


def test_preview_preserves_bytes_resources_and_private_modes(prepared):
    root, request, result = prepared
    directory = root / request["bundle"]
    assert (directory / request["name"]).read_bytes() == INPUT
    assert result["sha256"] == hashlib.sha256(INPUT).hexdigest()
    assert result["stages"] == 2
    assert "#SBATCH --mem=1024M" in result["script"]
    assert "GAUSSIAN_MEM_MB=768" in result["script"]
    assert "#SBATCH --time=1:00:00" in result["script"]
    assert directory.stat().st_mode & 0o777 == 0o700
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    syntax = subprocess.run(
        ["bash", "-n", str(directory / "job.sbatch")], capture_output=True
    )
    assert syntax.returncode == 0, syntax.stderr
    with pytest.raises(FileExistsError):
        tensor.handle(request, root)


def test_confirmed_submission_records_job_and_prevents_duplicates(
    prepared, monkeypatch
):
    calls = []

    def scheduler(args, directory):
        calls.append(args)
        persisted = json.loads((directory / "receipt.json").read_text())
        assert persisted["submission"] == "SUBMISSION_UNKNOWN"
        return subprocess.CompletedProcess(args, 0, "12345;local\n", "")

    monkeypatch.setattr(tensor, "_scheduler", scheduler)
    with pytest.raises(ValueError, match="confirmation"):
        operate(prepared, "submit")
    assert not calls
    result = operate(prepared, "submit", confirm=True)
    assert result["job_id"] == "12345"
    assert calls == [
        [
            "sbatch",
            "--parsable",
            "--comment=inkly:" + prepared[1]["bundle"],
            "job.sbatch",
        ]
    ]
    with pytest.raises(ValueError, match="Already attempted"):
        operate(prepared, "submit", confirm=True)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "rejection", "malformed"])
def test_ambiguous_or_failed_submission_never_retries(prepared, monkeypatch, failure):
    calls = []

    def scheduler(args, directory):
        calls.append(args)
        persisted = json.loads((directory / "receipt.json").read_text())
        assert persisted["submission"] == "SUBMISSION_UNKNOWN"
        assert args == [
            "sbatch",
            "--parsable",
            "--comment=inkly:" + prepared[1]["bundle"],
            "job.sbatch",
        ]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        return subprocess.CompletedProcess(
            args, int(failure == "rejection"), "unconfirmed", "private output"
        )

    monkeypatch.setattr(tensor, "_scheduler", scheduler)
    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        operate(prepared, "submit", confirm=True)
    assert operate(prepared, "status")["state"] == "SUBMISSION_UNKNOWN"
    with pytest.raises(ValueError, match="Already attempted"):
        operate(prepared, "submit", confirm=True)
    assert len(calls) == 1


def test_changed_input_blocks_submission(prepared):
    root, request, _ = prepared
    (root / request["bundle"] / request["name"]).write_bytes(INPUT + b"changed")
    with pytest.raises(ValueError, match="changed"):
        operate(prepared, "submit", confirm=True)


def test_completed_output_checks_scheduler_and_all_gaussian_stages(
    prepared, monkeypatch
):
    monkeypatch.setattr(
        tensor,
        "_scheduler",
        lambda *a: subprocess.CompletedProcess(a, 0, "12345\n", ""),
    )
    operate(prepared, "submit", confirm=True)
    root, request, _ = prepared
    output = root / request["bundle"] / "sample_1.log"
    tensor._write(output, b"Normal termination of Gaussian\n")
    monkeypatch.setattr(
        tensor,
        "_scheduler",
        lambda *a: subprocess.CompletedProcess(a, 0, "12345|COMPLETED|0:0|\n", ""),
    )
    with pytest.raises(ValueError, match="incomplete"):
        operate(prepared, "fetch")
    output.write_bytes(b"Normal termination of Gaussian\n" * 2)
    result = operate(prepared, "fetch")
    assert (
        hashlib.sha256(base64.b64decode(result["data"])).hexdigest() == result["sha256"]
    )
    output.write_bytes(b"Normal termination of Gaussian\n" * 2 + b"Error termination")
    with pytest.raises(ValueError, match="error"):
        operate(prepared, "fetch")


def test_running_job_cannot_be_fetched_and_queries_only_its_id(prepared, monkeypatch):
    monkeypatch.setattr(
        tensor,
        "_scheduler",
        lambda *a: subprocess.CompletedProcess(a, 0, "12345\n", ""),
    )
    operate(prepared, "submit", confirm=True)

    def scheduler(args, directory):
        assert "12345" in args and "-a" not in args
        return subprocess.CompletedProcess(args, 0, "12345|RUNNING|0:0|\n", "")

    monkeypatch.setattr(tensor, "_scheduler", scheduler)
    with pytest.raises(ValueError, match="COMPLETED"):
        operate(prepared, "fetch")


@pytest.mark.parametrize(
    "directive",
    [
        b"%chk=/tmp/private.chk",
        b"%rwf=../x.rwf",
        b"%oldchk=x.chk",
        b"%cpu=0-1",
        b"%chk=receipt.json",
    ],
)
def test_external_or_unsupported_link0_rejected(directive):
    with pytest.raises(ValueError):
        tensor._validate_input(directive + b"\n# hf/sto-3g\n")


def test_runtime_rewrites_rwf_only_in_temporary_copy(prepared, tmp_path):
    root, request, result = prepared
    source = root / request["bundle"] / request["name"]
    target = tmp_path / "runtime.com"
    program = result["script"].split("<<'PY_INPUT'\n", 1)[1].split("\nPY_INPUT", 1)[0]
    import sys

    run = subprocess.run(
        [sys.executable, "-c", program, str(source), str(target), "768", "2", "1"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    text = target.read_text()
    assert f"%rwf={tmp_path / 'sample_1.rwf'}" in text
    assert "%chk=sample_1.chk" in text
    assert "sample_1 scaling metadata" in text
    assert text.count("%mem=768MB") == 2
    assert source.read_bytes() == INPUT


def test_symlink_bundle_cannot_expose_other_files(prepared, tmp_path):
    root, _, _ = prepared
    (root / ("b" * 32)).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PermissionError):
        tensor.handle({"action": "status", "bundle": "b" * 32}, root)


@pytest.mark.parametrize(
    "key,value", [("cpus", 0), ("memory_mib", 1), ("hours", 0), ("hours", 337)]
)
def test_invalid_resource_requests_cannot_prepare_or_submit(prepared, key, value):
    root, request, _ = prepared
    request = {**request, "bundle": "b" * 32, key: value}
    with pytest.raises(ValueError):
        tensor.handle(request, root)
    assert not (root / request["bundle"]).exists()


def test_exported_resources_are_reused_with_cluster_memory_headroom(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("INKLY_CLUSTER_PROFILE_NAME", "cuttlefish")
    data = b"%nprocshared=4\n%mem=2GB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
    data += b"--Link1--\n%NProc=4\n%Mem=2048MB\n# hf/sto-3g geom=allcheck\n\n"
    request = {
        "action": "prepare",
        "bundle": "b" * 32,
        "name": "export.gjf",
        "data": base64.b64encode(data).decode(),
    }
    result = tensor.handle(request, tmp_path / "bundles")
    assert result["resources"]["cpus"] == 4
    assert result["resources"]["gaussian_memory_mb"] == 2048
    assert result["resources"]["slurm_memory_mib"] == 2731
    assert result["resources"]["exported_memory_mb"] == [2048, 2048]
    assert "GAUSSIAN_MEM_MB=2048" in result["script"]
    assert (
        tmp_path / "bundles" / request["bundle"] / "export.gjf"
    ).read_bytes() == data
    restored = tensor.handle(
        {"action": "status", "bundle": request["bundle"]}, tmp_path / "bundles"
    )
    assert restored["resources"] == result["resources"]
    assert restored["script"] == result["script"]


@pytest.mark.parametrize(
    "header,option",
    [
        ("%mem=768MB", "--cpus"),
        ("%nprocshared=2", "--memory-mib"),
        ("%nprocshared=2\n%mem=100MW", "--memory-mib"),
        ("%nprocshared=2\n%nproc=4\n%mem=768MB", "--cpus"),
        (
            "%nprocshared=2\n%mem=768MB\n# hf\n--Link1--\n%nprocshared=4\n%mem=768MB",
            "--cpus",
        ),
        (
            "%nprocshared=2\n%mem=768MB\n# hf\n--Link1--\n%nprocshared=2\n%mem=1GB",
            "--memory-mib",
        ),
        ("%nprocshared=2\n%mem=768MB\n# hf\n--Link1--", "--cpus"),
    ],
)
def test_ambiguous_exports_require_explicit_values(
    tmp_path, monkeypatch, header, option
):
    monkeypatch.setenv("INKLY_CLUSTER_PROFILE_NAME", "cuttlefish")
    data = (header + "\n# hf/sto-3g\n").encode()
    root = tmp_path / "bundles"
    request = {
        "action": "prepare",
        "bundle": "c" * 32,
        "name": "export.gjf",
        "data": base64.b64encode(data).decode(),
    }
    with pytest.raises(ValueError, match=option):
        tensor.handle(request, root)
    assert not (root / request["bundle"]).exists()


def test_explicit_overrides_are_visible_in_preview(prepared):
    _, _, result = prepared
    resources = result["resources"]
    assert resources["cpus_source"] == "explicit override"
    assert resources["memory_source"] == "explicit Slurm override"
    assert resources["exported_cpus"] == [32, None]
    assert resources["exported_memory_mb"] == [64512, None]
    assert resources["gaussian_memory_mb"] == 768


@pytest.mark.parametrize("fraction", ["invalid", 0, 1, "NaN"])
def test_resource_reuse_rejects_invalid_trusted_memory_policy(fraction):
    profile = {"software": {"gaussian": {"slurm": {"auto_memory_fraction": fraction}}}}
    with pytest.raises(ValueError, match="trusted Gaussian memory"):
        tensor._resource_preview(
            INPUT, {"name": "sample.gjf", "cpus": 2, "memory_mib": 1024}, profile
        )


def test_session_protocol_handles_multiple_lines_and_flushes(monkeypatch, capsys):
    import io
    import sys
    from types import SimpleNamespace

    requests = [
        {"action": "status", "bundle": "d" * 32},
        {"action": "status", "bundle": "e" * 32},
    ]
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b"".join(json.dumps(r).encode() + b"\n" for r in requests)
            )
        ),
    )
    monkeypatch.setattr(tensor, "handle", lambda request: {"bundle": request["bundle"]})
    assert tensor.main(session=True) == 0
    assert [
        json.loads(line)["bundle"] for line in capsys.readouterr().out.splitlines()
    ] == ["d" * 32, "e" * 32]


def test_guided_client_to_real_endpoint_with_mocked_slurm(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from tests.test_tensor_bridge import bridge

    source = tmp_path / "molecule_1.gjf"
    source.write_bytes(
        b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
    )
    original = source.read_bytes()
    peer = """
import json, os, subprocess, sys
from pathlib import Path
from inkly.gaussian import tensor
os.environ['INKLY_CLUSTER_PROFILE_NAME'] = 'cuttlefish'
root = Path(sys.argv[1]) / 'bundles'
original_handle = tensor.handle
tensor.handle = lambda request: original_handle(request, root)
def scheduler(args, directory):
    if args[0] == 'sbatch':
        count = directory / 'synthetic-submit-count'
        count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
        (directory / 'molecule_1.log').write_bytes(b'Normal termination of Gaussian\\n')
        return subprocess.CompletedProcess(args, 0, '12345\\n', '')
    return subprocess.CompletedProcess(args, 0, '12345|COMPLETED|0:0|\\n', '')
tensor._scheduler = scheduler
sys.exit(tensor.main(session=True))
"""
    popen = subprocess.Popen
    starts = []

    def start(args, **kwargs):
        starts.append(args)
        return popen([sys.executable, "-u", "-c", peer, str(tmp_path)], **kwargs)

    monkeypatch.setattr(bridge.subprocess, "Popen", start)
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(["s", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert bridge.main(["run", str(source), "--host", "cluster"]) == 0
    assert len(starts) == 1
    assert source.read_bytes() == original
    assert (
        tmp_path / "returned/molecule_1.log"
    ).read_bytes() == b"Normal termination of Gaussian\n"
    assert [
        p.read_text() for p in (tmp_path / "bundles").glob("*/synthetic-submit-count")
    ] == ["1"]


def test_version_reports_capabilities_without_files_locks_or_slurm(
    tmp_path, monkeypatch
):
    def scheduler(*args):
        raise AssertionError("version must not call Slurm")

    monkeypatch.setattr(tensor, "_scheduler", scheduler)
    root = tmp_path / "bundles"
    result = tensor.handle({"action": "version", "bundle": "d" * 32}, root)
    assert result["bundle"] == "d" * 32
    assert result["version"]
    assert {"prepare", "submit", "status", "fetch", "cancel", "version"} <= set(
        result["capabilities"]
    )
    assert not root.exists()


def test_unknown_operation_reply_keeps_session_open(monkeypatch, capsys):
    """Desktop probes older endpoints with "version"; an error must not end SSH."""
    import io
    import sys
    from types import SimpleNamespace

    requests = [
        {"action": "something-new", "bundle": "d" * 32},
        {"action": "version", "bundle": "d" * 32},
    ]
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b"".join(json.dumps(r).encode() + b"\n" for r in requests)
            )
        ),
    )
    assert tensor.main(session=True) == 0
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0] == {"ok": False, "error": "Unsupported operation"}
    assert replies[1]["ok"] and "cancel" in replies[1]["capabilities"]


def submitted(prepared, monkeypatch):
    monkeypatch.setattr(
        tensor,
        "_scheduler",
        lambda *a: subprocess.CompletedProcess(a, 0, "12345\n", ""),
    )
    operate(prepared, "submit", confirm=True)


def slurm(states, calls, scancel_code=0, check=None):
    """Fake sacct answers in order (last repeats) and record every call."""
    answers = list(states)

    def scheduler(args, directory):
        calls.append(list(args))
        if args[0] == "scancel":
            if check:
                check(directory)
            return subprocess.CompletedProcess(args, scancel_code, "", "")
        if args[0] == "sacct":
            state = answers.pop(0) if len(answers) > 1 else answers[0]
            return subprocess.CompletedProcess(args, 0, f"12345|{state}|0:0\n", "")
        raise AssertionError(f"unexpected scheduler call {args}")

    return scheduler


def test_cancel_needs_confirmation_and_a_recorded_job(prepared, monkeypatch):
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", slurm(["PENDING"], calls))
    with pytest.raises(ValueError, match="No Slurm job"):
        operate(prepared, "cancel", confirm=True)
    submitted(prepared, monkeypatch)
    monkeypatch.setattr(tensor, "_scheduler", slurm(["PENDING"], calls))
    with pytest.raises(ValueError, match="confirmation"):
        operate(prepared, "cancel")
    assert not calls


def test_cancel_pending_job_records_request_before_scancel(prepared, monkeypatch):
    submitted(prepared, monkeypatch)
    calls = []

    def recorded_first(directory):
        persisted = json.loads((directory / "receipt.json").read_text())
        assert len(persisted["cancel_requested"]) == 1

    monkeypatch.setattr(
        tensor,
        "_scheduler",
        slurm(["PENDING", "CANCELLED by 1000"], calls, check=recorded_first),
    )
    result = operate(prepared, "cancel", confirm=True)
    assert result["state"] == "CANCELLED by 1000"
    assert result["job_id"] == "12345"
    assert result["submission"] == "SUBMITTED"
    scancels = [call for call in calls if call[0] == "scancel"]
    assert scancels == [["scancel", "--state=PENDING", "12345"]]
    with pytest.raises(ValueError, match="Only PENDING.*CANCELLED"):
        operate(prepared, "cancel", confirm=True)
    assert len([call for call in calls if call[0] == "scancel"]) == 1
    with pytest.raises(ValueError, match="Already attempted"):
        operate(prepared, "submit", confirm=True)


@pytest.mark.parametrize(
    "state", ["RUNNING", "COMPLETING", "COMPLETED", "FAILED", "CANCELLED by 7"]
)
def test_cancel_refuses_jobs_that_are_not_pending(prepared, monkeypatch, state):
    submitted(prepared, monkeypatch)
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", slurm([state], calls))
    with pytest.raises(ValueError, match="Only PENDING"):
        operate(prepared, "cancel", confirm=True)
    assert all(call[0] != "scancel" for call in calls)
    root, request, _ = prepared
    receipt = json.loads((root / request["bundle"] / "receipt.json").read_text())
    assert "cancel_requested" not in receipt


def test_failed_scancel_is_reported_once_and_never_retried(prepared, monkeypatch):
    submitted(prepared, monkeypatch)
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", slurm(["PENDING"], calls, 1))
    with pytest.raises(ValueError, match="unconfirmed"):
        operate(prepared, "cancel", confirm=True)
    assert len([call for call in calls if call[0] == "scancel"]) == 1
    root, request, _ = prepared
    receipt = json.loads((root / request["bundle"] / "receipt.json").read_text())
    assert len(receipt["cancel_requested"]) == 1


def test_status_reads_after_cancel_are_bounded(prepared, monkeypatch):
    submitted(prepared, monkeypatch)
    calls, sleeps = [], []
    monkeypatch.setattr(tensor, "_scheduler", slurm(["PENDING"], calls))
    monkeypatch.setattr(tensor.time, "sleep", sleeps.append)
    result = operate(prepared, "cancel", confirm=True)
    assert result["state"] == "PENDING"
    assert len([call for call in calls if call[0] == "scancel"]) == 1
    assert len([call for call in calls if call[0] == "sacct"]) == 1 + (
        tensor.CANCEL_STATUS_READS
    )
    assert len(sleeps) == tensor.CANCEL_STATUS_READS - 1


@pytest.mark.parametrize("state", ["PENDING", "RUNNING"])
def test_watch_status_reads_accounting_once_per_check(prepared, monkeypatch, state):
    # Each watch check stays one sacct call per job; the queue is read only
    # when accounting has no record (and by Job details on request).
    root, request, _ = prepared
    receipt_path = root / request["bundle"] / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(submission="SUBMITTED", job_id="12345")
    receipt_path.write_text(json.dumps(receipt))
    calls = []

    def scheduler(args, directory):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, f"12345|{state}|0:0\n", "")

    monkeypatch.setattr(tensor, "_scheduler", scheduler)
    response = operate(prepared, "status")
    assert response["state"] == state and calls == ["sacct"]


SINFO = "general*|7-00:00:00|64|256000\ndebug|1:00:00|8|16000\n"


def script_checks(calls, refusal=None, sinfo=SINFO, assoc="users|normal\n"):
    """Fake Slurm for script checks; --test-only never submits."""

    def scheduler(args, directory):
        calls.append(args)
        if args[:2] == ["sbatch", "--test-only"]:
            candidate = directory / args[-1]
            assert candidate.name.startswith(".revise-")
            assert candidate.stat().st_mode & 0o777 == 0o600
            if isinstance(refusal, BaseException):
                raise refusal
            if isinstance(refusal, tuple):
                return subprocess.CompletedProcess(args, 1, *refusal)
            if refusal:
                return subprocess.CompletedProcess(args, 1, "", refusal)
            return subprocess.CompletedProcess(args, 0, "", "sbatch: Job 1 to start")
        if args[0] == "sacctmgr":
            assert args[:5] == ["sacctmgr", "-n", "-P", "show", "assoc"]
            if isinstance(assoc, BaseException):
                raise assoc
            if assoc is None:
                return subprocess.CompletedProcess(args, 1, "", "sacctmgr: error")
            return subprocess.CompletedProcess(args, 0, assoc, "")
        if args[0] == "sinfo":
            if sinfo is None:
                return subprocess.CompletedProcess(args, 1, "", "sinfo: error")
            return subprocess.CompletedProcess(args, 0, sinfo, "")
        raise AssertionError(f"unexpected Slurm call {args}")

    return scheduler


def revise(prepared, script, expected=None):
    root, request, result = prepared
    receipt = json.loads((root / request["bundle"] / "receipt.json").read_text())
    return operate(
        prepared,
        "revise_script",
        script=script,
        expected_sha256=expected or receipt["script_sha256"],
    )


def test_revised_script_is_stored_checked_and_submitted(prepared, monkeypatch):
    root, request, result = prepared
    directory = root / request["bundle"]
    generated = result["script"]
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", script_checks(calls))
    edited = generated.replace("--time=1:00:00", "--time=4:00:00").replace(
        "#SBATCH --hint=nomultithread",
        "#SBATCH --hint=nomultithread\n#SBATCH --mail-type=END",
    )
    response = revise(prepared, edited.replace("\n", "\r\n"))
    assert response["script"] == edited
    assert response["generated_script"] == generated
    assert response["script_edited"] is True
    assert response["state"] == "PREPARED"
    assert response["script_resources"] == {
        "cpus": 2,
        "slurm_memory_mib": 1024,
        "time_limit": "4:00:00",
        "hours": 4,
        "partition": "general",
    }
    assert response["resources"] == result["resources"]
    assert response["warnings"] == []
    assert calls[0] == [
        "sbatch",
        "--test-only",
        "--comment=inkly:" + request["bundle"],
        calls[0][-1],
    ]
    assert (directory / "job.sbatch").read_text() == edited
    assert (directory / "job.sbatch.generated").read_text() == generated
    assert not list(directory.glob(".revise-*"))
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    saved = json.loads((directory / "receipt.json").read_text())
    assert saved["script_sha256"] == hashlib.sha256(edited.encode()).hexdigest()
    assert saved["generated_script_sha256"] == result["script_sha256"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", saved["script_edited_at"])
    status = operate(prepared, "status")
    assert status["script"] == edited
    assert status["generated_script"] == generated

    submitted = []
    monkeypatch.setattr(
        tensor,
        "_scheduler",
        lambda args, d: (
            submitted.append(args)
            or subprocess.CompletedProcess(args, 0, "12345\n", "")
        ),
    )
    assert operate(prepared, "submit", confirm=True)["job_id"] == "12345"
    assert submitted == [
        ["sbatch", "--parsable", "--comment=inkly:" + request["bundle"], "job.sbatch"]
    ]


def test_reset_to_generated_needs_no_slurm_and_clears_the_edit(prepared, monkeypatch):
    root, request, result = prepared
    directory = root / request["bundle"]
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", script_checks(calls))
    revise(prepared, result["script"].replace("--time=1:00:00", "--time=2:00:00"))
    calls.clear()
    response = revise(prepared, result["script"])
    assert calls == []
    assert response["script_edited"] is False
    assert "script_resources" not in response
    assert "script_edited_at" not in response
    assert response["warnings"] == []
    assert (directory / "job.sbatch").read_text() == result["script"]
    assert "generated_script" not in operate(prepared, "status")


def test_stale_copy_or_outside_change_is_refused(prepared, monkeypatch):
    root, request, result = prepared
    directory = root / request["bundle"]
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", script_checks(calls))
    edited = result["script"].replace("--time=1:00:00", "--time=2:00:00")
    with pytest.raises(ValueError, match="changed since it was opened"):
        revise(prepared, edited, expected="0" * 64)
    (directory / "job.sbatch").write_text(result["script"] + "# outside\n")
    with pytest.raises(ValueError, match="changed since it was opened"):
        revise(prepared, edited)
    assert calls == []
    assert not (directory / "job.sbatch.generated").exists()


def test_submitted_job_script_cannot_change(prepared, monkeypatch):
    submitted(prepared, monkeypatch)
    with pytest.raises(ValueError, match="not been submitted"):
        revise(prepared, prepared[2]["script"])


@pytest.mark.parametrize(
    "script, message",
    [
        (None, "must be text"),
        ("#!/bin/bash\n\x00\n", "NUL"),
        ("#!/bin/bash\n" + "#" * (64 * 1024), "64 KiB"),
        ("#!/bin/bash\n\ud800\n", "UTF-8"),
        ("  \n", "empty"),
        ("#SBATCH --time=1:00:00\nhostname\n", "#!"),
    ],
)
def test_unusable_scripts_are_refused_before_slurm(
    prepared, monkeypatch, script, message
):
    root, request, result = prepared
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", script_checks(calls))
    with pytest.raises(ValueError, match=message):
        revise(prepared, script)
    assert calls == []
    assert (root / request["bundle"] / "job.sbatch").read_text() == result["script"]


def test_slurm_refusal_is_reported_and_nothing_changes(prepared, monkeypatch):
    root, request, result = prepared
    directory = root / request["bundle"]
    before = (directory / "receipt.json").read_bytes()
    refusal = (
        "sbatch: error: Batch job submission failed: Invalid partition name "
        "specified\x07\nprivate detail\n"
    )
    monkeypatch.setattr(tensor, "_scheduler", script_checks([], refusal=refusal))
    edited = result["script"].replace("--partition=general", "--partition=nope")
    with pytest.raises(ValueError) as error:
        revise(prepared, edited)
    assert str(error.value) == (
        "Slurm would refuse this script: Invalid partition name specified"
    )
    assert (directory / "job.sbatch").read_text() == result["script"]
    assert (directory / "receipt.json").read_bytes() == before
    assert not list(directory.glob(".revise-*"))


@pytest.mark.parametrize(
    "output, reason",
    [
        (
            ("", "sbatch: Requested node configuration is not available\n"),
            "Requested node configuration is not available",
        ),
        (
            ("sbatch: error: Memory specification can not be satisfied\n", ""),
            "Memory specification can not be satisfied",
        ),
        (("", "\n"), None),
    ],
)
def test_slurm_reason_without_an_error_line_is_still_shown(
    prepared, monkeypatch, output, reason
):
    result = prepared[2]
    monkeypatch.setattr(tensor, "_scheduler", script_checks([], refusal=output))
    edited = result["script"].replace("--cpus-per-task=2", "--cpus-per-task=4096")
    with pytest.raises(ValueError) as error:
        revise(prepared, edited)
    assert str(error.value) == (
        f"Slurm would refuse this script: {reason}"
        if reason
        else "Slurm would refuse this script; check its #SBATCH lines"
    )


def test_unavailable_check_refuses_only_unlisted_partitions(prepared, monkeypatch):
    result = prepared[2]
    monkeypatch.setattr(
        tensor,
        "_scheduler",
        script_checks([], refusal=FileNotFoundError("sbatch")),
    )
    with pytest.raises(ValueError, match="no partition named nope"):
        revise(
            prepared,
            result["script"].replace("--partition=general", "--partition=nope"),
        )
    response = revise(
        prepared, result["script"].replace("--partition=general", "--partition=debug")
    )
    assert response["script_resources"]["partition"] == "debug"
    assert any("could not check" in w for w in response["warnings"])


@pytest.mark.parametrize(
    "trouble",
    [
        "sbatch: error: Batch job submission failed: Unable to contact slurm "
        "controller (connect failure)\n",
        "sbatch: error: Batch job submission failed: Socket timed out on "
        "send/recv operation\n",
        "sbatch: error: Slurm backup controller in standby mode\n",
        subprocess.TimeoutExpired(["sbatch"], 30),
    ],
)
def test_unreachable_controller_saves_with_a_warning(prepared, monkeypatch, trouble):
    """Slurm being unavailable is not a verdict on the script."""
    root, request, result = prepared
    calls = []
    monkeypatch.setattr(
        tensor, "_scheduler", script_checks(calls, refusal=trouble, sinfo=None)
    )
    edited = result["script"].replace("--time=1:00:00", "--time=2:00:00")
    response = revise(prepared, edited)
    assert response["script_edited"] is True
    assert response["script"] == edited
    assert any("Slurm could not check this script" in w for w in response["warnings"])
    assert (root / request["bundle"] / "job.sbatch").read_text() == edited
    assert [call[0] for call in calls] == ["sbatch", "sinfo"]


def test_own_script_is_saved_with_warnings_about_inkly_lines(prepared, monkeypatch):
    monkeypatch.setattr(tensor, "_scheduler", script_checks([]))
    own = (
        "#!/bin/bash\n"
        "#SBATCH -c 4 --mem=2G\n"
        "#SBATCH -t 2-00:00:00\n"
        "#SBATCH --array=1-3 --output=run.out\n"
        "module load gaussian\n"
        "g16 < sample_1.gjf > sample_1.out\n"
    )
    response = revise(prepared, own)
    assert response["script"] == own
    assert response["script_resources"] == {
        "cpus": 4,
        "slurm_memory_mib": 2048,
        "time_limit": "2-00:00:00",
        "hours": 48,
        "partition": None,
    }
    warnings = "\n".join(response["warnings"])
    for expected in [
        "exactly the prepared input",
        "sample_1.log",
        "started the way Inkly starts it",
        "--hint=nomultithread",
        "--array is set",
        "--output is set",
    ]:
        assert expected in warnings
    assert "REQUESTED_CPUS" not in warnings


def test_cpu_and_memory_mismatches_warn_and_point_to_desktop_fields(
    prepared, monkeypatch
):
    result = prepared[2]
    monkeypatch.setattr(tensor, "_scheduler", script_checks([]))
    fewer = revise(
        prepared,
        result["script"]
        .replace("--cpus-per-task=2", "--cpus-per-task=1")
        .replace("--mem=1024M", "--mem=700M"),
    )
    warnings = "\n".join(fewer["warnings"])
    assert "1 CPUs but starts Gaussian on 2" in warnings
    assert "stop before Gaussian runs" in warnings
    assert "768 MB (GAUSSIAN_MEM_MB), more than the 700 MiB" in warnings
    assert "CPU and memory fields" in warnings
    tight = revise(
        prepared,
        result["script"]
        .replace("--cpus-per-task=2", "--cpus-per-task=8")
        .replace("--mem=1024M", "--mem=1000M"),
    )
    warnings = "\n".join(tight["warnings"])
    assert "Gaussian uses only 2" in warnings
    assert "less room than Inkly's usual headroom" in warnings


def test_unknown_account_or_qos_warns_without_refusing(prepared, monkeypatch):
    result = prepared[2]
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", script_checks(calls))
    edited = result["script"].replace(
        "#SBATCH --hint=nomultithread",
        "#SBATCH --hint=nomultithread\n#SBATCH -A labx --qos=hiprio",
    )
    response = revise(prepared, edited)
    assert response["script"] == edited
    warnings = "\n".join(response["warnings"])
    assert "no account labx for you (yours: users)" in warnings
    assert "does not list QOS hiprio for you (yours: normal)" in warnings
    assert calls[-1][:5] == ["sacctmgr", "-n", "-P", "show", "assoc"]
    assert calls[-1][-1] == "format=account,qos"


@pytest.mark.parametrize(
    "assoc", ["USERS|normal,Long\n", None, FileNotFoundError("sacctmgr"), "\n"]
)
def test_known_account_or_no_accounting_gives_no_account_warning(
    prepared, monkeypatch, assoc
):
    """Listed names (any case) pass; without accounting records nothing is said."""
    result = prepared[2]
    monkeypatch.setattr(tensor, "_scheduler", script_checks([], assoc=assoc))
    edited = result["script"].replace(
        "#SBATCH --hint=nomultithread",
        "#SBATCH --hint=nomultithread\n#SBATCH --account=users -q long",
    )
    response = revise(prepared, edited)
    assert not [w for w in response["warnings"] if "accounting" in w]


def test_accounting_is_not_asked_without_account_or_qos(prepared, monkeypatch):
    calls = []
    monkeypatch.setattr(tensor, "_scheduler", script_checks(calls))
    revise(prepared, prepared[2]["script"].replace("--time=1:00:00", "--time=2:00:00"))
    assert "sacctmgr" not in [call[0] for call in calls]


def test_partition_limits_from_sinfo_warn(prepared, monkeypatch):
    result = prepared[2]
    monkeypatch.setattr(tensor, "_scheduler", script_checks([]))
    response = revise(
        prepared,
        result["script"]
        .replace("--partition=general", "--partition=debug")
        .replace("--time=1:00:00", "--time=3:00:00")
        .replace("--cpus-per-task=2", "--cpus-per-task=16")
        .replace("--mem=1024M", "--mem=32G"),
    )
    warnings = "\n".join(response["warnings"])
    assert "longer than partition debug allows" in warnings
    assert "No node in partition debug has 16 CPUs (largest: 8)" in warnings
    assert "No node in partition debug has 32768 MiB" in warnings


def test_edited_jobs_are_marked_in_history_and_details(prepared, monkeypatch):
    root, request, result = prepared
    monkeypatch.setattr(tensor, "_scheduler", script_checks([]))
    revise(prepared, result["script"].replace("--time=1:00:00", "--time=2:00:00"))
    jobs = tensor.handle({"action": "jobs", "bundle": "0" * 32}, root)["jobs"]
    assert [job["script_edited"] for job in jobs] == [True]
    details = operate(prepared, "job_details")
    assert details["script_edited"] is True


def test_version_offers_revise_script(tmp_path):
    result = tensor.handle({"action": "version", "bundle": "d" * 32}, tmp_path)
    assert "revise_script" in result["capabilities"]
