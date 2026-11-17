"""Desktop HPC setup script: a temporary HOME, a local repository standing in for
GitHub and a stub setup.sh. No network, Slurm, Gaussian or real installer."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not shutil.which("bash") or not shutil.which("flock"),
    reason="the setup script runs on Linux HPC login nodes",
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/desktop_hpc_setup.sh"
spec = importlib.util.spec_from_file_location(
    "tensor_bridge", Path(__file__).resolve().parents[1] / "scripts/tensor_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]

# Stands in for the repository's setup.sh: noisy on stdout and stderr, then it
# installs a fake launcher that answers the version handshake.
STUB_SETUP = r"""#!/usr/bin/env bash
set -euo pipefail
echo "Inkly setup"
echo "INKLY-SETUP {\"done\":true,\"ok\":true}"
echo "a warning on stderr" >&2
printf 'no trailing newline'; echo
[ -f "$HOME/fail-setup" ] && { echo "pip failed"; exit 3; }
mkdir -p "$HOME/.inkly/lib/inkly" "$HOME/.inkly/bin"
cp inkly/__init__.py "$HOME/.inkly/lib/inkly/__init__.py"
[ -e "$HOME/.inkly/config.toml" ] || cp config.toml "$HOME/.inkly/config.toml"
cp fake_ink.py "$HOME/.inkly/bin/ink"
chmod +x "$HOME/.inkly/bin/ink"
echo run >>"$HOME/setup-runs"
echo "Inkly setup complete."
"""

FAKE_INK = r"""#!/usr/bin/env python3
import json, pathlib, sys
lib = pathlib.Path.home() / ".inkly/lib/inkly/__init__.py"
version = lib.read_text().split('"')[1]
def answer(line):
    request = json.loads(line)
    print(json.dumps({"ok": True, "bundle": request["bundle"], "version": version,
                      "capabilities": ["prepare", "submit", "status", "fetch", "version"]}), flush=True)
if sys.argv[1:] == ["tensor", "--session"]:
    for line in sys.stdin:
        answer(line)
else:
    answer(sys.stdin.read())
"""

CONFIG = """[install]
user_space_only = true

[logging]
enabled = true

[logging.shared]
enabled = true
directory = "/scratch/example/shared"

[logging.history]
enabled = true
"""


def git(*args, cwd):
    return subprocess.run(
        [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare repository served over file://, like an anonymous GitHub clone."""
    work = tmp_path / "work"
    (work / "inkly").mkdir(parents=True)
    (work / "setup.sh").write_text(STUB_SETUP)
    (work / "fake_ink.py").write_text(FAKE_INK)
    (work / "config.toml").write_text(CONFIG)
    (work / ".gitignore").write_text("__pycache__/\n")
    git("init", "-q", "-b", "main", cwd=work)
    bare = tmp_path / "remote.git"

    def release(version):
        (work / "inkly/__init__.py").write_text(f'__version__ = "{version}"\n')
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", f"Inkly {version}", cwd=work)
        commit = git("rev-parse", "HEAD", cwd=work)
        git("push", "-q", str(bare), "main", cwd=work)
        return commit

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=bare)
    release.url = bare.as_uri()
    return release


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    (tmp_path / "pypi.html").write_text("ok")
    return path


def raw_command(**values):
    """The command shape without Desktop's checks, to test the script's own."""
    payload = base64.b64encode(SCRIPT.read_bytes()).decode()
    assignments = "; ".join(f"{key}={value}" for key, value in values.items())
    return (
        f"/bin/bash -c '{assignments}; P={payload}; "
        'eval "$(printf %s "$P" | base64 -d)"\''
    )


def command(*, C, V, R, M, S):
    """Exactly the remote command Desktop sends."""
    return bridge.setup_command(
        SCRIPT.read_bytes(),
        commit=C,
        version=V,
        repository=R,
        mode=M,
        shared_logging=bool(S),
    )


