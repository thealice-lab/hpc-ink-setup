#!/usr/bin/env python3
"""Exercise setup in a new private HOME; never reuse the operator's Inkly data.

Linux only. Leaves the sandbox and private command log available for inspection.
No Slurm jobs, scraper build, SSH configuration, or shared storage provisioning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


REPOSITORY = "https://github.com/thealice-lab/hpc-ink-setup.git"


def isolated_environment(
    home: Path,
    python: Path,
    *,
    shared_root: Path | None = None,
    exceptions: Path | None = None,
) -> dict[str, str]:
    """Allow only system executables; discard caches, credentials and overrides."""
    if exceptions is not None and shared_root is None:
        raise ValueError("A reviewed exceptions file requires an explicit shared root")
    child = {
        "HOME": str(home),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHON": str(python),
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    # Opt in only through explicit CLI inputs, never inherited operator settings.
    # setup.sh validates/copies from this root; it cannot build a corpus.
    if shared_root is not None:
        child["INKLY_GAUSSIAN_SHARED_ROOT"] = str(shared_root.resolve())
    if exceptions is not None:
        child["INKLY_GAUSSIAN_EXCEPTIONS"] = str(exceptions.resolve())
    return child


def isolated_config(template: str) -> str:
    """Fail closed if the template changes rather than accidentally share logs."""
    for section, key in (
        ("install", "allow_modify_shell_rc"),
        ("install", "allow_path_injection"),
        ("logging", "enabled"),
        ("logging.shared", "enabled"),
        ("logging.shared", "archive_conversations"),
        ("logging.shared", "share_conversations"),
    ):
        pattern = (
            rf"(?ms)(^\[{re.escape(section)}\]\n(?:(?!^\[).)*?^{key}\s*=\s*)true[ \t]*$"
        )
        template, count = re.subn(pattern, r"\g<1>false", template)
        if count != 1:
            raise ValueError(f"Cannot safely isolate configuration: {section}.{key}")
    return template


def fingerprint(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


# Executed only in the newly installed interpreter, outside the source checkout.
# No source passage, user prompt, model answer or configuration secret is printed.
RUNTIME_PROBE = r"""
import json, pathlib, sqlite3, sys
home = pathlib.Path.home() / ".inkly"
sys.path.insert(0, str(home / "lib"))
import inkly
from inkly.cluster_profile import resolve_cluster_profile
from inkly.config import TomlParser
from gaussian_scraper.config import DEFAULT_OUTPUT_DIR
from gaussian_scraper.search import search_docs
cfg = TomlParser(home / "config.toml").load()
profile = resolve_cluster_profile()
db = pathlib.Path(DEFAULT_OUTPUT_DIR) / "gaussian.db"
result = {
    "runtime_is_private": pathlib.Path(inkly.__file__).parent == home / "lib" / "inkly",
    "interpreter_is_private": pathlib.Path(sys.prefix) == home / "venv",
    "shared_logging_disabled": not cfg.logging.enabled and not cfg.logging.shared.enabled,
    "shell_changes_disabled": not cfg.install.allow_modify_shell_rc and not cfg.install.allow_path_injection,
    "profile": profile.get("name") if profile else None,
    "model": cfg.llm.model,
    "gaussian_database_is_private": db == home / "gaussian.db",
    "gaussian_database_exists": db.is_file(),
    "gaussian_integrity_ok": False,
    "gaussian_search_matches": 0,
    "jobs_database_separate": (home / "jobs.db").is_file() and db != home / "jobs.db",
}
if db.is_file():
    try:
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
            result["gaussian_integrity_ok"] = connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        result["gaussian_search_matches"] = sum(m.score > 0 for m in search_docs("gaussian", "Gaussian geometry optimization", top_k=5))
    except (OSError, ValueError, sqlite3.DatabaseError):
        result["gaussian_check_failed"] = True
