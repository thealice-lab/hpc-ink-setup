"""Run the real PowerShell 5.1 helper against isolated offline fixtures."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows helper")
ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/windows/get_inklydesktop_test.ps1"
SYNTHETIC = b"synthetic fixture, never executed"
DIGEST = hashlib.sha256(SYNTHETIC).hexdigest()
BUILD = "Inkly/DesktopBuilds/f5c9b926"
RUN = "https://github.com/thealice-lab/hpc-ink-setup/actions/runs/43600766207"


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def sandbox(tmp_path):
    local = tmp_path / "local app data"
    local.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    return {
        **os.environ,
        "LOCALAPPDATA": str(local),
        "TEMP": str(temporary),
        "TMP": str(temporary),
    }


def run_ps(tmp_path, sandbox, body):
    script = tmp_path / "scenario.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". {ps_quote(HELPER)}\n"
        "try {\n"
        + body
        + "\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        env=sandbox,
        capture_output=True,
        text=True,
        timeout=45,
    )


def artifact(tmp_path, names=("InklyDesktop.exe",), data=SYNTHETIC):
    path = tmp_path / "artifact with spaces.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, data)
    return path


def target(sandbox):
    return Path(sandbox["LOCALAPPDATA"]) / BUILD / "InklyDesktop.exe"


def fake_smoke():
    return """
function Invoke-InklySmokeTest($Exe, $ReportFile) {
    Assert-InklyExeHash $Exe
    [IO.File]::WriteAllText($ReportFile, '{"window":"Inkly Desktop","frozen":true,"askpass":true,"ssh":true}')
}
function Start-Process($FilePath) { Write-Output ('LAUNCH:' + $FilePath) | Out-Host }
"""


def test_windows_51_parser(tmp_path, sandbox):
    result = run_ps(
        tmp_path,
        sandbox,
        f"""
$tokens = $null; $errors = $null
[Management.Automation.Language.Parser]::ParseFile({ps_quote(HELPER)}, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count) {{ throw ($errors | Out-String) }}
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) {{ throw 'Expected Windows PowerShell 5.1' }}
""",
    )
    assert result.returncode == 0, result.stderr


def test_hash_verification_without_get_file_hash_module(tmp_path, sandbox):
    binary = tmp_path / "verified fixture.exe"
    binary.write_bytes(SYNTHETIC)
    result = run_ps(
        tmp_path,
        sandbox,
        f"""
function Get-FileHash {{ throw 'Get-FileHash is unavailable in this session' }}
$expectedSha = '{DIGEST}'
Assert-InklyExeHash {ps_quote(binary)}
""",
    )
    assert result.returncode == 0, result.stderr
    assert "SHA-256 verified" in result.stdout


@pytest.mark.parametrize("cached", [False, True])
def test_wrong_hash_never_runs_or_overwrites_existing_files(tmp_path, sandbox, cached):
    exe = target(sandbox)
    exe.parent.mkdir(parents=True)
    config = Path(sandbox["LOCALAPPDATA"]) / "Inkly/tensor-picker.json"
    config.write_text("existing configuration", encoding="utf-8")
    if cached:
        exe.write_bytes(b"existing unverified executable")
    result = run_ps(
        tmp_path,
        sandbox,
        "function Start-Process { throw 'Must never run a process' }\n"
        f"Invoke-InklyDesktopTest -Zip {ps_quote(artifact(tmp_path))}",
    )
    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert config.read_text() == "existing configuration"
    assert (
        exe.read_bytes() == b"existing unverified executable"
        if cached
        else not exe.exists()
    )
    assert not list(Path(sandbox["TEMP"]).glob("inkly-desktop-*"))


@pytest.mark.parametrize("names", [(), ("nested/InklyDesktop.exe",), ("other.exe",)])
def test_incorrect_archive_layout_is_rejected(tmp_path, sandbox, names):
    result = run_ps(
        tmp_path,
        sandbox,
        f"Invoke-InklyDesktopTest -Zip {ps_quote(artifact(tmp_path, names))} -SkipLaunch",
    )
    assert result.returncode == 1
    assert "exactly one InklyDesktop.exe" in result.stderr
    assert not target(sandbox).exists()


def test_verified_copy_reuse_explicit_zip_and_preservation(tmp_path, sandbox):
    zip_path = artifact(tmp_path, ("InklyDesktop.exe", "../unwanted.txt"))
    local = Path(sandbox["LOCALAPPDATA"])
    config = local / "Inkly/tensor-picker.json"
    config.parent.mkdir()
    config.write_text("keep settings")
    other = local / "Inkly/DesktopBuilds/other/InklyDesktop.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other build")
    result = run_ps(
        tmp_path,
        sandbox,
        f"$expectedSha = '{DIGEST}'\n"
        + fake_smoke()
        + f"""
