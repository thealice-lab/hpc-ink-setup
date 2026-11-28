"""The only Slurm commands Inkly runs. Every one of them only reads.

Each command is a fixed template in ``QUERIES``. Callers choose a query by
name and may pass only validated values: a look-back in days, or numeric job
IDs. The user is always identified by the operating system's numeric user ID,
never by text from a question. Commands run without a shell, with a time
limit and a cap on output size, and without the environment variables that
change Slurm's output format, so the parsers always see the requested layout.

Nothing in this module can submit, cancel, hold or change a job.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType


# Programs that are allowed to appear in a template. All of them only read.
READ_ONLY_PROGRAMS = frozenset({"squeue", "sinfo", "sacct"})

COMMAND_TIMEOUT_SECONDS = 10
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_DAYS = 31
MAX_JOB_IDS = 20

# sacct accepts raw (numeric) job IDs; array tasks have their own raw ID.
RAW_JOB_ID = re.compile(r"^\d{1,12}$")

# Job IDs as people type them, including array tasks such as 123456_7. Plugins
# use this to find IDs in a question; such IDs only filter the user's own jobs.
JOB_ID_IN_TEXT = re.compile(r"(?<![\w.])(\d{2,12}(?:_\d{1,7})?)(?![\w.])")

# Variables that change what Slurm prints (field layout, time format, array
# ID width, implicit filters). They are removed before running a command.
FORMAT_VARIABLE_PREFIXES = ("SQUEUE_", "SINFO_", "SACCT_")
FORMAT_VARIABLES = frozenset({"SLURM_TIME_FORMAT", "SLURM_BITSTR_LEN"})

# Placeholders stand for a whole argument. Their values never come from the
# question text: {uid} comes from the operating system, {start} is a date
# computed from a validated number of days, {job_ids} are validated numbers.
USER_PLACEHOLDER = "{uid}"
START_PLACEHOLDER = "{start}"
JOB_IDS_PLACEHOLDER = "{job_ids}"

# Flags left out of the command shown to people, so a pasted copy prints
# column headers. The data is otherwise identical.
HIDDEN_DISPLAY_FLAGS = frozenset({"--noheader"})

QUERIES = MappingProxyType(
    {
        # One state per visible job; only counted, never shown.
        "queue_states": (
            "squeue",
            ("--noheader", "--format", "%T"),
        ),
        # Partition, availability, nodes A/I/O/T, CPUs A/I/O/T.
        "partition_load": (
            "sinfo",
            ("--noheader", "--format", "%P|%a|%F|%C"),
        ),
        # Partition, availability, nodes, CPUs/node, memory/node, GRES,
        # time limit, default time, job size in nodes.
        "partition_resources": (
            "sinfo",
            ("--noheader", "--format", "%P|%a|%D|%c|%m|%G|%l|%L|%s"),
        ),
        # The user's own queued and running jobs, without job names.
        "my_queue": (
            "squeue",
            (
                "--noheader",
                "--user",
                USER_PLACEHOLDER,
                "--format",
                "%i|%T|%P|%r|%M|%L|%l|%S|%D|%C|%m",
            ),
        ),
        # The user's own jobs since a start date, one line per job. AllocTRES
        # gives the job's total allocated memory on every Slurm version
        # (ReqMem changed meaning in 21.08), so it comes last.
        "my_recent_jobs": (
            "sacct",
            (
                "--user",
                USER_PLACEHOLDER,
                "--starttime",
                START_PLACEHOLDER,
                "--allocations",
                "--noheader",
                "--parsable2",
                "--format",
                "JobIDRaw,JobID,State,ExitCode,Partition,Elapsed,Timelimit,"
                "AllocCPUS,ReqCPUS,NNodes,TotalCPU,AllocTRES",
            ),
        ),
        # CPU time and peak memory of the steps of some of the user's jobs.
        "my_job_steps": (
            "sacct",
            (
                "--user",
                USER_PLACEHOLDER,
                "--jobs",
                JOB_IDS_PLACEHOLDER,
                "--noheader",
                "--parsable2",
                "--format",
                "JobIDRaw,TotalCPU,MaxRSS",
            ),
        ),
    }
)


class SlurmQueryRefused(ValueError):
    """Raised for anything outside the allowlist or with unvalidated values."""


@dataclass(frozen=True)
class SlurmResult:
    """Outcome of one allowlisted command.

    status is "ok", "missing" (program not installed), "failed" (non-zero
    exit, for example an unreachable controller) or "timeout".
    """

    query: str
    status: str
    output: str = ""
    truncated: bool = False
    shown_command: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _uid() -> str:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise SlurmQueryRefused("Slurm queries need a POSIX user ID")
    return str(getuid())


def _today() -> date:
    return date.today()


def _start_date(days) -> str:
    if isinstance(days, bool) or not isinstance(days, int):
        raise SlurmQueryRefused("days must be a whole number")
    if not 1 <= days <= MAX_DAYS:
        raise SlurmQueryRefused(f"days must be between 1 and {MAX_DAYS}")
    return (_today() - timedelta(days=days)).isoformat()


def _job_id_list(job_ids) -> str:
    if isinstance(job_ids, (str, bytes)) or not isinstance(job_ids, (list, tuple)):
        raise SlurmQueryRefused("job_ids must be a list of job IDs")
    for job_id in job_ids:
        if not isinstance(job_id, str) or not RAW_JOB_ID.fullmatch(job_id):
            raise SlurmQueryRefused("job IDs must be numeric")
    unique = list(dict.fromkeys(job_ids))
    if not 1 <= len(unique) <= MAX_JOB_IDS:
        raise SlurmQueryRefused(f"between 1 and {MAX_JOB_IDS} job IDs are allowed")
    return ",".join(unique)


def build_command(name: str, *, days=None, job_ids=None) -> tuple[list[str], str]:
    """Return (argv, shown_command) for an allowlisted query.

    argv is what runs. shown_command is the same command for people to read
    and paste: the user ID is written as $USER and --noheader is left out.
    """
    if not isinstance(name, str) or name not in QUERIES:
        raise SlurmQueryRefused(f"{name!r} is not an allowed Slurm query")

    program, template = QUERIES[name]
    if program not in READ_ONLY_PROGRAMS:
        raise SlurmQueryRefused(f"{program!r} is not a read-only Slurm program")

    if days is not None and START_PLACEHOLDER not in template:
        raise SlurmQueryRefused(f"{name} does not take a number of days")
    if job_ids is not None and JOB_IDS_PLACEHOLDER not in template:
        raise SlurmQueryRefused(f"{name} does not take job IDs")

    values = {}
    if USER_PLACEHOLDER in template:
        values[USER_PLACEHOLDER] = _uid()
    if START_PLACEHOLDER in template:
        values[START_PLACEHOLDER] = _start_date(days)
    if JOB_IDS_PLACEHOLDER in template:
        values[JOB_IDS_PLACEHOLDER] = _job_id_list(job_ids)

    argv = [program, *(values.get(arg, arg) for arg in template)]

    shown = [program]
    for arg in template:
        if arg in HIDDEN_DISPLAY_FLAGS:
            continue
        if arg == USER_PLACEHOLDER:
            shown.append("$USER")
        else:
            shown.append(shlex.quote(values.get(arg, arg)))
    return argv, " ".join(shown)


def _clean_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in FORMAT_VARIABLES and not key.startswith(FORMAT_VARIABLE_PREFIXES)
    }
    # ISO-style timestamps (2026-11-25T14:05:00) on every Slurm version.
    env["SLURM_TIME_FORMAT"] = "standard"
    return env


def run(name: str, *, days=None, job_ids=None) -> SlurmResult:
    """Run one allowlisted, read-only Slurm query.

    Raises SlurmQueryRefused for an unknown query or invalid values; every
    other problem is reported in the returned SlurmResult.
    """
    argv, shown = build_command(name, days=days, job_ids=job_ids)

    executable = shutil.which(argv[0])
    if executable is None:
        return SlurmResult(name, "missing", shown_command=shown)

    with tempfile.TemporaryFile() as captured:
        try:
            completed = subprocess.run(
                [executable, *argv[1:]],
                stdin=subprocess.DEVNULL,
                stdout=captured,
                stderr=subprocess.DEVNULL,
                env=_clean_environment(),
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return SlurmResult(name, "timeout", shown_command=shown)
        except OSError:
            return SlurmResult(name, "failed", shown_command=shown)

        if completed.returncode != 0:
            return SlurmResult(name, "failed", shown_command=shown)

        captured.seek(0)
        data = captured.read(MAX_OUTPUT_BYTES + 1)

    truncated = len(data) > MAX_OUTPUT_BYTES
    if truncated:
        # Keep only complete lines so a cut-off row is never parsed.
        data = data[:MAX_OUTPUT_BYTES].rsplit(b"\n", 1)[0]
    text = data.decode("utf-8", errors="replace")
    return SlurmResult(name, "ok", text, truncated, shown)


def unavailable_reason(result: SlurmResult) -> str:
    """Plain words for a result that is not ok, naming the program."""
    program = QUERIES[result.query][0]
    if result.status == "missing":
        return (
            f"`{program}` is not installed here, so this cluster may not use "
            "Slurm or the command is not on PATH"
        )
    if result.status == "timeout":
        return (
            f"`{program}` did not answer within {COMMAND_TIMEOUT_SECONDS} seconds; "
            "the scheduler may be busy"
        )
    return f"`{program}` failed; the scheduler may be unreachable right now"
