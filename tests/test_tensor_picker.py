"""File-selection workflow tests with synthetic inputs and no live SSH/Slurm."""

import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
from types import SimpleNamespace

import pytest


scripts = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "tensor_bridge", scripts / "tensor_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
sys.modules["tensor_bridge"] = bridge
spec = importlib.util.spec_from_file_location(
    "tensor_picker", scripts / "tensor_picker.py"
)
picker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(picker)

SETTINGS = {
    "host": "researcher@cluster",
    "ink_path": "~/.inkly/bin/ink",
    "auth": "configured",
    "hours": 2,
}


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    folder = tmp_path / "research folder"
    folder.mkdir()
    paths = [folder / f"sample_{number}.gjf" for number in (1, 2)]
    for path in paths:
        path.write_bytes(
            b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
        )
    config = tmp_path / "preferences.json"
    picker.save_settings(config, SETTINGS)
    monkeypatch.setattr(picker, "config_path", lambda: config)
    monkeypatch.setattr(picker.shutil, "which", lambda name: "ssh.exe")
    monkeypatch.setattr(picker, "choose_files", lambda: paths)
    monkeypatch.setattr(picker.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(picker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    calls, connections, records = [], [], {}

    class FakeSession:
        def __init__(self, connection, ssh):
            connections.append((connection, ssh))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def exchange(self, request, *, receipt):
            action, bundle = request["action"], receipt["bundle"]
            calls.append((bundle, action))
            if action == "prepare":
                data = base64.b64decode(request["data"])
                assert request["cpus"] is None and request["memory_mib"] is None
                records[bundle] = {
                    "name": request["name"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "stages": 1,
                    "submission": "PREPARED",
                    "state": "PREPARED",
                    "script": "#!/bin/bash\n# synthetic script\n",
                    "resources": {
                        "cpus": 2,
                        "gaussian_memory_mb": 768,
                        "slurm_memory_mib": 1024,
                        "hours": request["hours"],
                    },
                }
            record = records[bundle]
            if action == "submit":
                assert request["confirm"] is True
                record.update(
                    submission="SUBMITTED", state="RUNNING", job_id=str(len(records))
                )
            if action == "status" and record["state"] == "RUNNING":
                record.update(state="COMPLETED", exit_code="0:0")
            if action == "fetch":
                data = b"synthetic successful output"
                return {
                    "name": Path(record["name"]).with_suffix(".log").name,
                    "data": base64.b64encode(data).decode(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            return dict(record)

    monkeypatch.setattr(bridge, "Session", FakeSession)
    return SimpleNamespace(
        paths=paths,
        config=config,
        calls=calls,
        connections=connections,
        records=records,
    )


def actions(workflow):
    return [action for _, action in workflow.calls]


def test_multifile_preview_reuses_one_connection_and_preserves_inputs(workflow, capsys):
    originals = [path.read_bytes() for path in workflow.paths]
    assert picker.main([]) == 0
    assert actions(workflow) == ["prepare", "prepare"]
    assert len(workflow.connections) == 1
    assert [path.read_bytes() for path in workflow.paths] == originals
    output = capsys.readouterr().out
    assert "Nothing submitted" in output
    assert "sample_1.gjf | 2 | 768 | 1024 | 2" in output
    for path in workflow.paths:
        receipt = json.loads(path.with_suffix(".gjf.inkly.json").read_text())
        assert "data" not in receipt


def test_cancelled_picker_does_not_configure_connect_or_write(workflow, monkeypatch):
    workflow.config.unlink()
    monkeypatch.setattr(picker, "choose_files", lambda: [])
    assert picker.main(["--mode", "run"]) == 0
    assert not workflow.connections
    assert not workflow.config.exists()
    assert not list(workflow.paths[0].parent.glob("*.json"))


def test_batch_submission_happens_after_all_previews_then_monitors_all(
    workflow, monkeypatch, capsys
):
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 2")
    assert picker.main(["--mode", "run"]) == 0
    assert actions(workflow) == [
        "prepare",
        "prepare",
        "submit",
        "submit",
        "status",
        "fetch",
        "status",
        "fetch",
    ]
    assert len(workflow.connections) == 1
    assert len(list((workflow.paths[0].parent / "returned").glob("*.log"))) == 2
    assert "4 CPUs, 2048 MiB total" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "y", "submit 1", "submit all"])
def test_only_exact_count_confirms(workflow, monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert picker.main(["--mode", "run"]) == 0
    assert actions(workflow) == ["prepare", "prepare"]


def test_show_scripts_then_decline_does_not_submit(workflow, monkeypatch, capsys):
    responses = iter(["s", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    assert picker.main(["--mode", "run"]) == 0
    assert capsys.readouterr().out.count("# synthetic script") == 2
    assert "submit" not in actions(workflow)


def test_noninteractive_run_cannot_submit(workflow, monkeypatch):
    monkeypatch.setattr(picker.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert picker.main(["--mode", "run"]) == 1
    assert "submit" not in actions(workflow)


def test_repreview_and_resume_keep_original_receipts_and_time(workflow, monkeypatch):
    assert picker.main(["--mode", "preview"]) == 0
    receipts = [
        path.with_suffix(".gjf.inkly.json").read_bytes() for path in workflow.paths
    ]
    workflow.calls.clear()
    assert picker.main(["--mode", "preview", "--hours", "8"]) == 0
    assert actions(workflow) == ["status", "status"]
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 2")
    assert picker.main(["--mode", "resume"]) == 0
    assert all(
        record["resources"]["hours"] == 2 for record in workflow.records.values()
    )
    assert [
        path.with_suffix(".gjf.inkly.json").read_bytes() for path in workflow.paths
    ] == receipts
    workflow.calls.clear()
    # A second resume checks remote state/digests, doesn't submit or overwrite logs.
    logs = list((workflow.paths[0].parent / "returned").glob("*.log"))
    times = [path.stat().st_mtime_ns for path in logs]
    assert picker.main(["--mode", "resume"]) == 0
    assert set(actions(workflow)) == {"status", "fetch"}
    assert [path.stat().st_mtime_ns for path in logs] == times


def test_changed_export_and_conflicting_result_are_preserved(workflow, monkeypatch):
    assert picker.main(["--mode", "preview"]) == 0
    original = workflow.paths[0].read_bytes()
    workflow.paths[0].write_bytes(original + b"changed")
    assert picker.main(["--mode", "run"]) == 1
    assert "submit" not in actions(workflow)
    workflow.paths[0].write_bytes(original)
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 2")
    output = workflow.paths[0].parent / "returned" / "sample_1.log"
    output.parent.mkdir()
    output.write_text("existing research result")
    assert picker.main(["--mode", "run"]) == 1
    assert output.read_text() == "existing research result"
    assert "submit" not in actions(workflow)


def test_preexisting_output_blocks_new_handoff_before_network(workflow):
    output = workflow.paths[0].parent / "returned" / "sample_1.log"
    output.parent.mkdir()
    output.write_text("existing research result")
    assert picker.main(["--mode", "preview"]) == 1
    assert not workflow.connections


def test_resume_never_overwrites_changed_returned_result(workflow, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 2")
    assert picker.main(["--mode", "run"]) == 0
    output = workflow.paths[0].parent / "returned" / "sample_1.log"
    output.write_text("researcher changed this result")
    workflow.calls.clear()
    assert picker.main(["--mode", "resume"]) == 1
    assert output.read_text() == "researcher changed this result"
    assert "submit" not in actions(workflow)


@pytest.mark.parametrize(
    "state", ["UNKNOWN", "SUBMISSION_UNKNOWN", "FAILED", "CANCELLED"]
)
def test_unresolved_job_blocks_all_new_submissions(workflow, state):
    assert picker.main(["--mode", "preview"]) == 0
    next(iter(workflow.records.values())).update(
        submission="SUBMISSION_UNKNOWN", state=state
    )
    workflow.calls.clear()
    assert picker.main(["--mode", "run"]) == 1
    assert actions(workflow) == ["status", "status"]


def test_partial_submit_disconnect_never_retries_and_preserves_receipts(
    workflow, monkeypatch
):
    original = bridge.Session.exchange

    def disconnect(self, request, **kwargs):
        response = original(self, request, **kwargs)
        if request["action"] == "submit":
            raise ValueError("Connection lost after possible submission")
        return response

    monkeypatch.setattr(bridge.Session, "exchange", disconnect)
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 2")
    assert picker.main(["--mode", "run"]) == 1
    assert actions(workflow) == ["prepare", "prepare", "submit"]
    assert all(path.with_suffix(".gjf.inkly.json").exists() for path in workflow.paths)
    monkeypatch.setattr(bridge.Session, "exchange", original)
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 1")
    assert picker.main(["--mode", "resume"]) == 0
    assert actions(workflow).count("submit") == 2
    assert actions(workflow).count("prepare") == 2


def test_invalid_selection_is_rejected_before_network_or_receipts(workflow):
    missing = workflow.paths[0].parent / "missing.gjf"
    assert (
        picker.main(
            ["--mode", "preview", "--files", str(workflow.paths[0]), str(missing)]
        )
        == 1
    )
    assert not workflow.connections
    assert not list(workflow.paths[0].parent.glob("*.json"))
    assert picker.selections([workflow.paths[0], workflow.paths[0]]) == [
        workflow.paths[0]
    ]


def test_resume_missing_receipt_and_output_collision_fail_before_network(workflow):
    assert picker.main(["--mode", "resume"]) == 1
    assert not workflow.connections
    second = workflow.paths[0].with_suffix(".com")
    second.write_bytes(workflow.paths[0].read_bytes())
    assert (
        picker.main(
            ["--mode", "preview", "--files", str(workflow.paths[0]), str(second)]
        )
        == 1
    )
    assert not workflow.connections


def test_mixed_hosts_rejected_before_connect(workflow):
    assert picker.main(["--mode", "preview"]) == 0
    path = workflow.paths[0].with_suffix(".gjf.inkly.json")
    receipt = json.loads(path.read_text())
    path.write_text(json.dumps({**receipt, "host": "another@host"}))
    workflow.connections.clear()
    assert picker.main(["--mode", "resume"]) == 1
    assert not workflow.connections


def test_authentication_restores_environment_on_failure(monkeypatch):
    monkeypatch.setenv("SSH_ASKPASS", "existing-approved-helper.exe")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "prefer")
    with pytest.raises(RuntimeError):
        with picker.authentication({"auth": "keyboard-interactive"}):
            assert picker.os.environ["SSH_ASKPASS_REQUIRE"] == "force"
            raise RuntimeError("synthetic failure")
    assert picker.os.environ["SSH_ASKPASS"] == "existing-approved-helper.exe"
    assert picker.os.environ["SSH_ASKPASS_REQUIRE"] == "prefer"


@pytest.mark.parametrize(
    "change",
    [
        {"host": "-bad"},
        {"ink_path": "~/bad;command"},
        {"hours": 0},
        {"hours": 337},
        {"auth": "password"},
    ],
)
def test_invalid_configuration_is_not_saved(tmp_path, change):
    path = tmp_path / "config.json"
    with pytest.raises(ValueError):
        picker.save_settings(path, {**SETTINGS, **change})
    assert not path.exists()


def test_session_routes_bundle_and_rejects_mixed_target_or_wrong_response():
    receipt = {**SETTINGS, "bundle": "a" * 32}
    second = {**SETTINGS, "bundle": "b" * 32}
    session = bridge.Session.__new__(bridge.Session)
    session.receipt = receipt
    session.process = SimpleNamespace(stdin=io.BytesIO(), kill=lambda: None)
    session.responses = queue.Queue()
    session.responses.put(json.dumps({"ok": True, "bundle": second["bundle"]}).encode())
    assert (
        session.exchange({"action": "status"}, receipt=second)["bundle"]
        == second["bundle"]
    )
    assert json.loads(session.process.stdin.getvalue())["bundle"] == second["bundle"]
    with pytest.raises(ValueError, match="mix connection"):
        session.exchange({"action": "status"}, receipt={**second, "host": "other"})
    session.responses.put(
        json.dumps({"ok": True, "bundle": receipt["bundle"]}).encode()
    )
    with pytest.raises(ValueError, match="bundle does not match"):
        session.exchange({"action": "status"}, receipt=second)


@pytest.mark.skipif(os.name != "posix", reason="HPC endpoint requires Linux")
def test_two_files_through_real_session_and_endpoint_with_mocked_slurm(
    tmp_path, monkeypatch
):
    paths = [tmp_path / f"sample_{number}.gjf" for number in (1, 2)]
    for path in paths:
        path.write_bytes(
            b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
        )
    peer = r"""
import json, os, subprocess, sys
from pathlib import Path
from inkly.gaussian import tensor
os.environ['INKLY_CLUSTER_PROFILE_NAME'] = 'cuttlefish'
root = Path(sys.argv[1]) / 'bundles'
original_handle = tensor.handle
tensor.handle = lambda request: original_handle(request, root)
def scheduler(args, directory):
    record = json.loads((directory / 'receipt.json').read_text())
    job = str(100 + int(Path(record['name']).stem.rsplit('_', 1)[1]))
    if args[0] == 'sbatch':
        count = directory / 'synthetic-submit-count'
        count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
        (directory / Path(record['name']).with_suffix('.log')).write_bytes(b'Normal termination of Gaussian\n')
        return subprocess.CompletedProcess(args, 0, job + '\n', '')
    return subprocess.CompletedProcess(args, 0, job + '|COMPLETED|0:0|\n', '')
tensor._scheduler = scheduler
sys.exit(tensor.main(session=True))
"""
    popen, starts = subprocess.Popen, []

    def start(args, **kwargs):
        starts.append(args)
        return popen([sys.executable, "-u", "-c", peer, str(tmp_path)], **kwargs)

    monkeypatch.setattr(bridge.subprocess, "Popen", start)
    monkeypatch.setattr(picker.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "submit 2")
    items = picker.plan(paths, SETTINGS, "run")
    picker.handoff(items, SETTINGS, "run", "ssh")
    assert len(starts) == 1
    assert len(list((tmp_path / "returned").glob("*.log"))) == 2
    assert [
        path.read_text()
        for path in (tmp_path / "bundles").glob("*/synthetic-submit-count")
    ] == ["1", "1"]
    # Resume checks/fetches both existing jobs through one additional connection.
    picker.handoff(picker.plan(paths, SETTINGS, "resume"), SETTINGS, "resume", "ssh")
    assert len(starts) == 2
    assert [
        path.read_text()
        for path in (tmp_path / "bundles").glob("*/synthetic-submit-count")
    ] == ["1", "1"]