Invoke-InklyDesktopTest -Zip {ps_quote(zip_path)}
Invoke-InklyDesktopTest -SkipLaunch
# An explicitly supplied ZIP must be validated even when a cache is present.
""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("LAUNCH:") == 1
    assert target(sandbox).read_bytes() == SYNTHETIC
    assert config.read_text() == "keep settings"
    assert other.read_bytes() == b"other build"
    assert len(list(target(sandbox).parent.glob("local-smoke-test-*.json"))) == 2
    assert not list(Path(sandbox["TEMP"]).rglob("unwanted.txt"))
    bad = artifact(tmp_path, data=b"wrong explicit replacement")
    result = run_ps(
        tmp_path,
        sandbox,
        f"$expectedSha = '{DIGEST}'\n"
        + fake_smoke()
        + f"Invoke-InklyDesktopTest -Zip {ps_quote(bad)}",
    )
    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert "LAUNCH:" not in result.stdout
    assert target(sandbox).read_bytes() == SYNTHETIC


def browser_scenario(zip_path):
    return f"""
function Find-InklyArtifactZip {{ return $null }}
function Read-Host {{ return {ps_quote(zip_path)} }}
function Start-Process($FilePath) {{ Write-Host ('OPEN:' + $FilePath) }}
"""


@pytest.mark.parametrize(
    "gh_status", ["absent", "unauthenticated", "failed", "success"]
)
def test_browser_and_cli_always_use_pinned_run(tmp_path, sandbox, gh_status):
    zip_path = artifact(tmp_path)
    setup = "function Get-Command { return $null }\n"
    if gh_status != "absent":
        cli = tmp_path / "fake-gh.cmd"
        binary = tmp_path / "synthetic.exe"
        binary.write_bytes(SYNTHETIC)
        log = tmp_path / "gh arguments.txt"
        sandbox["INKLY_TEST_EXE"] = str(binary)
        sandbox["INKLY_TEST_ARGS"] = str(log)
        # Simulate a partial CLI download before a browser retry as well.
        cli.write_text(
            "@echo off\n"
            'echo %* >> "%INKLY_TEST_ARGS%"\n'
            f'if "%1"=="auth" exit /b {1 if gh_status == "unauthenticated" else 0}\n'
            'copy /b "%INKLY_TEST_EXE%" "%~9\\InklyDesktop.exe" > nul\n'
            f"exit /b {1 if gh_status == 'failed' else 0}\n"
        )
        setup = f"function Get-Command {{ return [pscustomobject]@{{ Source={ps_quote(cli)} }} }}\n"
    result = run_ps(
        tmp_path,
        sandbox,
        f"$expectedSha = '{DIGEST}'\n"
        + fake_smoke()
        + browser_scenario(zip_path)
        + setup
        + "Invoke-InklyDesktopTest -SkipLaunch",
    )
    assert result.returncode == 0, result.stderr
    assert target(sandbox).read_bytes() == SYNTHETIC
    assert ("OPEN:" + RUN in result.stdout) == (gh_status != "success")
    if gh_status in {"failed", "success"}:
        args = log.read_text()
        assert (
            "run download 43600766207 -R thealice-lab/hpc-ink-setup -n InklyDesktop-windows"
            in args
        )
    assert not list(Path(sandbox["TEMP"]).glob("inkly-desktop-*"))


