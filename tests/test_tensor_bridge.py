"""Transport tests require no network, passwords, model or Gaussian license."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

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