print(json.dumps(result))
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref", default="dev", help="branch/tag to clone (default: dev)"
    )
    parser.add_argument(
        "--parent", type=Path, help="private test parent (default: system temp)"
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--shared-root",
        type=Path,
        help="explicit approved existing snapshot root (read/copy only; never builds)",
    )
    parser.add_argument(
        "--exceptions", type=Path, help="reviewed omissions for the exact config hash"
    )
    parser.add_argument(
        "--check-model",
        action="store_true",
        help="send one synthetic prompt using the template model",
    )
    args = parser.parse_args(argv)
    if args.exceptions is not None and args.shared_root is None:
        parser.error("--exceptions requires --shared-root")
    if os.name != "posix":
        parser.error("Run this validator on Linux, preferably Cuttlefish.")
    python = args.python.absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        parser.error("--python must name an executable Python interpreter")

    original_home = Path.home()
    protected = [
        original_home / ".bashrc",
        original_home / ".inkly" / "config.toml",
        original_home / ".inkly" / "bin" / "ink",
    ]
    before = [fingerprint(path) for path in protected]
    previous_umask = os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="inkly-fresh-", dir=args.parent)).resolve()
    home = root / "home"
    home.mkdir(mode=0o700)
    repository = root / "repo"
    env = isolated_environment(
        home, python, shared_root=args.shared_root, exceptions=args.exceptions
    )
    report = {
        "validation": "isolated-same-account",
        "cross_account_verified": False,
        "model_checked": False,
        "shared_corpus_requested": args.shared_root is not None,
    }
    print(f"Isolated test directory: {root}", flush=True)

    def run(
        label, command, *, cwd=root, accepted=(0,), timeout=1200, expected_stderr=None
    ):
        print(f"Checking {label}", flush=True)
        with (root / "commands.log").open("a", encoding="utf-8") as log:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            log.write(
                f"\n[{label}] exit={result.returncode}\n{result.stdout}\n{result.stderr}\n"
            )
        if result.returncode not in accepted:
            raise RuntimeError(
                f"{label} failed (exit {result.returncode}); inspect the private command log"
            )
        if expected_stderr is not None and result.stderr.strip() != expected_stderr:
            raise RuntimeError(f"{label} did not reach the expected CLI entry point")
        return result.stdout.strip()

    try:
        run(
            "clone",
            [
                "git",
                "clone",
                "--branch",
                args.ref,
                "--single-branch",
                REPOSITORY,
                str(repository),
            ],
        )
        report["commit"] = run(
            "source commit", ["git", "rev-parse", "HEAD"], cwd=repository
        )
        state = home / ".inkly"
        state.mkdir(mode=0o700)
        config = state / "config.toml"
        config.write_text(
            isolated_config((repository / "config.toml").read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        config_before = fingerprint(config)
        for attempt in (1, 2):
            run(f"setup pass {attempt}", ["bash", "setup.sh"], cwd=repository)
        report["setup_repeated"] = True
        report["config_preserved"] = fingerprint(config) == config_before
        report["isolated_shell_untouched"] = not (home / ".bashrc").exists()
        venv_python = state / "venv" / "bin" / "python"
        launcher = state / "bin" / "ink"
        report["installed_shebang_correct"] = (
            launcher.read_text(encoding="utf-8").splitlines()[0] == f"#!{venv_python}"
        )
        for label, command in (
            ("installed launcher", [str(launcher)]),
            ("source launcher", [str(repository / "ink")]),
        ):
            run(
                label,
                command,
                cwd=repository,
                accepted=(1,),
                timeout=60,
                expected_stderr="Usage: ink <prompt>",
            )
        report["launchers_load"] = True
        report.update(
            json.loads(
                run(
                    "installed runtime and documentation",
                    [str(venv_python), "-c", RUNTIME_PROBE],
                    timeout=120,
                )
            )
        )
        if args.check_model:
            response = run(
                "configured model",
                [str(launcher), "Reply with exactly INKLY_INSTALL_OK"],
                timeout=120,
            )
            report["model_checked"] = True
            report["model_response_ok"] = response == "INKLY_INSTALL_OK"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report["failure_category"] = type(exc).__name__
        print(str(exc), file=sys.stderr)
    finally:
        report["original_installation_unchanged"] = [
            fingerprint(path) for path in protected
        ] == before
        (root / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        os.umask(previous_umask)
        print(json.dumps(report, indent=2))
        print("Retained private test directory; do not publish commands.log.")
    gates = (
        "setup_repeated",
        "config_preserved",
        "isolated_shell_untouched",
        "installed_shebang_correct",
        "launchers_load",
        "runtime_is_private",
        "interpreter_is_private",
        "shared_logging_disabled",
        "shell_changes_disabled",
        "gaussian_database_is_private",
        "gaussian_database_exists",
        "gaussian_integrity_ok",
        "jobs_database_separate",
        "original_installation_unchanged",
    )
    passed = all(report.get(key) is True for key in gates)
    passed = (
        passed
        and bool(report.get("gaussian_search_matches"))
        and report.get("profile") == "Cuttlefish"
    )
    if args.check_model:
        passed = passed and report.get("model_response_ok") is True
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
