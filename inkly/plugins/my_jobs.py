from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from inkly import slurm_readonly
from inkly.plugins._slurm_reasons import explain, reason_code
from inkly.plugins._slurm_text import (
    clock,
    human_duration,
    is_unset,
    parse_duration,
    parse_time,
    relative_time,
    source_lines,
    split_fields,
    time_limit_words,
)
from inkly.plugins.common import format_plugin_output, validate_plugin_meta


PLUGIN_META = {
    "name": "my_jobs",
    "description": (
        "Shows your own Slurm jobs that are running or waiting in the queue "
        "right now, from squeue for your user only: time used and left, why a "
        "pending job is waiting in plain words, and when the scheduler expects "
        "it to start. Job names are left out."
    ),
    "category": "my-jobs",
    "example_queries": [
        "What are my jobs doing right now?",
        "Is my job running yet?",
        "Why is my job pending?",
        "When will my job start?",
        "How much time is left on my running job?",
        "Show my queued jobs",
        "What jobs do I have in the queue?",
        "Why is job 123456 still waiting?",
    ],
}

validate_plugin_meta(PLUGIN_META)

JOB_LIMIT = 8
# Numbers shorter than this in a question are not treated as job IDs when
# reporting "not found" (they are usually counts, days or versions).
MIN_REPORTED_ID_DIGITS = 5
FIELD_COUNT = 11


@dataclass(frozen=True)
class QueuedJob:
    job_id: str
    state: str
    partition: str
    reason: str
    used: str
    left: str
    limit: str
    start: str
    nodes: str
    cpus: str
    memory: str

    @property
    def base_id(self) -> str:
        return self.job_id.split("_", 1)[0]


def _now() -> datetime:
    return datetime.now()


def _parse(output: str) -> list[QueuedJob]:
    jobs = []
    for line in output.splitlines():
        fields = split_fields(line, FIELD_COUNT)
        if fields is not None and fields[0]:
            jobs.append(QueuedJob(*fields))
    return jobs


def _mentioned_ids(query: str) -> list[str]:
    return list(dict.fromkeys(slurm_readonly.JOB_ID_IN_TEXT.findall(query or "")))


def _matches(job: QueuedJob, wanted: str) -> bool:
    return wanted in {job.job_id, job.base_id}


def _resources(job: QueuedJob) -> str:
    nodes = "1 node" if job.nodes == "1" else f"{job.nodes} nodes"
    parts = [f"{job.cpus} CPUs", nodes]
    if not is_unset(job.memory) and job.memory not in {"0", "0M"}:
        parts.append(f"{job.memory} memory")
    return ", ".join(parts)


def _limit(job: QueuedJob) -> str:
    words = time_limit_words(job.limit)
    return "no time limit" if words == "no limit" else f"{words} limit"


def _running_line(job: QueuedJob) -> str:
    used = human_duration(parse_duration(job.used))
    left_seconds = parse_duration(job.left)
    left = f", {human_duration(left_seconds)} left" if left_seconds is not None else ""
    return (
        f"- {job.job_id} on {job.partition}: {_resources(job)}; running for "
        f"{used} ({_limit(job)}{left})"
    )


def _pending_line(job: QueuedJob, now: datetime) -> str:
    code = reason_code(job.reason)
    reason = f"reason {code}" if code and code != "None" else "no reason given"
    start = parse_time(job.start)
    if start is None:
        estimate = "no start estimate yet"
    else:
        estimate = f"expected start {clock(start)} ({relative_time(start, now)})"
    return (
        f"- {job.job_id} on {job.partition}: {_resources(job)}, "
        f"{_limit(job)}; {reason}; {estimate}"
    )


def _summary(jobs: list[QueuedJob]) -> str:
    counts = Counter(job.state for job in jobs)
    parts = [
        f"{counts.pop('RUNNING', 0)} running",
        f"{counts.pop('PENDING', 0)} pending",
    ]
    parts += [f"{count} {state.lower()}" for state, count in sorted(counts.items())]
    noun = "job" if len(jobs) == 1 else "jobs"
    return f"You have {len(jobs)} {noun} in the queue: {', '.join(parts)}."


def _job_lines(result: slurm_readonly.SlurmResult, query: str, now: datetime):
    if not result.ok:
        return [
            f"Your jobs are unavailable: {slurm_readonly.unavailable_reason(result)}."
        ]

    jobs = _parse(result.output)
    mentioned = _mentioned_ids(query)
    missing = [
        wanted
        for wanted in mentioned
        if len(wanted.split("_", 1)[0]) >= MIN_REPORTED_ID_DIGITS
        and not any(_matches(job, wanted) for job in jobs)
    ]
    lines = []
    for wanted in missing:
        lines.append(
            f"Job {wanted} is not among your jobs in the queue: it may have "
            "finished (see your recent jobs), or it is not yours; ink shows "
            "details only for your own jobs."
        )

    if not jobs:
        if result.output.strip():
            return [*lines, "Your jobs are unavailable: could not parse squeue output."]
        return [
            *lines,
            "You have no jobs in the queue right now. Finished jobs leave the queue; "
            "your recent jobs cover those.",
        ]

    # Jobs the question names come first, then running, pending, the rest.
    order = {"RUNNING": 1, "PENDING": 2}
    jobs.sort(
        key=lambda job: (
            not any(_matches(job, wanted) for wanted in mentioned),
            order.get(job.state, 3),
        )
    )
    shown, hidden = jobs[:JOB_LIMIT], jobs[JOB_LIMIT:]

    lines += [_summary(jobs), "Job names are not included."]
    running = [job for job in shown if job.state == "RUNNING"]
    pending = [job for job in shown if job.state == "PENDING"]
    other = [job for job in shown if job.state not in order]
    if running:
        lines.append("Running:")
        lines += [_running_line(job) for job in running]
    if pending:
        lines.append("Pending:")
        lines += [_pending_line(job, now) for job in pending]
    if other:
        lines.append("Other states:")
        lines += [
            f"- {job.job_id} on {job.partition}: {job.state.lower()}" for job in other
        ]
    if hidden:
        lines.append(f"- and {len(hidden)} more jobs not listed")

    codes = list(
        dict.fromkeys(
            reason_code(job.reason)
            for job in pending
            if reason_code(job.reason) not in {"", "None"}
        )
    )
    if codes:
        lines.append("What the reasons mean:")
        lines += [f"- {code}: {explain(code)}." for code in codes]
    if any(parse_time(job.start) for job in pending):
        lines.append(
            "Start times are the scheduler's current estimate; they often move as "
            "other jobs finish or arrive."
        )
    if result.truncated:
        lines.append("The list was cut short because it is very long.")
    return lines


def run(query: str = "") -> str:
    """Summarise the user's own queued and running jobs."""
    result = slurm_readonly.run("my_queue")
    body_lines = _job_lines(result, query, _now())
    body_lines.extend(source_lines(result))
    return format_plugin_output("Your Slurm Jobs (live)", body_lines)
