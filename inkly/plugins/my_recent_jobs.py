from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from inkly import slurm_readonly
from inkly.plugins._slurm_text import (
    human_duration,
    human_memory,
    parse_bytes_as_mib,
    parse_duration,
    parse_memory_mib,
    source_lines,
    split_fields,
    time_limit_words,
)
from inkly.plugins.common import format_plugin_output, validate_plugin_meta


PLUGIN_META = {
    "name": "my_recent_jobs",
    "description": (
        "Summarises your own Slurm jobs from the last 7 days, from sacct for "
        "your user only: how each job ended, exit codes, run time against the "
        "time limit, and how much of the requested CPUs and memory it actually "
        "used (efficiency). Job names are left out."
    ),
    "category": "my-jobs",
    "example_queries": [
        "How did my recent jobs go?",
        "Did my last job finish?",
        "Why did my job fail?",
        "How efficient were my jobs?",
        "How much memory did my job use?",
        "Am I asking for too many CPUs or too much memory?",
        "Show my jobs from the last week",
    ],
}

validate_plugin_meta(PLUGIN_META)

DEFAULT_DAYS = 7
SHOWN_JOBS = 6
# Jobs shorter than this are left out of efficiency figures: start-up time
# dominates and the percentages say little.
MIN_EFFICIENCY_SECONDS = 60
LOW_CPU_USE = 0.5
LOW_MEMORY_USE = 0.25
FIELD_COUNT = 11

ACTIVE_STATES = {
    "PENDING",
    "RUNNING",
    "REQUEUED",
    "REQUEUE_HOLD",
    "REQUEUE_FED",
    "RESIZING",
    "SUSPENDED",
}
STATE_WORDS = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "OUT_OF_MEMORY": "out of memory",
    "TIMEOUT": "timed out",
    "CANCELLED": "cancelled",
    "NODE_FAIL": "node failure",
    "PREEMPTED": "preempted",
    "BOOT_FAIL": "boot failure",
    "DEADLINE": "missed deadline",
}
STATE_NOTES = {
    "OUT_OF_MEMORY": "it ran out of memory; it needs a larger memory request",
    "TIMEOUT": "it hit its time limit; it needs more time or a restart point",
    "NODE_FAIL": "a node failed under it; running it again usually works",
    "PREEMPTED": "a higher-priority job took its nodes",
}
_DAYS_IN_QUERY = re.compile(r"\b(\d{1,2})\s*days?\b", re.IGNORECASE)
_MEM_TRES = re.compile(r"(?:^|,)mem=([^,]+)")


@dataclass
class RecentJob:
    raw_id: str
    job_id: str
    state: str
    exit_code: str
    partition: str
    elapsed: str
    limit: str
    cpus: str
    nodes: str
    total_cpu: str
    alloc_tres: str
    step_cpu_seconds: float | None = None
    peak_memory_mib: float | None = None

    @property
    def finished(self) -> bool:
        return self.state not in ACTIVE_STATES

    @property
    def sort_key(self) -> int:
        return int(self.raw_id) if self.raw_id.isdigit() else -1

    @property
    def memory_mib(self) -> float | None:
        match = _MEM_TRES.search(self.alloc_tres)
        return parse_memory_mib(match[1]) if match else None

    @property
    def elapsed_seconds(self) -> int | None:
        return parse_duration(self.elapsed)

    def cpu_use(self) -> float | None:
        elapsed = self.elapsed_seconds
        cpus = int(self.cpus) if self.cpus.isdigit() else 0
        if not elapsed or elapsed < MIN_EFFICIENCY_SECONDS or not cpus:
            return None
        # Zero CPU time means Slurm did not record it, not an idle job.
        used = self.step_cpu_seconds or parse_duration(self.total_cpu)
        if not used:
            return None
        return used / (elapsed * cpus)

    def memory_use(self) -> float | None:
        requested = self.memory_mib
        elapsed = self.elapsed_seconds
        # A blank or zero peak means Slurm did not record memory use.
        if not requested or not self.peak_memory_mib:
            return None
        if not elapsed or elapsed < MIN_EFFICIENCY_SECONDS:
            return None
        return self.peak_memory_mib / requested


def _days(query: str) -> int:
    text = query or ""
    match = _DAYS_IN_QUERY.search(text)
    if match and 1 <= int(match[1]) <= slurm_readonly.MAX_DAYS:
        return int(match[1])
    if re.search(r"\bmonth\b", text, re.IGNORECASE):
        return 30
    return DEFAULT_DAYS


def _state(value: str) -> str:
    # "CANCELLED by 12345" names whoever cancelled it; keep only the state.
    return value.split(" ", 1)[0].strip().rstrip("+")


def _parse_jobs(output: str) -> list[RecentJob]:
    jobs = []
    for line in output.splitlines():
        fields = split_fields(line, FIELD_COUNT)
        if fields is None or not fields[0].isdigit():
            continue
        fields[2] = _state(fields[2])
        jobs.append(RecentJob(*fields))
    return jobs


def _apply_steps(jobs: list[RecentJob], output: str) -> None:
    by_id = {job.raw_id: job for job in jobs}
    for line in output.splitlines():
        fields = split_fields(line, 3)
        if fields is None or "." not in fields[0]:
            continue
        job = by_id.get(fields[0].split(".", 1)[0])
        if job is None:
            continue
        cpu = parse_duration(fields[1])
        if cpu is not None:
            job.step_cpu_seconds = (job.step_cpu_seconds or 0) + cpu
        rss = parse_bytes_as_mib(fields[2])
        if rss:
            job.peak_memory_mib = max(job.peak_memory_mib or 0, rss)


