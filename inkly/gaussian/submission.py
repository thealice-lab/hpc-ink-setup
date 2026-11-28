"""What happened to a submission whose outcome is not known.

`submit` records SUBMISSION_UNKNOWN before calling sbatch, so a lost reply can
never lead to a second job. These helpers let it, and `check_submission`,
settle that state from evidence instead of leaving it for good:

- Slurm's own refusal: sbatch printed "Batch job submission failed:" with a
  validation reason (not a communication failure), so the controller created
  no job;
- the job itself: waiting or running with the bundle's ``inkly:<bundle>``
  comment, in accounting under the bundle's folder, or a ``slurm-<id>.out``
  that a started job wrote there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import subprocess

REFUSED = "Slurm refused the job, so nothing was submitted"
# Reasons that mean the reply may have been lost, not that Slurm said no.
_UNCERTAIN = (
    "unable to contact",
    "timed out",
    "time out",
    "timeout",
    "socket",
    "connection",
    "communication",
    "zero bytes",
    "standby",
    "temporarily unavailable",
    "temporarily unable",
    "try again",
    "not responding",
    "unreachable",
    "no route",
    "protocol",
    "transport endpoint",
    "slurm_receive_msg",
    "interrupted",
)
# Accounting can lag the controller; reopening waits at least this long.
SETTLE = timedelta(minutes=2)


def refusal(result):
    """Slurm's reason when sbatch was clearly refused; None when unsure."""
    if result.returncode == 0 or re.search(r"(?m)^[0-9]+", result.stdout or ""):
        return None
    for line in (result.stderr or "").splitlines():
        if "Batch job submission failed:" not in line:
            continue
        reason = line.split("Batch job submission failed:", 1)[1].strip()
        reason = "".join(c for c in reason if c.isprintable())[:200]
        if reason and not any(word in reason.lower() for word in _UNCERTAIN):
            return reason
    return None


def _when(value):
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc)


def attempted_at(directory, receipt):
    """When submission was attempted: recorded, or the receipt's last write."""
    recorded = _when(receipt.get("submit_attempted_at"))
    if recorded:
        return recorded
    seconds = (directory / "receipt.json").stat().st_mtime
    return datetime.fromtimestamp(seconds, timezone.utc)


def find_job(scheduler, directory, receipt, since):
    """(job ID or None, whether every place could be checked)."""
    uid, comment = str(os.getuid()), f"inkly:{receipt['bundle']}"
    answered = True
    try:
        queue = scheduler(
            ["squeue", "--noheader", "--user", uid, "--format", "%A|%k"], directory
        )
    except (OSError, subprocess.SubprocessError):
        queue, answered = None, False
    if queue is not None and queue.returncode == 0:
        for line in queue.stdout.splitlines():
            job, _, note = line.strip().partition("|")
            if job.isdigit() and note.strip() == comment:
                return job, True
    else:
        answered = False
    # Ended jobs: accounting keeps each job's working directory; comments are
    # stored only on clusters that keep them.
    folders = {str(directory), os.path.realpath(directory)}
    start = (since - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        accounting = scheduler(
            [
                "sacct",
                "--uid",
                uid,
                "--starttime",
                start,
                "-X",
                "-n",
                "-P",
                "-o",
                "JobIDRaw,WorkDir,Comment",
            ],
            directory,
        )
    except (OSError, subprocess.SubprocessError):
        accounting, answered = None, False
    if accounting is not None and accounting.returncode == 0:
        for line in accounting.stdout.splitlines():
            fields = line.strip().split("|")
            if len(fields) >= 3 and fields[0].isdigit():
                if fields[1] in folders or fields[2] == comment:
                    return fields[0], True
    else:
        answered = False
    # A job that started writes its Slurm log here unless its script moved it.
    for path in sorted(directory.glob("slurm-*.out")):
        match = re.fullmatch(r"slurm-([0-9]+)\.out", path.name)
        if match:
            return match[1], True
    return None, answered