def run(
    home,
    url,
    commit,
    version,
    *,
    mode="auto",
    shared=1,
    shell="bash",
    requests=(),
    path=None,
):
    env = {
        "HOME": str(home),
        "PATH": path or os.environ["PATH"],
        "INKLY_SETUP_PYPI": (home.parent / "pypi.html").as_uri(),
    }
    stdin = "".join(
        json.dumps({"action": "version", "bundle": bundle}) + "\n"
        for bundle in requests
    )
    result = subprocess.run(
        [
            shutil.which(shell) or shell,
            "-c",
            command(C=commit, V=version, R=url, M=mode, S=shared),
        ],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    lines = result.stdout.splitlines()
    frames = [
        json.loads(line[12:]) for line in lines if line.startswith("INKLY-SETUP ")
    ]
    done = [frame for frame in frames if frame.get("done")]
    tail = lines[len(frames) :]
    return result, frames, (done[0] if done else None), tail


def checkout(home):
    return home / ".inkly/src/hpc-ink-setup"


def runs(home):
    path = home / "setup-runs"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_clean_install_frames_everything_then_continues_as_a_session(remote, home):
    commit = remote("0.6.0")
    result, frames, done, tail = run(
        home, remote.url, commit, "0.6.0", requests=["a" * 32, "b" * 32]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert done == {
        "done": True,
        "ok": True,
        "version": "0.6.0",
        "commit": commit,
        "changed": True,
        "log": done["log"],
    }
    assert done["log"].startswith("~/.inkly/logs/desktop-setup-")
    # Every line before "done" is a frame, including setup.sh's noisy output,
    # its stderr and a line that imitates a frame.
    output = result.stdout.splitlines()
    assert all(line.startswith("INKLY-SETUP ") for line in output[: len(frames)])
    assert output[len(frames) - 1] == "INKLY-SETUP " + json.dumps(
        done, separators=(",", ":")
    )
    lines = [frame.get("line", "") for frame in frames]
    assert "a warning on stderr" in lines
    assert 'INKLY-SETUP {"done":true,"ok":true}' in lines
    assert {frame["stage"] for frame in frames if not frame.get("done")} >= {
        "preflight",
        "lock",
        "code",
        "install",
        "verify",
    }
    # The same connection then answered both session requests.
    replies = [json.loads(line) for line in tail]
    assert [reply["bundle"] for reply in replies] == ["a" * 32, "b" * 32]
    assert all(reply["version"] == "0.6.0" for reply in replies)
    assert git("rev-parse", "HEAD", cwd=checkout(home)) == commit
    assert git("remote", "get-url", "origin", cwd=checkout(home)) == remote.url
    assert runs(home) == 1
    assert not list((home / ".inkly/src").glob(".hpc-ink-setup.*"))
    log = (home / ".inkly/logs" / done["log"].rsplit("/", 1)[1]).read_text()
    assert "pip failed" not in log and "Inkly setup complete." in log


def test_same_version_changes_nothing_and_repair_reruns_setup(remote, home):
    commit = remote("0.6.0")
    assert run(home, remote.url, commit, "0.6.0")[2]["changed"] is True
    result, frames, done, _ = run(
        home, remote.url, commit, "0.6.0", requests=["c" * 32]
    )
    assert done["ok"] and done["changed"] is False
    assert runs(home) == 1
    assert any("already installed" in frame.get("line", "") for frame in frames)
    done = run(home, remote.url, commit, "0.6.0", mode="repair")[2]
    assert done["ok"] and done["changed"] is True
    assert runs(home) == 2


def test_update_fetches_the_new_commit_and_reruns_setup(remote, home):
    first = remote("0.6.0")
    run(home, remote.url, first, "0.6.0")
    second = remote("0.6.1")
    result, frames, done, tail = run(
        home, remote.url, second, "0.6.1", requests=["d" * 32]
    )
    assert done["ok"] and done["changed"] is True, frames
    assert git("rev-parse", "HEAD", cwd=checkout(home)) == second
    assert json.loads(tail[0])["version"] == "0.6.1"
    assert runs(home) == 2


def test_an_interrupted_update_is_completed_on_the_next_run(remote, home):
    commit = remote("0.6.0")
    run(home, remote.url, commit, "0.6.0")
    (home / ".inkly/lib/inkly/__init__.py").write_text('__version__ = "0.5.9"\n')
    done = run(home, remote.url, commit, "0.6.0")[2]
    assert done["ok"] and done["changed"] is True
    assert runs(home) == 2


def failure(result, done, text):
    assert result.returncode != 0
    assert done["ok"] is False and text in done["error"], done
    # On failure nothing but frames reaches Desktop, and no session starts.
    assert all(line.startswith("INKLY-SETUP ") for line in result.stdout.splitlines())


def test_never_downgrades_an_installed_copy(remote, home):
    commit = remote("0.6.0")
    lib = home / ".inkly/lib/inkly"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text('__version__ = "0.7.0"\n')
    result, _, done, _ = run(home, remote.url, commit, "0.6.0")
    failure(result, done, "newer than this Desktop")
    assert not checkout(home).exists() and runs(home) == 0


def test_never_downgrades_the_checkout(remote, home):
    old = remote("0.6.0")
    new = remote("0.6.1")
    run(home, remote.url, new, "0.6.1")
    result, _, done, _ = run(home, remote.url, old, "0.6.0")
    failure(result, done, "newer than this Desktop")
    assert git("rev-parse", "HEAD", cwd=checkout(home)) == new


def test_local_changes_are_never_overwritten(remote, home):
    first = remote("0.6.0")
    run(home, remote.url, first, "0.6.0")
    (checkout(home) / "setup.sh").write_text("# my edit\n")
    (checkout(home) / "notes.txt").write_text("untracked work\n")
    second = remote("0.6.1")
    result, _, done, _ = run(home, remote.url, second, "0.6.1")
    failure(result, done, "has local changes")
    assert (checkout(home) / "setup.sh").read_text() == "# my edit\n"
    assert (checkout(home) / "notes.txt").exists()
    assert git("rev-parse", "HEAD", cwd=checkout(home)) == first


def test_a_checkout_of_another_repository_is_left_alone(remote, home, tmp_path):
    commit = remote("0.6.0")
    run(home, remote.url, commit, "0.6.0")
    git(
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/fork.git",
        cwd=checkout(home),
    )
    result, _, done, _ = run(home, remote.url, commit, "0.6.0")
    failure(result, done, "different repository")


def test_a_plain_folder_in_the_way_is_left_alone(remote, home):
    commit = remote("0.6.0")
    checkout(home).mkdir(parents=True)
    (checkout(home) / "mine.txt").write_text("keep\n")
    result, _, done, _ = run(home, remote.url, commit, "0.6.0")
    failure(result, done, "not an Inkly checkout")
    assert [p.name for p in checkout(home).iterdir()] == ["mine.txt"]


def test_a_commit_with_another_version_installs_nothing(remote, home):
    commit = remote("0.6.1")
    result, _, done, _ = run(home, remote.url, commit, "0.6.0")
    failure(result, done, "is not Inkly 0.6.0")
    assert not checkout(home).exists()
    assert not list((home / ".inkly/src").glob(".hpc-ink-setup.*"))


def test_a_commit_missing_from_the_repository_leaves_no_partial_folder(remote, home):
    remote("0.6.0")
    result, _, done, _ = run(home, remote.url, "1" * 40, "0.6.0")
    failure(result, done, "push that commit first")
    assert not checkout(home).exists()
    assert not list((home / ".inkly/src").glob(".hpc-ink-setup.*"))


def test_a_held_lock_stops_a_second_setup(remote, home):
    commit = remote("0.6.0")
    (home / ".inkly").mkdir()
    lock = home / ".inkly/desktop-setup.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    try:
        for _ in range(50):
            if lock.exists():
                break
            subprocess.run(["sleep", "0.1"])
        result, _, done, _ = run(home, remote.url, commit, "0.6.0")
    finally:
        holder.kill()
        holder.wait()
    failure(result, done, "Another Inkly setup is already running")
    assert not checkout(home).exists()


def test_setup_failure_keeps_the_checkout_and_points_to_the_log(remote, home):
    commit = remote("0.6.0")
    (home / "fail-setup").write_text("")
    result, frames, done, _ = run(home, remote.url, commit, "0.6.0")
    failure(result, done, "setup.sh stopped with exit code 3")
    assert "Details: ~/.inkly/logs/desktop-setup-" in done["error"]
    assert any(frame.get("line") == "pip failed" for frame in frames)
    assert git("rev-parse", "HEAD", cwd=checkout(home)) == commit
    (home / "fail-setup").unlink()
    assert run(home, remote.url, commit, "0.6.0")[2]["ok"]


def test_shared_logging_opt_out_only_writes_a_new_config(remote, home):
    commit = remote("0.6.0")
    done = run(home, remote.url, commit, "0.6.0", shared=0)[2]
    assert done["ok"]
    text = (home / ".inkly/config.toml").read_text()
    assert text == CONFIG.replace(
        "[logging.shared]\nenabled = true", "[logging.shared]\nenabled = false"
    )
    (home / ".inkly/config.toml").write_text(CONFIG)
    assert run(home, remote.url, commit, "0.6.0", mode="repair", shared=0)[2]["ok"]
    assert (home / ".inkly/config.toml").read_text() == CONFIG


def tools_without(tmp_path, *missing, extra=None):
    """A PATH that has the usual tools except those named; extra replaces some."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in [
        "git",
        "python3",
        "flock",
        "timeout",
        "sort",
        "sed",
        "awk",
        "grep",
        "tr",
        "od",
        "mktemp",
        "df",
        "date",
        "cat",
        "head",
        "tail",
        "mkdir",
        "mv",
        "rm",
        "base64",
        "printf",
        "cut",
        "dirname",
        "env",
        "bash",
        "sh",
        "chmod",
        "cp",
        "pwd",
        "sleep",
        "id",
    ]:
        real = shutil.which(name)
        if real and name not in missing:
            (bin_dir / name).symlink_to(real)
    for name, text in (extra or {}).items():
        (bin_dir / name).unlink(missing_ok=True)
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    return str(bin_dir)


@pytest.mark.parametrize("tool", ["git", "flock"])
def test_a_missing_tool_is_named_before_anything_is_created(
    remote, home, tmp_path, tool
):
    commit = remote("0.6.0")
    result, _, done, _ = run(
        home, remote.url, commit, "0.6.0", path=tools_without(tmp_path, tool)
    )
    failure(result, done, f"'{tool}' is not available")
    assert not (home / ".inkly").exists()


def test_old_python_is_reported_before_anything_is_created(remote, home, tmp_path):
    commit = remote("0.6.0")
    old = "#!/bin/sh\necho 'ModuleNotFoundError: No module named venv'\nexit 1\n"
    result, _, done, _ = run(
        home,
        remote.url,
        commit,
        "0.6.0",
        path=tools_without(tmp_path, extra={"python3": old}),
    )
    failure(result, done, "Python 3.9 or newer")
    assert "No module named venv" in done["error"]
    assert not (home / ".inkly").exists()


def test_low_disk_space_is_reported_before_anything_is_created(remote, home, tmp_path):
    commit = remote("0.6.0")
    df = "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\necho 'x 100 90 10 90% /'\n"
    result, _, done, _ = run(
        home,
        remote.url,
        commit,
        "0.6.0",
        path=tools_without(tmp_path, extra={"df": df}),
    )
    failure(result, done, "1 GB of free space")
    assert not (home / ".inkly").exists()


def test_an_unreachable_package_index_is_reported_first(remote, home):
    commit = remote("0.6.0")
    (home.parent / "pypi.html").unlink()
    result, _, done, _ = run(home, remote.url, commit, "0.6.0")
    failure(result, done, "Python package index")
    assert not (home / ".inkly").exists()


@pytest.mark.parametrize(
    "values",
    [
        {"C": "abc", "V": "0.6.0", "M": "auto", "S": 1},
        {"C": "a" * 40, "V": "0.6", "M": "auto", "S": 1},
        {"C": "a" * 40, "V": "0.6.0", "M": "force", "S": 1},
        {"C": "a" * 40, "V": "0.6.0", "M": "auto", "S": 2},
    ],
)
def test_invalid_values_from_desktop_are_refused(home, values):
    result = subprocess.run(
        ["bash", "-c", raw_command(R="file:///nowhere", **values)],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        timeout=30,
    )
    assert result.returncode != 0
    assert '"ok":false' in result.stdout and "invalid" in result.stdout
    assert not (home / ".inkly").exists()


@pytest.mark.parametrize("shell", ["sh", "dash", "tcsh", "csh", "zsh"])
def test_the_remote_command_works_from_other_login_shells(remote, home, shell):
    if not shutil.which(shell):
        pytest.skip(f"{shell} is not installed")
    commit = remote("0.6.0")
    result, _, done, tail = run(
        home, remote.url, commit, "0.6.0", shell=shell, requests=["e" * 32]
    )
    assert done["ok"], result.stdout + result.stderr
    assert json.loads(tail[0])["bundle"] == "e" * 32


def test_the_script_stays_well_inside_the_windows_command_line_limit():
    # Windows allows 32,767 characters per command line; base64 adds a third.
    longest = command(
        C="a" * 40, V="10.10.10", R="https://github.com/o/r.git", M="repair", S=1
    )
    assert len(longest) < 20000


def test_desktop_bridge_runs_setup_then_the_session_on_one_connection(
    remote, home, monkeypatch
):
    """The bridge's own command, through a login shell standing in for sshd."""
    commit = remote("0.6.0")
    popen = subprocess.Popen
    started = []

    def ssh(args, **kwargs):
        started.append(args)
        env = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "INKLY_SETUP_PYPI": (home.parent / "pypi.html").as_uri(),
        }
        # Login banners arrive before the remote command starts.
        wrapped = "echo 'Welcome to the cluster'; " + args[-1]
        return popen(["sh", "-c", wrapped], env=env, **kwargs)

    monkeypatch.setattr(bridge.subprocess, "Popen", ssh)
    receipt = {"host": "cluster", "ink_path": "~/.inkly/bin/ink", "auth": None}
    remote_command = command(C=commit, V="0.6.0", R=remote.url, M="auto", S=1)
    progress = []
    with bridge.Session(receipt, "ssh", remote=remote_command) as session:
        done = session.run_setup(progress.append)
        reply = session.exchange(
            {"action": "version"}, receipt={**receipt, "bundle": "f" * 32}
        )
    assert len(started) == 1 and started[0][-1] == remote_command
    assert done["ok"] and done["version"] == "0.6.0" and done["commit"] == commit
    assert reply["version"] == "0.6.0"
    assert any(frame.get("stage") == "install" for frame in progress)
    assert session.process.returncode == 0
