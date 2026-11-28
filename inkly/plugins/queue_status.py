from __future__ import annotations

from collections import Counter

from inkly import slurm_readonly
from inkly.plugins._slurm_text import partition_label, source_lines, split_fields
from inkly.plugins.common import format_plugin_output, validate_plugin_meta


# Metadata describing this plugin.
# This is used by plugin discovery, retrieval, and runtime selection.
PLUGIN_META = {
    "name": "queue_status",
    "description": (
        "Summarizes the current Slurm queue and cluster load using squeue and "
        "sinfo: running and pending job counts, and per partition the nodes "
        "and CPUs in use, idle and unavailable. Counts only, never other "
        "users' jobs."
    ),
    "category": "queue-status",
    "example_queries": [
        "How busy is the cluster right now?",
        "Are there many pending jobs?",
        "What does the current queue look like?",
        "Is the gpu partition busy?",
        "How many idle nodes are there?",
        "How many CPUs are free right now?",
    ],
}

# Validate metadata at import time so invalid plugins fail early.
validate_plugin_meta(PLUGIN_META)

PARTITION_LIMIT = 8


def _job_count_lines(result: slurm_readonly.SlurmResult) -> list[str]:
    """Running and pending counts from one squeue call (states only)."""
    if not result.ok:
        reason = slurm_readonly.unavailable_reason(result)
        return [f"Queue counts unavailable: {reason}."]

    states = Counter(line.strip() for line in result.output.splitlines())
    states.pop("", None)
    running, pending = states["RUNNING"], states["PENDING"]
    other = sum(states.values()) - running - pending

    lines = [f"Running jobs: {running}", f"Pending jobs: {pending}"]
    if other:
        lines.append(f"Jobs in other states (completing, suspended and so on): {other}")
    if result.truncated:
        lines.append("The queue is very long, so these counts are a lower bound.")
    lines.append(
        "These counts cover the jobs your account can see; on clusters that hide "
        "other users' jobs they cover only yours."
    )
    return lines


def _count_pair(text: str) -> tuple[int, int, int, int] | None:
    """Parse sinfo's allocated/idle/other/total value."""
    parts = text.split("/")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return None
    allocated, idle, other, total = (int(part) for part in parts)
    return allocated, idle, other, total


def _partition_lines(result: slurm_readonly.SlurmResult) -> list[str]:
    """Per-partition node and CPU load from one sinfo call."""
    if not result.ok:
        reason = slurm_readonly.unavailable_reason(result)
        return [f"Partition summary unavailable: {reason}."]
    if not result.output.strip():
        return ["Partition summary unavailable: sinfo returned no data."]

    lines: list[str] = []
    rows = [row for row in result.output.splitlines() if row.strip()]
    for row in rows:
        fields = split_fields(row, 4)
        if fields is None:
            continue
        partition, availability, node_text, cpu_text = fields
        nodes, cpus = _count_pair(node_text), _count_pair(cpu_text)
        if nodes is None or cpus is None:
            continue
        lines.append(
            f"- {partition_label(partition, availability)}: "
            f"nodes {nodes[0]} in use, {nodes[1]} idle, {nodes[2]} unavailable, "
            f"{nodes[3]} total; "
            f"CPUs {cpus[0]} in use, {cpus[1]} idle, {cpus[2]} unavailable, "
            f"{cpus[3]} total"
        )

    if not lines:
        return ["Partition summary unavailable: could not parse sinfo output."]
    if len(lines) > PARTITION_LIMIT:
        hidden = len(lines) - PARTITION_LIMIT
        lines = lines[:PARTITION_LIMIT] + [f"- and {hidden} more partitions"]
    return lines


def run() -> str:
    """
    Entry point for the queue-status plugin.

    Collects queue counts and per-partition load through the read-only
    Slurm allowlist and formats them into a plugin output block.
    """
    queue = slurm_readonly.run("queue_states")
    partitions = slurm_readonly.run("partition_load")

    body_lines = _job_count_lines(queue)
    body_lines.append("Partitions:")
    body_lines.extend(_partition_lines(partitions))
    body_lines.extend(source_lines(queue, partitions))

    return format_plugin_output("Queue Status", body_lines)
