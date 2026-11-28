"""Made-up Slurm output for plugin tests, served through the real allowlist.

Tests describe what each allowlisted query should print; the fake replaces
only shutil.which and subprocess.run inside inkly.slurm_readonly, so the
allowlist's own argument building and checks still run.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date

from inkly import slurm_readonly

TIMEOUT = object()
FAILED = object()


def _query_for(argv: list[str]) -> str:
    program = os.path.basename(argv[0])
    fields = argv[argv.index("--format") + 1]
    for name, (query_program, template) in slurm_readonly.QUERIES.items():
        if query_program == program and fields in template:
            return name
    raise AssertionError(f"command outside the allowlist: {argv}")


def install_fake_slurm(monkeypatch, outputs, *, missing=(), uid="4242"):
    """Serve outputs[query_name]; return the list of (query, argv) calls."""
    calls: list[tuple[str, list[str]]] = []

    def fake_which(cmd):
        return None if cmd in missing else f"/usr/bin/{cmd}"

    def fake_run(argv, **kwargs):
        assert kwargs.get("shell") is False
        name = _query_for(argv)
        calls.append((name, list(argv)))
        value = outputs.get(name, "")
        if value is TIMEOUT:
            raise subprocess.TimeoutExpired(
                argv, slurm_readonly.COMMAND_TIMEOUT_SECONDS
            )
        if value is FAILED:
            return subprocess.CompletedProcess(argv, 1)
        kwargs["stdout"].write(value.encode("utf-8"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(slurm_readonly.shutil, "which", fake_which)
    monkeypatch.setattr(slurm_readonly.subprocess, "run", fake_run)
    monkeypatch.setattr(slurm_readonly, "_uid", lambda: uid)
    monkeypatch.setattr(slurm_readonly, "_today", lambda: date(2026, 11, 25))
    return calls