@pytest.mark.parametrize(
    "change",
    [
        {"frozen": False},
        {"frozen": "false"},
        {"askpass": False},
        {"window": "wrong window"},
        {"ssh": False},
        {"ssh": "false"},
    ],
)
def test_bad_smoke_report_prevents_gui_launch(tmp_path, sandbox, change):
    report = {"frozen": True, "askpass": True, "window": "Inkly Desktop", "ssh": True}
    report.update(change)
    exe = target(sandbox)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(SYNTHETIC)
    result = run_ps(
        tmp_path,
        sandbox,
        f"""
$expectedSha = '{DIGEST}'
function Start-Process($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru) {{
    if (-not $ArgumentList) {{ throw 'Unexpected GUI launch' }}
    [IO.File]::WriteAllText($ArgumentList[1].Trim('"'), {ps_quote(json.dumps(report))})
    $p = [pscustomobject]@{{ ExitCode=0 }}
    $p | Add-Member ScriptMethod WaitForExit {{ return $true }}
    $p | Add-Member ScriptMethod Dispose {{ }}
    return $p
}}
Invoke-InklyDesktopTest
""",
    )
    assert result.returncode == 1
    assert "Unexpected GUI launch" not in result.stderr
    assert (
        "OpenSSH Client is unavailable"
        if "ssh" in change
        else "packaging checks failed"
    ) in result.stderr


def test_checkout_destination_refused_before_download(tmp_path, sandbox):
    (Path(sandbox["LOCALAPPDATA"]) / ".git").write_text("gitdir: elsewhere")
    result = run_ps(tmp_path, sandbox, "Invoke-InklyDesktopTest")
    assert result.returncode == 1
    assert "inside a Git checkout" in result.stderr
    assert not target(sandbox).exists()


@pytest.mark.parametrize("outcome", ["missing", "timeout"])
def test_smoke_failure_never_uses_stale_report_or_launches(tmp_path, sandbox, outcome):
    exe = target(sandbox)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(SYNTHETIC)
    (exe.parent / "local-smoke-test-old.json").write_text(
        '{"frozen":true,"askpass":true,"ssh":true,"window":"Inkly Desktop"}'
    )
    killed = tmp_path / "killed.txt"
    result = run_ps(
        tmp_path,
        sandbox,
        f"""
$expectedSha = '{DIGEST}'
function taskkill.exe {{ [IO.File]::WriteAllText({ps_quote(killed)}, ($args -join ' ')) }}
function Start-Process($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru) {{
    if (-not $ArgumentList) {{ throw 'Unexpected GUI launch' }}
    $p = [pscustomobject]@{{ ExitCode=0; Id=123456 }}
    $p | Add-Member ScriptMethod WaitForExit {{ return ${"false" if outcome == "timeout" else "true"} }}
    $p | Add-Member ScriptMethod Dispose {{ }}
    return $p
}}
Invoke-InklyDesktopTest
""",
    )
    assert result.returncode == 1
    assert "Unexpected GUI launch" not in result.stderr
    assert (
        "timed out" if outcome == "timeout" else "smoke test failed"
    ) in result.stderr
    if outcome == "timeout":
        assert killed.read_text() == "/PID 123456 /T /F"
    else:
        assert not killed.exists()


@pytest.mark.parametrize("layout", ["repository", "standalone"])
def test_cmd_layout_argument_and_exit_code_forwarding(tmp_path, sandbox, layout):
    launcher = tmp_path / "Get-InklyDesktop-Test.cmd"
    launcher.write_bytes((ROOT / launcher.name).read_bytes())
    implementation = tmp_path / (
        "scripts/windows/get_inklydesktop_test.ps1"
        if layout == "repository"
        else "get_inklydesktop_test.ps1"
    )
    implementation.parent.mkdir(parents=True, exist_ok=True)
    implementation.write_text(
        "param([string]$ArtifactZip, [switch]$NoLaunch)\n"
        "Write-Host ('ZIP:' + $ArtifactZip + ';NoLaunch:' + $NoLaunch)\nexit 17\n"
    )
    zip_path = tmp_path / "ZIP with spaces.zip"
    result = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            launcher.name,
            "-ArtifactZip",
            str(zip_path),
            "-NoLaunch",
        ],
        cwd=tmp_path,
        env=sandbox,
        capture_output=True,
        text=True,
        input="\n",
        timeout=30,
    )
    assert result.returncode == 17, result.stderr
    assert f"ZIP:{zip_path};NoLaunch:True" in result.stdout