def _exit_words(code: str) -> str:
    status, _, signal = code.partition(":")
    if signal and signal != "0":
        return f"signal {signal}"
    if status and status != "0":
        return f"exit code {status}"
    return ""


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _job_line(job: RecentJob) -> str:
    head = [STATE_WORDS.get(job.state, job.state.lower())]
    exit_words = _exit_words(job.exit_code)
    if exit_words:
        head.append(exit_words)
    resources = f"{job.cpus} CPUs"
    if job.memory_mib:
        resources += f", {human_memory(job.memory_mib)}"
    parts = [
        f"- {job.job_id}: {', '.join(head)}; {job.partition}, {resources}",
        f"ran {human_duration(job.elapsed_seconds)} of {time_limit_words(job.limit)}",
    ]

    cpu_use = job.cpu_use()
    if cpu_use is not None:
        note = " (requested more CPUs than it used)" if cpu_use < LOW_CPU_USE else ""
        parts.append(f"CPU use {_percent(cpu_use)}{note}")
    memory_use = job.memory_use()
    if memory_use is not None:
        note = " (much less than requested)" if memory_use < LOW_MEMORY_USE else ""
        parts.append(
            f"memory peak {human_memory(job.peak_memory_mib)}, "
            f"{_percent(memory_use)} of the request{note}"
        )
    line = "; ".join(parts)
    if job.state in STATE_NOTES:
        line += f"; {STATE_NOTES[job.state]}"
    return line


def _mentioned(job: RecentJob, ids: list[str]) -> bool:
    return any(
        wanted in {job.raw_id, job.job_id, job.job_id.split("_", 1)[0]}
        for wanted in ids
    )


def _summary(jobs: list[RecentJob], days: int) -> str:
    counts = Counter(job.state for job in jobs)
    active = sum(counts.pop(state, 0) for state in ACTIVE_STATES)
    parts = [
        f"{count} {STATE_WORDS.get(state, state.lower())}"
        for state, count in counts.most_common()
    ]
    if active:
        parts.append(f"{active} still running or waiting")
    noun = "job" if len(jobs) == 1 else "jobs"
    return f"In the last {days} days you had {len(jobs)} {noun}: {', '.join(parts)}."


def _average(values: list[float]) -> str:
    return _percent(sum(values) / len(values))


def _efficiency_lines(shown: list[RecentJob]) -> list[str]:
    cpu = [value for job in shown if (value := job.cpu_use()) is not None]
    memory = [value for job in shown if (value := job.memory_use()) is not None]
    long_enough = [
        job for job in shown if (job.elapsed_seconds or 0) >= MIN_EFFICIENCY_SECONDS
    ]
    lines = []
    if cpu or memory:
        figures = []
        if cpu:
            figures.append(f"average CPU use {_average(cpu)}")
        if memory:
            figures.append(f"average memory peak {_average(memory)} of the request")
        lines.append(f"Across the jobs listed: {', '.join(figures)}.")
    if long_enough and not cpu and not memory:
        lines.append(
            "Efficiency is unavailable: Slurm recorded no CPU time or memory use "
            "for these jobs (job accounting may not collect it on this cluster)."
        )
    elif long_enough and not memory:
        lines.append(
            "Memory peak is unavailable: Slurm recorded no memory use for these jobs."
        )
    if memory:
        lines.append(
            "Memory peak is the largest step's sampled maximum; short spikes, such "
            "as the one that ends an out-of-memory job, may not show."
        )
    return lines


def collect(query: str = "") -> tuple[list[str], list[slurm_readonly.SlurmResult]]:
    days = _days(query)
    allocations = slurm_readonly.run("my_recent_jobs", days=days)
    if not allocations.ok:
        reason = slurm_readonly.unavailable_reason(allocations)
        return [f"Your recent jobs are unavailable: {reason}."], [allocations]

    jobs = _parse_jobs(allocations.output)
    if not jobs:
        if allocations.output.strip():
            return (
                ["Your recent jobs are unavailable: could not parse sacct output."],
                [allocations],
            )
        return [f"You had no jobs in the last {days} days."], [allocations]

    ids = list(dict.fromkeys(slurm_readonly.JOB_ID_IN_TEXT.findall(query or "")))
    finished = sorted(
        (job for job in jobs if job.finished),
        key=lambda job: (_mentioned(job, ids), job.sort_key),
        reverse=True,
    )
    shown = finished[:SHOWN_JOBS]

    results = [allocations]
    if shown:
        steps = slurm_readonly.run(
            "my_job_steps", job_ids=[job.raw_id for job in shown]
        )
        results.append(steps)
        if steps.ok:
            _apply_steps(shown, steps.output)

    lines = [_summary(jobs, days), "Job names are not included."]
    if shown:
        lines.append("Most recent finished jobs:")
        lines += [_job_line(job) for job in shown]
        if len(finished) > len(shown):
            lines.append(f"- and {len(finished) - len(shown)} more finished jobs")

        lines += _efficiency_lines(shown)
    for wanted in ids:
        if len(wanted.split("_", 1)[0]) >= 5 and not any(
            _mentioned(job, [wanted]) for job in jobs
        ):
            lines.append(
                f"Job {wanted} is not among your jobs from the last {days} days."
            )
    if allocations.truncated:
        lines.append("The job list was cut short because it is very long.")
    return lines, results


def run(query: str = "") -> str:
    """Summarise the user's own recent jobs and how efficiently they ran."""
    lines, results = collect(query)
    lines.extend(source_lines(*results))
    return format_plugin_output("Your Recent Slurm Jobs (live)", lines)
