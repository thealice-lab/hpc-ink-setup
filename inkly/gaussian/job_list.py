"""Read-only current-user Slurm listing, independent of local input files."""

from datetime import datetime, timezone
import os
import re
import subprocess

MAX_JOBS = 200


def _text(value, limit=160):
    return "".join(c for c in value.strip() if c.isprintable())[:limit]


def list_jobs(scheduler, directory):
    uid = str(os.getuid())
    jobs = {}
    queued = set()
    unavailable = []
    truncated = False
    commands = [
        (
            "accounting",
            [
                "sacct",
                "--uid",
                uid,
                "--starttime",
                "now-7days",
                "-X",
                "-n",
                "-P",
                "-o",
                "JobIDRaw,JobName%128,State,Elapsed,ExitCode",
            ],
        ),
        (
            "queue",
            ["squeue", "--noheader", "--user", uid, "--format", "%A|%j|%T|%M|%S|%r"],
        ),
    ]
    for source, args in commands:
        try:
            result = scheduler(args, directory)
            if result.returncode:
                unavailable.append(source)
                continue
        except (OSError, subprocess.SubprocessError):
            unavailable.append(source)
            continue
        for line in result.stdout.splitlines():
            fields = line.strip().split("|")
            if len(fields) < (5 if source == "accounting" else 6):
                continue
            job = fields[0]
            if not re.fullmatch(r"[0-9]{1,20}(?:_[0-9]{1,20})?", job):
                continue  # No steps, federated banners or arbitrary IDs.
            row = {
                "job_id": job,
                "name": _text(fields[1]),
                "state": _text(fields[2]),
                "elapsed": _text(fields[3]),
                "exit_code": None,
                "start_time": None,
                "reason": None,
            }
            if source == "accounting":
                row["exit_code"] = _text(fields[4])
            else:
                row["start_time"] = _text(fields[4])
                row["reason"] = _text(fields[5])
            jobs[job] = row  # The current queue takes precedence over accounting.
            if source == "queue":
                queued.add(job)
            # Bound returned metadata while retaining the highest IDs. The queue
            # gets priority so recent accounting cannot hide pending/running jobs.
            if len(jobs) > MAX_JOBS:
                truncated = True
                oldest = min(
                    jobs,
                    key=lambda key: (key in queued, tuple(map(int, key.split("_")))),
                )
                del jobs[oldest]
                queued.discard(oldest)
    return {
        "jobs": sorted(
            jobs.values(),
            key=lambda row: tuple(map(int, row["job_id"].split("_"))),
            reverse=True,
        ),
        "unavailable": unavailable,
        "truncated": truncated,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "history_days": 7,
    }
