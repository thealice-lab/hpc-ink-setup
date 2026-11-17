"""What Inkly Desktop installs on the HPC: its bundled setup script and release commit."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

# Anonymous HTTPS clone of the public repository; a new user needs no GitHub access.
REPOSITORY = "https://github.com/thealice-lab/hpc-ink-setup.git"
# Where setup.sh installs the launcher, the same as a manual install.
LAUNCHER = "~/.inkly/bin/ink"
SCRIPT_NAME = "desktop_hpc_setup.sh"
BUILD_INFO_NAME = "desktop_build.json"
_HERE = Path(__file__).resolve().parent


def _bundled(name):
    """A data file beside this module, or inside the packaged application."""
    return Path(getattr(sys, "_MEIPASS", _HERE)) / name


def script():
    try:
        data = _bundled(SCRIPT_NAME).read_bytes()
    except OSError:
        raise ValueError("This Desktop is missing its HPC setup script.") from None
    # bash on the HPC needs LF line endings, whatever a Windows checkout used.
    return data.replace(b"\r\n", b"\n")


def ready():
    """For the packaged smoke test: the script and its release commit are bundled."""
    try:
        return bool(script()) and bool(target())
    except Exception:
        return False


def build_info():
    """Release tag and commit recorded by the build, or None for a source run."""
    try:
        info = json.loads(_bundled(BUILD_INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _git(*args):
    return subprocess.run(
        ["git", "-C", str(_HERE.parent), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def target():
    """The exact release this Desktop installs on the HPC: version and full commit.

    A packaged Desktop uses the commit its build recorded. A source run uses
    the checkout's committed HEAD; the HPC can only fetch it once it is on
    GitHub, and the setup says so if it is not.
    """
    from inkly import __version__

    if getattr(sys, "frozen", False):
        info = build_info() or {}
        commit, version = info.get("commit"), info.get("version")
        if commit is None:
            raise ValueError(
                "This Desktop build does not record which release it is, so it "
                "cannot set up the HPC. Use a Desktop release from GitHub."
            )
    else:
        try:
            commit = _git("rev-parse", "HEAD").strip()
            text = _git("show", "HEAD:inkly/__init__.py")
        except (OSError, subprocess.SubprocessError):
            raise ValueError(
                "Running Desktop from source needs Git to find the commit to install."
            ) from None
        match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
        version = match.group(1) if match else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Cannot tell which commit to install on the HPC.")
    if version != __version__:
        raise ValueError(
            f"The committed Inkly version ({version}) differs from this Desktop "
            f"({__version__}). Commit the version change, or restart Desktop."
        )
    return {"version": version, "commit": commit}
