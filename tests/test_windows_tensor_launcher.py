"""Exercise the real PowerShell launcher without SSH, credentials or a job."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/windows/tensor.ps1"


def settings(tmp_path, **changes):
    value = {
        "host": "researcher@cluster.example",
        "ink_path": "~/.inkly/bin/ink",
        "auth": "configured",
        **changes,
    }
    path = tmp_path / "local settings.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def launch(*args, env=None):
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            *map(str, args),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )


def fake_python(tmp_path, exit_code=0):
    """Capture the actual argument array after the launcher's version check."""
    interpreter = tmp_path / "fake python.ps1"
    interpreter.write_text(
        "if ($args[0] -eq '-c') { exit 0 }\n"
        "[IO.File]::WriteAllText($env:INKLY_LAUNCHER_CAPTURE, "
        "(ConvertTo-Json -InputObject @($args) -Compress))\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    capture = tmp_path / "arguments.json"
    env = {**os.environ, "INKLY_LAUNCHER_CAPTURE": str(capture)}
    return interpreter, capture, env


def test_local_check_compiles_generic_dialog_without_connecting(tmp_path):
    config = settings(tmp_path, auth="keyboard-interactive")
    cache = tmp_path / "dialog cache"
    result = launch(
        "-Action",
        "check",
        "-ConfigPath",
        config,
        "-Python",
        sys.executable,
        "-CacheDirectory",
        cache,
    )
    assert result.returncode == 0, result.stderr
    assert "No network connection or job submission" in result.stdout
    assert len(list(cache.glob("InklySshAskpass-*.exe"))) == 1


def test_preview_preserves_paths_and_passes_settings_to_real_bridge(tmp_path):
    interpreter, capture, env = fake_python(tmp_path)
    source = tmp_path / "research with spaces" / "sample_1.gjf"
    output = tmp_path / "returned results"
    receipt = tmp_path / "my receipt.inkly.json"
    result = launch(
        "-Action",
        "preview",
        "-InputFile",
        source,
        "-ConfigPath",
        settings(tmp_path),
        "-Python",
        interpreter,
        "-Receipt",
        receipt,
        "-Out",
        output,
        "-Hours",
        "2",
        "-Cpus",
        "4",
        "-MemoryMiB",
        "2048",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads(capture.read_text())
    assert Path(args[0]).resolve() == LAUNCHER.parents[1] / "tensor_bridge.py"
    assert args[3:] == [
        "run",
        str(source),
        "--host",
        "researcher@cluster.example",
        "--ink-path",
        "~/.inkly/bin/ink",
        "--auth",
        "configured",
        "--hours",
        "2",
        "--preview",
        "--receipt",
        str(receipt),
        "--cpus",
        "4",
        "--memory-mib",
        "2048",
        "--out",
        str(output),
    ]


def test_resume_uses_receipt_without_new_config_and_propagates_failure(tmp_path):
    interpreter, capture, env = fake_python(tmp_path, exit_code=17)
    receipt = settings(tmp_path, bundle="a" * 32)
    result = launch(
        "-Action",
        "resume",
        "-Receipt",
        receipt,
        "-ConfigPath",
        tmp_path / "missing-new-run-config.json",
        "-Out",
        tmp_path / "recovered results",
        "-Python",
        interpreter,
        env=env,
    )
    assert result.returncode == 17, result.stderr
    args = json.loads(capture.read_text())
    assert args[3:] == [
        "resume",
        str(receipt),
        "--out",
        str(tmp_path / "recovered results"),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "YOUR_USERNAME@YOUR_HPC_HOST"},
        {"host": "-oProxyCommand=bad"},
        {"ink_path": "~/x;bad"},
        {"password": "synthetic-not-a-credential"},
    ],
)
def test_invalid_or_credential_settings_stop_before_running_python(tmp_path, changes):
    interpreter, capture, env = fake_python(tmp_path)
    result = launch(
        "-Action",
        "check",
        "-ConfigPath",
        settings(tmp_path, **changes),
        "-Python",
        interpreter,
        env=env,
    )
    assert result.returncode == 1
    assert not capture.exists()


def test_authentication_environment_is_restored_after_launcher_returns(tmp_path):
    config = settings(tmp_path, auth="keyboard-interactive")
    runner = tmp_path / "check and inspect.ps1"
    # Pass paths as parameters, never interpolate shell source with local paths.
    runner.write_text(
        "param($Launcher, $ConfigFile, $Interpreter, $Cache)\n"
        "& $Launcher -Action check -ConfigPath $ConfigFile -Python $Interpreter -CacheDirectory $Cache\n"
        "Write-Output ('restored:' + $env:SSH_ASKPASS + '|' + $env:SSH_ASKPASS_REQUIRE + '|' + $env:INKLY_SSH_TARGET)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
            str(LAUNCHER),
            str(config),
            sys.executable,
            str(tmp_path / "cache"),
        ],
        env={
            **os.environ,
            "SSH_ASKPASS": "previous-dialog",
            "SSH_ASKPASS_REQUIRE": "previous-setting",
            "INKLY_SSH_TARGET": "previous-target",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert "restored:previous-dialog|previous-setting|previous-target" in result.stdout
