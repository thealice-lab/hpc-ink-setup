"""Private file workflow and scheduler failure boundaries for the HPC endpoint."""

import base64
import hashlib
import json
import os
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
    with pytest.raises(ValueError, match="Already attempted"):
        operate(prepared, "submit", confirm=True)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "rejection", "malformed"])
def test_ambiguous_or_failed_submission_never_retries(prepared, monkeypatch, failure):
    calls = []

    def scheduler(args, directory):
        calls.append(args)
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
