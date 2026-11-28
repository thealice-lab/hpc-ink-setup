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


def test_missing_remote_bundle_has_a_specific_recovery_error():
    receipt = {
        "host": "cluster",
        "ink_path": "/bin/ink",
        "bundle": "a" * 32,
    }
    raw = json.dumps(
        {"ok": False, "error": "Unknown bundle; prepare a new input first"}
    ).encode()
    with pytest.raises(bridge.MissingBundleError, match="Unknown bundle"):
        bridge._response(raw, receipt)


def test_other_remote_errors_are_not_classified_as_missing_bundles():
    receipt = {
        "host": "cluster",
        "ink_path": "/bin/ink",
        "bundle": "a" * 32,
    }
    raw = json.dumps({"ok": False, "error": "SSH authentication denied"}).encode()
    with pytest.raises(ValueError, match="authentication denied") as error:
        bridge._response(raw, receipt)
    assert not isinstance(error.value, bridge.MissingBundleError)


@pytest.mark.parametrize(
    "stdout,code", [(b"[]", 0), (b"not JSON", 255), (b'{"ok":true}', 255)]
)
def test_only_answered_refusals_are_remote_refusals(monkeypatch, stdout, code):
    """A refusal leaves the session usable; a broken reply never looks like one."""
    receipt = {"host": "cluster", "ink_path": "/bin/ink", "bundle": "a" * 32}
    raw = json.dumps({"ok": False, "error": "Slurm would refuse this script"})
    with pytest.raises(bridge.RemoteRefusal, match="refuse this script"):
        bridge._response(raw.encode(), receipt)
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, code, stdout),
    )
    with pytest.raises(ValueError) as error:
        bridge.exchange(receipt, {}, "ssh")
    assert not isinstance(error.value, bridge.RemoteRefusal)


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


def test_a_connection_lost_after_answers_is_not_reported_as_a_login_problem(
    monkeypatch,
):
    # The HPC answers once, then the connection drops (sleep, network change).
    peer = (
        "import json, sys\n"
        "line = sys.stdin.buffer.readline()\n"
        "print(json.dumps({'ok': True, **json.loads(line)}), flush=True)\n"
        "sys.stderr.write('client_loop: send disconnect: Connection reset')\n"
        "sys.exit(255)\n"
    )
    receipt = fake_remote(monkeypatch, peer)
    with bridge.Session(receipt, "ssh") as session:
        assert session.exchange({"action": "status"})["action"] == "status"
        with pytest.raises(ValueError) as error:
            session.exchange({"action": "status"})
    assert str(error.value) == bridge.LOST_CONNECTION
    assert "Submitted jobs keep running" in str(error.value)
    assert "matching HPC version" not in str(error.value)


def test_a_connection_that_never_answers_still_points_to_login_and_version(
    monkeypatch,
):
    receipt = fake_remote(monkeypatch, "import sys; sys.exit(1)")
    with bridge.Session(receipt, "ssh") as session:
        with pytest.raises(ValueError, match="check login and matching HPC version"):
            session.exchange({"action": "version"})


def test_a_connection_lost_after_setup_finished_is_reported_as_lost(monkeypatch):
    frames = [{"done": True, "ok": True, "version": "0.6.0", "commit": "a" * 40}]
    peer = (
        "import json, sys\n"
        f"print('INKLY-SETUP ' + json.dumps({frames[0]!r}), flush=True)\n"
        "sys.exit(255)\n"
    )
    receipt = fake_remote(monkeypatch, peer)
    with bridge.Session(receipt, "ssh", remote=setup_remote()) as session:
        session.run_setup(lambda frame: None)
        with pytest.raises(ValueError) as error:
            session.exchange({"action": "version"})
    assert str(error.value) == bridge.LOST_CONNECTION


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


SETUP_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts/desktop_hpc_setup.sh"
).read_bytes()


def setup_remote(**overrides):
    values = {
        "commit": "a" * 40,
        "version": "0.6.0",
        "repository": "https://github.com/thealice-lab/hpc-ink-setup.git",
        **overrides,
    }
    return bridge.setup_command(SETUP_SCRIPT, **values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"commit": "a" * 39},
        {"commit": "A" * 40},
        {"version": "0.6"},
        {"version": "0.6.0; rm"},
        {"mode": "force"},
        {"repository": "https://x/y.git'; touch z"},
        {"repository": "$(id)"},
    ],
)
def test_setup_command_refuses_values_that_are_not_shell_safe(overrides):
    with pytest.raises(ValueError):
        setup_remote(**overrides)


def test_setup_command_is_one_single_quoted_word_for_any_login_shell():
    command = setup_remote(mode="repair", shared_logging=False)
    assert command.startswith("/bin/bash -c '") and command.endswith("'")
    assert command.count("'") == 2 and "\n" not in command
    assert " M=repair; S=0; " in command
    assert len(command) < bridge.SETUP_COMMAND_LIMIT


def test_setup_command_refuses_a_script_too_large_for_windows():
    with pytest.raises(ValueError, match="too large"):
        bridge.setup_command(
            b"#" * 25000,
            commit="a" * 40,
            version="0.6.0",
            repository="https://github.com/o/r.git",
        )


