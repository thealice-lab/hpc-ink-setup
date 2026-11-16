"""Read-only list of this account's Inkly handoffs and their Slurm states.

The list comes from the protected handoff receipts under ~/.inkly/tensor, so it
needs no local input files and never includes jobs submitted outside Inkly.
Slurm is asked only about the job IDs those receipts record. Nothing here
creates, changes or deletes a file, and nothing submits or cancels a job.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat
import subprocess

MAX_JOBS = 500
# Bundle directories read at most, newest first; bounds a very old store.
MAX_SCANNED = 5000
# Job IDs per sacct call; keeps each command line short.
ID_CHUNK = 100
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:gjf|com)", re.I)
JOB_ID = re.compile(r"[0-9]{1,20}")
SUBMISSIONS = {"PREPARED", "SUBMITTED", "SUBMISSION_UNKNOWN"}
# Accounting for these may lag the queue; a final state never comes from it.
LIVE = {
    "PENDING",
    "RUNNING",
    "CONFIGURING",
    "COMPLETING",
    "SUSPENDED",
    "REQUEUED",
    "RESIZING",
}


def _text(value, limit=160):
    return "".join(c for c in value.strip() if c.isprintable())[:limit]


def _utc(value):
    """Slurm prints times in the HPC's local time without a zone; return UTC."""
    value = value.strip()
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None  # Unknown, None, N/A or an estimate Slurm cannot give.
    return moment.astimezone(timezone.utc).isoformat()


def _moment(value):
    return datetime.fromisoformat(value)


def _owned(info, kind):
    return kind(info.st_mode) and info.st_uid == os.getuid()


def _receipts(root):
    """Inspect only protected owned receipt files; never chmod or create storage."""
    try:
        info = root.lstat()
    except FileNotFoundError:
        return [], False
    if not _owned(info, stat.S_ISDIR) or info.st_mode & 0o022:
        raise PermissionError("Job history requires protected, owned handoff storage")
    entries = []
    with os.scandir(root) as listing:
        for entry in listing:
            if not re.fullmatch(r"[0-9a-f]{32}", entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _owned(info, stat.S_ISDIR) and not info.st_mode & 0o022:
                entries.append((info.st_mtime_ns, entry))
    entries.sort(key=lambda pair: pair[0], reverse=True)
    rows = []
    for _, entry in entries[:MAX_SCANNED]:
        try:
            row = _receipt(entry)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if row:
            rows.append(row)
    rows.sort(key=lambda row: _moment(row["prepared_at"]), reverse=True)
    return rows[:MAX_JOBS], len(rows) > MAX_JOBS or len(entries) > MAX_SCANNED


def _receipt(entry):
    fd = os.open(
        os.path.join(entry.path, "receipt.json"),
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not _owned(info, stat.S_ISREG) or info.st_nlink != 1:
            return None
        raw = source.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        return None
    record = json.loads(raw)
    name, job = record.get("name", ""), record.get("job_id")
    if record.get("bundle") != entry.name or not isinstance(name, str):
        return None
    if not NAME.fullmatch(name):
        return None
    if job is not None and (not isinstance(job, str) or not JOB_ID.fullmatch(job)):
        return None
    submission = record.get("submission")
    if submission not in SUBMISSIONS:
        return None
    # The input is written once, when the bundle is prepared, so its time is
    # when this handoff began. Fall back to the receipt if it is unusable.
    prepared = info.st_mtime
    try:
        source = os.stat(os.path.join(entry.path, name), follow_symlinks=False)
        if _owned(source, stat.S_ISREG):
            prepared = source.st_mtime
    except OSError:
        pass
    return {
        "bundle": entry.name,
        "name": name,
        "job_id": job or "",
        "submission": submission,
        "prepared_at": datetime.fromtimestamp(prepared, timezone.utc).isoformat(),
    }


def _accounting(scheduler, directory, uid, ids, since):
    found = {}
    for start in range(0, len(ids), ID_CHUNK):
        chunk = ids[start : start + ID_CHUNK]
        result = scheduler(
            [
                "sacct",
                "--uid",
                uid,
                # Explicit window: from the day before the oldest recorded
                # handoff, so older jobs are found whatever sacct's default is.
                "--starttime",
                since,
                "-X",
                "-n",
                "-P",
                "-o",
                "JobIDRaw,State,ExitCode,Submit,End,Elapsed",
                "--jobs",
                ",".join(chunk),
            ],
            directory,
        )
        if result.returncode:
            raise OSError("accounting")
        for line in result.stdout.splitlines():
            fields = line.strip().split("|")
            if len(fields) >= 6 and fields[0] in chunk:
                found[fields[0]] = {
                    "state": _text(fields[1]),
                    "exit_code": _text(fields[2]) or None,
                    "submitted_at": _utc(fields[3]),
                    "ended_at": _utc(fields[4]),
                    "elapsed": _text(fields[5]) or None,
                }
    return found


def _queue(scheduler, directory, uid, ids):
    # List the account's own queue and keep only recorded IDs. Asking for one
    # finished job ID makes squeue fail, so IDs are never passed to it.
    result = scheduler(
        ["squeue", "--noheader", "--user", uid, "--format", "%A|%T|%r|%S|%M"],
        directory,
    )
    if result.returncode:
        raise OSError("queue")
    wanted, found = set(ids), {}
    for line in result.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) >= 5 and fields[0] in wanted:
            reason = _text(fields[2])
            found[fields[0]] = {
                "state": _text(fields[1]),
                "reason": None if reason in {"", "None", "(null)"} else reason,
                "start_at": _utc(fields[3]),
                "elapsed": _text(fields[4]) or None,
            }
    return found


def list_jobs(scheduler, directory, root):
    """Every recorded handoff, newest first, with its current Slurm state."""
    receipts, truncated = _receipts(root)
    ids = sorted({row["job_id"] for row in receipts if row["job_id"]}, key=int)
    unavailable, accounting, queue = [], {}, {}
    if ids:
        uid = str(os.getuid())
        oldest = min(_moment(row["prepared_at"]) for row in receipts if row["job_id"])
        since = (oldest - timedelta(days=1)).astimezone().strftime("%Y-%m-%d")
        for source, read in (
            ("accounting", lambda: _accounting(scheduler, directory, uid, ids, since)),
            ("queue", lambda: _queue(scheduler, directory, uid, ids)),
        ):
            try:
                found = read()
            except (OSError, subprocess.SubprocessError):
                unavailable.append(source)
                continue
            if source == "accounting":
                accounting = found
            else:
                queue = found
    jobs = []
    for row in receipts:
        job = {
            **row,
            "state": row["submission"] if not row["job_id"] else "UNKNOWN",
            "exit_code": None,
            "submitted_at": None,
            "ended_at": None,
            "elapsed": None,
            "reason": None,
            "start_at": None,
        }
        if row["job_id"]:
            job.update(accounting.get(row["job_id"], {}))
            live = queue.get(row["job_id"])
            # The queue is current for live jobs, but a final accounting state
            # is never replaced by a stale queue entry.
            if live and (
                row["job_id"] not in accounting or job["state"].split(" ")[0] in LIVE
            ):
                job.update(live)
        jobs.append(job)
    jobs.sort(
        key=lambda job: _moment(job["submitted_at"] or job["prepared_at"]),
        reverse=True,
    )
    return {
        "jobs": jobs,
        "unavailable": unavailable,
        "truncated": truncated,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
