"""Bounded, read-only Slurm status and private failed-job excerpts."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess

TAIL_BYTES = 16 * 1024
TAIL_LINES = 60
FAILED_STATES = {"FAILED", "TIMEOUT", "OUT_OF_MEMORY"}


def display_text(value, limit=512):
    """Keep controls out of the UI without interpreting any log content."""
    return "".join(c for c in str(value) if c in "\n\t" or c.isprintable())[:limit]


def optional_value(value):
    value = value.strip()
    return (
        None
        if value.lower() in {"", "none", "unknown", "n/a", "invalid", "not_set"}
        else display_text(value)
    )


def query_status(directory, receipt, scheduler):
    job = receipt.get("job_id")
    if not job:
        return {"state": receipt["submission"], "exit_code": None}
    if not isinstance(job, str) or not re.fullmatch(r"[0-9]{1,20}", job):
        raise ValueError("Invalid recorded Slurm job ID")
    status = {"state": "UNKNOWN", "exit_code": None}
    try:
        result = scheduler(
            [
                "sacct",
                "-j",
                job,
                "-X",
                "-n",
                "-P",
                "-o",
                "JobIDRaw,State,ExitCode,Reason,Start,Elapsed",
            ],
            directory,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.strip().split("|")
                if len(fields) >= 3 and fields[0] == job:
                    status.update(
                        state=display_text(fields[1]), exit_code=display_text(fields[2])
                    )
                    if len(fields) >= 6:
                        status.update(
                            reason=optional_value(fields[3]),
                            start_time=optional_value(fields[4]),
                            elapsed=optional_value(fields[5]),
                        )
                    break
    except (OSError, subprocess.SubprocessError):
        pass
    # Accounting can lag and its Reason omits Priority/Resources. Use the queue
    # for live pending/running information, never override a recorded final state.
    if status["state"].split()[0] in {"UNKNOWN", "PENDING", "RUNNING", "CONFIGURING"}:
        try:
            result = scheduler(
                ["squeue", "--noheader", "--jobs", job, "--format", "%A|%T|%r|%S|%M"],
                directory,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    fields = line.strip().split("|")
                    if len(fields) >= 2 and fields[0] == job:
                        status["state"] = display_text(fields[1])
                        if len(fields) >= 5:
                            status.update(
                                reason=optional_value(fields[2]),
                                start_time=optional_value(fields[3]),
                                elapsed=optional_value(fields[4]),
                            )
                        break
        except (OSError, subprocess.SubprocessError):
            pass
    status["observed_at"] = datetime.now(timezone.utc).isoformat()
    return status


def _directory(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise PermissionError(
            "Job details require an owned, protected bundle directory"
        )


def _open_read(path):
    # Do not use private_open: that helper chmods files. Details never changes them.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise PermissionError("Job details require an owned regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _tail(path):
    try:
        with _open_read(path) as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - TAIL_BYTES))
            data = source.read(TAIL_BYTES)
        text = data.decode("utf-8", errors="replace")
        if size > TAIL_BYTES:
            # The initial bytes might start inside a line/UTF-8 sequence.
            text = text.partition("\n")[2]
        lines = text.splitlines()
        return {
            "name": path.name,
            "available": True,
            "text": display_text("\n".join(lines[-TAIL_LINES:]), TAIL_BYTES),
            "truncated": size > TAIL_BYTES or len(lines) > TAIL_LINES,
        }
    except OSError:
        return {"name": path.name, "available": False, "text": "", "truncated": False}


def job_details(root, bundle, scheduler):
    """Read only known files in this authenticated user's recorded bundle."""
    _directory(root)
    directory = root / bundle
    _directory(directory)
    with _open_read(directory / "receipt.json") as source:
        raw = source.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("Recorded handoff exceeds the details limit")
    receipt = json.loads(raw)
    name = receipt.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:gjf|com)", name, re.I
    ):
        raise ValueError("Invalid recorded input filename")
    if receipt.get("bundle") != bundle:
        raise ValueError("Recorded bundle does not match")
    status = query_status(directory, receipt, scheduler)
    result = {
        "bundle": bundle,
        "name": name,
        "job_id": receipt.get("job_id"),
        **status,
        "logs": [],
        "gaussian_error": None,
    }
    if status["state"].split()[0] in FAILED_STATES:
        # Slurm's default combines stdout/stderr in slurm-<id>.out. Never accept
        # filenames, globs or scheduler-reported paths from the request.
        gaussian = _tail(directory / (Path(name).stem + ".log"))
        result["logs"] = [
            gaussian,
            _tail(directory / ("slurm-" + receipt["job_id"] + ".out")),
        ]
        errors = [
            line
            for line in gaussian["text"].splitlines()
            if "error termination" in line.lower()
        ]
        result["gaussian_error"] = errors[-1] if errors else None
    return result