def test_setup_command_survives_windows_argument_parsing():
    """ssh.exe parses its command line as this child does (list2cmdline on Windows)."""
    receipt = {"host": "alice@cluster", "ink_path": "~/.inkly/bin/ink"}
    remote = setup_remote()
    args = bridge._ssh_args(receipt, sys.executable, session=True, remote=remote)
    assert args[-2:] == ["alice@cluster", remote]
    echo = "import json, sys; sys.stdout.write(json.dumps(sys.argv[1:]))"
    result = subprocess.run(
        [sys.executable, "-c", echo, *args[1:]],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == args[1:]


def fake_remote(monkeypatch, peer):
    popen = subprocess.Popen
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda args, **kwargs: popen([sys.executable, "-u", "-c", peer], **kwargs),
    )
    return {"host": "cluster", "ink_path": "~/.inkly/bin/ink", "bundle": "a" * 32}


SETUP_PEER = r"""
import json, sys
print("Welcome to the cluster")
for frame in FRAMES:
    print("INKLY-SETUP " + json.dumps(frame), flush=True)
for line in sys.stdin.buffer:
    print(json.dumps({"ok": True, **json.loads(line)}), flush=True)
"""


def test_run_setup_skips_the_banner_reports_progress_then_hands_over(monkeypatch):
    frames = [
        {"stage": "preflight", "line": "bash 5.1"},
        {"stage": "install", "line": "Installing Inkly"},
        {"done": True, "ok": True, "version": "0.6.0", "commit": "a" * 40},
    ]
    receipt = fake_remote(monkeypatch, f"FRAMES = {frames!r}\n" + SETUP_PEER)
    seen = []
    with bridge.Session(receipt, "ssh", remote=setup_remote()) as session:
        done = session.run_setup(seen.append)
        assert session.exchange({"action": "version"})["action"] == "version"
    assert seen == frames[:2]
    assert done == frames[2]


def test_run_setup_raises_the_setup_scripts_own_message(monkeypatch):
    frames = [
        {"stage": "code", "line": "Updating"},
        {"done": True, "ok": False, "stage": "code", "error": "has local changes"},
    ]
    receipt = fake_remote(monkeypatch, f"FRAMES = {frames!r}\n" + SETUP_PEER)
    with bridge.Session(receipt, "ssh", remote=setup_remote()) as session:
        with pytest.raises(ValueError, match="^has local changes$"):
            session.run_setup(lambda frame: None)


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "print('INKLY-SETUP {\"stage\": \"code\"}'); print('stray')",
            "Unexpected output",
        ),
        ("print('INKLY-SETUP [1, 2]')", "Invalid progress"),
        ("print('INKLY-SETUP not json')", "Invalid progress"),
        (
            'print(\'INKLY-SETUP {"stage": "code"}\')',
            "closed before HPC setup finished",
        ),
    ],
)
def test_run_setup_stops_on_broken_progress(monkeypatch, body, message):
    receipt = fake_remote(monkeypatch, body + "\nimport time; time.sleep(0.5)")
    with bridge.Session(receipt, "ssh", remote=setup_remote()) as session:
        with pytest.raises(ValueError, match=message):
            session.run_setup(lambda frame: None)


def test_run_setup_gives_up_when_the_hpc_goes_quiet(monkeypatch):
    monkeypatch.setattr(bridge, "SETUP_IDLE_SECONDS", 0.5)
    receipt = fake_remote(monkeypatch, "import time; time.sleep(30)")
    with bridge.Session(receipt, "ssh", remote=setup_remote()) as session:
        with pytest.raises(ValueError, match="stopped responding"):
            session.run_setup(lambda frame: None)
    assert session.process.returncode is not None


def test_run_setup_explains_a_failed_sign_in(monkeypatch):
    receipt = fake_remote(
        monkeypatch,
        "import sys; sys.stderr.write('Permission denied private-marker'); sys.exit(255)",
    )
    with bridge.Session(receipt, "ssh", remote=setup_remote()) as session:
        with pytest.raises(ValueError, match="authentication denied") as error:
            session.run_setup(lambda frame: None)
    assert "private-marker" not in str(error.value)


@pytest.mark.parametrize(
    "stderr, kind",
    [
        (b"bash: /home/alice/.inkly/bin/ink: No such file or directory\n", "missing"),
        (b"/home/alice/.inkly/bin/ink: Command not found.\n", "missing"),
        (
            b"bash: /home/alice/.inkly/bin/ink: /home/alice/.inkly/venv/bin/python: "
            b"bad interpreter: No such file or directory\n",
            "missing",
        ),
        # A Python error about some other file is not a missing launcher.
        (b"FileNotFoundError: [Errno 2] No such file or directory: 'x.db'\n", "other"),
        (b"Host key verification failed.\n", "host"),
    ],
)
def test_closed_session_tells_a_missing_launcher_and_an_untrusted_host_apart(
    monkeypatch, stderr, kind
):
    popen = subprocess.Popen
    peer = f"import sys; sys.stderr.buffer.write({stderr!r}); sys.exit(127)"
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda args, **kwargs: popen([sys.executable, "-c", peer], **kwargs),
    )
    receipt = {"host": "cluster", "ink_path": "~/.inkly/bin/ink", "bundle": "a" * 32}
    with bridge.Session(receipt, "ssh") as session:
        with pytest.raises(ValueError) as error:
            session.exchange({"action": "version"})
    expected = {
        "missing": bridge.MissingLauncherError,
        "host": bridge.HostKeyError,
        "other": ValueError,
    }[kind]
    assert type(error.value) is expected
    if kind == "missing":
        assert "Set up Inkly on the HPC" in str(error.value)
    assert "alice" not in str(error.value)
