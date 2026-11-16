"""Read-only current-user Slurm listing, independent of local input files."""

from datetime import datetime, timezone
import os
import re
import subprocess

MAX_JOBS = 200


def _text(value, limit=160):
    return "".join(c for c in value.strip() if c.isprintable())[:limit]


def list_jobs(scheduler, directory, root=None):
    uid = str(os.getuid())
    jobs = {}
    queued = set()
    unavailable = []
    truncated = False
    receipts = None
    if root is not None:
        receipts, truncated = _receipts(root)
        if not receipts:
            return {
                "jobs": [],
                "unavailable": [],
                "truncated": False,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "history_days": 7,
            }
        recorded_ids = sorted({row["job_id"] for row in receipts if row["job_id"]})
        if not recorded_ids:
            return {
                "jobs": [
                    {k: v for k, v in row.items() if k != "_modified"}
                    for row in receipts
                ],
                "unavailable": [],
                "truncated": truncated,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "history_days": 7,
            }
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
        if receipts is not None:
            args += ["--jobs", ",".join(recorded_ids)]
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
    if receipts is not None:
        recorded = []
        for row in receipts:
            status = jobs.get(row["job_id"], {})
            recorded.append(
                {
                    **status,
                    **{k: v for k, v in row.items() if k not in {"_modified", "state"}},
                    "state": status.get("state", row["state"]),
                }
            )
        jobs = {row["bundle"]: row for row in recorded}
    return {
        "jobs": list(jobs.values())
        if receipts is not None
        else sorted(
            jobs.values(),
            key=lambda row: tuple(map(int, row["job_id"].split("_"))),
            reverse=True,
        ),
        "unavailable": unavailable,
        "truncated": truncated,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "history_days": 7,
    }


def _receipts(root):
    """Inspect only protected owned receipt files; never chmod or create storage."""
    import json
    import stat

    rows = []
    try:
        info = root.lstat()
    except FileNotFoundError:
        return [], False
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise PermissionError("My jobs requires protected, owned handoff storage")
    with os.scandir(root) as entries:
        for entry in entries:
            if not re.fullmatch(r"[0-9a-f]{32}", entry.name) or not entry.is_dir(
                follow_symlinks=False
            ):
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                continue
            try:
                fd = os.open(
                    os.path.join(entry.path, "receipt.json"),
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                    ):
                        continue
                    raw = source.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    continue
                record = json.loads(raw)
                name, job = record.get("name", ""), record.get("job_id")
                if record.get("bundle") != entry.name or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:gjf|com)", name, re.I
                ):
                    continue
                if job is not None and (
                    not isinstance(job, str) or not re.fullmatch(r"[0-9]{1,20}", job)
                ):
                    continue
                submission = record.get("submission")
                if submission not in {"PREPARED", "SUBMITTED", "SUBMISSION_UNKNOWN"}:
                    continue
                rows.append(
                    {
                        "bundle": entry.name,
                        "name": name,
                        "job_id": job or "",
                        "state": submission if not job else "UNKNOWN",
                        "submission": submission,
                        "_modified": info.st_mtime_ns,
                    }
                )
                if len(rows) > MAX_JOBS:
                    rows.sort(key=lambda row: row["_modified"], reverse=True)
                    del rows[MAX_JOBS:]
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    return rows, len(rows) >= MAX_JOBS
