"""Transport tests require no network, passwords, model or Gaussian license."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "tensor_bridge", Path(__file__).resolve().parents[1] / "scripts/tensor_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def response(data=b"public synthetic output"):
    return {
        "name": "molecule_1.log",
        "data": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_atomic_retrieve_checks_digest_and_never_overwrites(tmp_path):
    value = response()
    path = bridge.retrieve(value, tmp_path)
    assert path.read_bytes() == b"public synthetic output"
    with pytest.raises(FileExistsError):
        bridge.retrieve(response(b"different"), tmp_path)
    assert path.read_bytes() == b"public synthetic output"
    value["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        bridge.retrieve(value, tmp_path)
    assert not list(tmp_path.glob(".tensor-*"))


@pytest.mark.parametrize("name", ["../bad.log", "C:\\bad.log", "-bad.log", "bad.gjf"])
def test_retrieve_rejects_untrusted_filename(name, tmp_path):
    value = response()
    value["name"] = name
    with pytest.raises(ValueError, match="filename"):
        bridge.retrieve(value, tmp_path)


def test_exchange_uses_native_ssh_host_verification_and_json_stdin(monkeypatch):
    receipt = {
        "host": "alice@cluster",
        "ink_path": "~/.inkly/bin/ink",
        "bundle": "a" * 32,
    }

    def run(args, **kwargs):
        assert args[0] == "ssh.exe"
        assert "StrictHostKeyChecking=yes" in args
        assert args[-2:] == ["alice@cluster", '"$HOME"/.inkly/bin/ink tensor']
        assert json.loads(kwargs["input"]) == {"action": "status"}
        assert kwargs["timeout"] == 180
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"ok": True, **receipt}).encode()
        )

    monkeypatch.setattr(bridge.subprocess, "run", run)
    assert bridge.exchange(receipt, {"action": "status"}, "ssh.exe")["ok"]


def test_timeout_keeps_recovery_receipt_without_retries(monkeypatch, tmp_path):
    source = tmp_path / "sample.gjf"
    source.write_text("# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n")
    receipt = tmp_path / "receipt.json"
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("ssh", 180)

    monkeypatch.setattr(bridge.subprocess, "run", timeout)
    assert (
        bridge.main(
            [
                "prepare",
                str(source),
                "--host",
                "alice@cluster",
                "--receipt",
                str(receipt),
                "--cpus",
                "2",
                "--memory-mib",
                "1024",
            ]
        )
        == 1
    )
    saved = json.loads(receipt.read_text())
    assert len(saved["bundle"]) == 32
    assert len(calls) == 1
    assert "data" not in saved


@pytest.mark.parametrize(
    "host,ink",
    [("-oProxyCommand=bad", "/bin/ink"), ("ok", "~/x;bad"), ("ok", "/tmp/../bin/ink")],
)
def test_exchange_rejects_shell_or_option_injection(host, ink):
    with pytest.raises(ValueError):
        bridge.exchange({"host": host, "ink_path": ink}, {}, "ssh")


def test_submit_requires_confirm():
    with pytest.raises(SystemExit):
        bridge.main(["submit", "receipt.json"])


def test_tensor_command_bypasses_model_and_conversation(monkeypatch):
    import sys

    from inkly import ink_core
    from inkly.gaussian import tensor

    monkeypatch.setattr(sys, "argv", ["ink", "tensor"])
    monkeypatch.setattr(tensor, "main", lambda: 42)
    monkeypatch.setattr(
        ink_core, "_load_config", lambda: pytest.fail("Loaded chat config")
    )
    assert ink_core.main() == 42


@pytest.mark.parametrize(
    "stdout,code", [(b"[]", 0), (b"not JSON", 255), (b'{"ok":true}', 255)]
)
def test_invalid_or_failed_ssh_response_is_reported(monkeypatch, stdout, code):
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, code, stdout),
    )
    with pytest.raises(ValueError):
        bridge.exchange(
            {"host": "cluster", "ink_path": "/bin/ink", "bundle": "a" * 32}, {}, "ssh"
        )


def test_session_carries_multiple_requests_over_one_process(monkeypatch):
    popen = subprocess.Popen
    started = []
    peer = (
        "import sys,json\n"
        "for line in sys.stdin.buffer:\n"
        " r=json.loads(line); print(json.dumps({'ok':True, **r}), flush=True)\n"
    )

    def start(args, **kwargs):
        assert args[-1] == '"$HOME"/.inkly/bin/ink tensor --session'
        assert "StrictHostKeyChecking=yes" in args
        started.append(args)
        return popen([sys.executable, "-u", "-c", peer], **kwargs)

    monkeypatch.setattr(bridge.subprocess, "Popen", start)
    receipt = {"host": "cluster", "ink_path": "~/.inkly/bin/ink", "bundle": "a" * 32}
    with bridge.Session(receipt, "ssh.exe") as session:
        assert session.exchange({"action": "prepare"})["action"] == "prepare"
        assert session.exchange({"action": "status"})["action"] == "status"
    assert len(started) == 1
    assert session.process.returncode == 0


def test_session_reports_auth_failure_without_echoing_remote_stderr(monkeypatch):
    popen = subprocess.Popen
    peer = "import sys; sys.stdin.buffer.readline(); sys.stderr.write('Permission denied private-marker'); sys.exit(255)"
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda args, **kwargs: popen([sys.executable, "-c", peer], **kwargs),
    )
    receipt = {"host": "cluster", "ink_path": "/bin/ink", "bundle": "a" * 32}
    with bridge.Session(receipt, "ssh") as session:
        with pytest.raises(ValueError, match="authentication denied") as error:
            session.exchange({"action": "status"})
    assert "private-marker" not in str(error.value)


def test_cuttlefish_auth_matches_existing_keyboard_interactive_helper():
    receipt = {
        "host": "cluster",
        "ink_path": "/bin/ink",
        "bundle": "a" * 32,
        "auth": "keyboard-interactive",
    }
    args = bridge._ssh_args(receipt, "ssh.exe", session=True)
    assert "PreferredAuthentications=keyboard-interactive" in args
    assert "PubkeyAuthentication=no" in args
    receipt["auth"] = "configured"
    assert "PubkeyAuthentication=no" not in bridge._ssh_args(receipt, "ssh.exe")


@pytest.fixture
def guided(monkeypatch, tmp_path):
    calls = []
    source = tmp_path / "molecule_1.gjf"
    source.write_text(
        "%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
    )
    preview = {
        "name": source.name,
        "stages": 1,
        "submission": "PREPARED",
        "script": "#!/bin/bash\n",
        "resources": {
            "cpus": 2,
            "cpus_source": "TENSOR export",
            "exported_cpus": [2],
            "exported_memory_mb": [768],
            "gaussian_memory_mb": 768,
            "slurm_memory_mib": 1024,
            "memory_source": "TENSOR export plus cluster headroom",
            "hours": 1,
        },
    }
    states = iter(["RUNNING", "COMPLETED"])

    class LocalSession:
        def __init__(self, receipt, ssh):
            self.receipt = receipt

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def exchange(self, request):
            calls.append(request)
            if request["action"] == "prepare":
                return preview
            if request["action"] == "submit":
                return {"job_id": "12345", "submission": "SUBMITTED"}
            if request["action"] == "fetch":
                return response()
            return {
                "state": next(states),
                "exit_code": "0:0",
                "job_id": "12345",
                "submission": "SUBMITTED",
            }

    monkeypatch.setattr(bridge, "Session", LocalSession)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return source, calls, preview


def test_guided_run_reuses_settings_confirms_once_and_returns_log(
    guided, monkeypatch, capsys
):
    source, calls, _ = guided
    original = source.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert bridge.main(["run", str(source), "--host", "cluster"]) == 0
    assert [c["action"] for c in calls] == [
        "prepare",
        "submit",
        "status",
        "status",
        "fetch",
    ]
    assert calls[0]["cpus"] is None and calls[0]["memory_mib"] is None
    assert calls[1]["confirm"] is True
    assert source.read_bytes() == original
    assert (
        source.parent / "returned/molecule_1.log"
    ).read_bytes() == b"public synthetic output"
    saved = json.loads(source.with_suffix(".gjf.inkly.json").read_text())
    assert "data" not in saved
    output = capsys.readouterr().out
    assert "Slurm total memory: 1024 MiB" in output
    assert "In TENSOR: Analysis & 3D view" in output


@pytest.mark.parametrize("answer", ["", "n", "yes please"])
def test_guided_default_and_other_answers_do_not_submit(guided, monkeypatch, answer):
    source, calls, _ = guided
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert bridge.main(["run", str(source), "--host", "cluster"]) == 0
    assert [c["action"] for c in calls] == ["prepare"]


def test_preview_is_noninteractive_and_never_submits(guided, monkeypatch):
    source, calls, _ = guided
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(
        "builtins.input", lambda *a: pytest.fail("Unexpected confirmation")
    )
    assert bridge.main(["run", str(source), "--host", "cluster", "--preview"]) == 0
    assert [c["action"] for c in calls] == ["prepare"]


def test_noninteractive_run_cannot_submit(guided, monkeypatch):
    source, calls, _ = guided
    monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert bridge.main(["run", str(source), "--host", "cluster"]) == 1
    assert [c["action"] for c in calls] == ["prepare"]


def test_resume_submitted_job_does_not_prepare_or_resubmit(guided, tmp_path):
    _, calls, _ = guided
    receipt = tmp_path / "saved.json"
    receipt.write_text(
        json.dumps({"host": "cluster", "ink_path": "/bin/ink", "bundle": "a" * 32})
    )
    assert bridge.main(["resume", str(receipt)]) == 0
    assert [c["action"] for c in calls] == ["status", "status", "fetch"]


@pytest.mark.parametrize(
    "state", ["SUBMISSION_UNKNOWN", "UNKNOWN", "FAILED", "CANCELLED", "COMPLETED"]
)
def test_resume_uncertain_or_failed_job_never_submits(
    guided, monkeypatch, tmp_path, state
):
    _, calls, _ = guided

    def status(self, request):
        calls.append(request)
        return {"state": state, "exit_code": "1:0", "submission": "SUBMISSION_UNKNOWN"}

    monkeypatch.setattr(bridge.Session, "exchange", status)
    receipt = tmp_path / "saved.json"
    receipt.write_text(
        json.dumps({"host": "cluster", "ink_path": "/bin/ink", "bundle": "a" * 32})
    )
    assert bridge.main(["resume", str(receipt)]) == 1
    assert all(c["action"] == "status" for c in calls)


def test_interrupt_keeps_receipt_and_does_not_submit(guided, monkeypatch):
    source, calls, _ = guided

    def stop(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", stop)
    assert bridge.main(["run", str(source), "--host", "cluster"]) == 130
    assert source.with_suffix(".gjf.inkly.json").exists()
    assert [c["action"] for c in calls] == ["prepare"]
