from __future__ import annotations

import re

from inkly import slurm_readonly
from inkly.plugins._slurm_text import (
    clean,
    is_unset,
    partition_label,
    source_lines,
    split_fields,
    time_limit_words,
)
from inkly.plugins.common import format_plugin_output, validate_plugin_meta


# Metadata describing this plugin.
# This is used by plugin discovery, retrieval, and runtime selection.
PLUGIN_META = {
    "name": "node_info",
    "description": (
        "Lists cluster partitions with node resources (CPUs, memory, GPUs) and "
        "partition limits (maximum and default time limit, nodes per job) "
        "using sinfo."
    ),
    "category": "node-info",
    "example_queries": [
        "What partitions are available?",
        "Which nodes have GPUs?",
        "What resources does this cluster have?",
        "How much memory do nodes provide?",
        "What is the maximum time limit for a job?",
        "What is the default walltime on each partition?",
    ],
}

# Validate metadata at import time so invalid plugins fail early.
validate_plugin_meta(PLUGIN_META)

ROW_LIMIT = 8
UNAVAILABLE_LIMITS = (
    "Not available from sinfo: per-partition memory defaults and QOS limits; "
    "check the cluster's documentation or ask HPC support."
)
_JOB_SIZE = re.compile(r"^(\d+)-(\d+|infinite)$")


def _gres_words(gres: str) -> str:
    if is_unset(gres):
        return "no GPUs"
    return f"GPUs/GRES {gres}"


def _limit_words(time_limit: str, default_time: str, job_size: str) -> str:
    limit = time_limit_words(time_limit)
    parts = ["no time limit" if limit == "no limit" else f"time limit {limit}"]
    if not is_unset(default_time):
        parts[0] += f" (default {time_limit_words(default_time)})"

    match = _JOB_SIZE.match(clean(job_size))
    if match and match[2] != "infinite":
        low, high = match[1], match[2]
        span = low if low == high else f"{low}-{high}"
        parts.append(f"jobs may use {span} node{'s' if span != '1' else ''}")
    return "; ".join(parts)


def _get_node_info_lines(result: slurm_readonly.SlurmResult) -> list[str]:
    """Format sinfo's partition resources and limits."""
    if not result.ok:
        reason = slurm_readonly.unavailable_reason(result)
        return [f"Node information unavailable: {reason}."]
    if not result.output.strip():
        return ["Node information unavailable: sinfo returned no data."]

    lines: list[str] = []
    for row in result.output.splitlines():
        if not row.strip():
            continue
        fields = split_fields(row, 9)
        if fields is None:
            continue
        (
            partition,
            availability,
            nodes,
            cpus,
            memory,
            gres,
            time_limit,
            default_time,
            job_size,
        ) = fields
        lines.append(
            f"- {partition_label(partition, availability)}: "
            f"{nodes} node{'' if nodes == '1' else 's'}, "
            f"{cpus} CPUs/node, {memory} MB/node, {_gres_words(gres)}; "
            f"{_limit_words(time_limit, default_time, job_size)}"
        )

    if not lines:
        return ["Node information unavailable: could not parse sinfo output."]
    if len(lines) > ROW_LIMIT:
        hidden = len(lines) - ROW_LIMIT
        lines = lines[:ROW_LIMIT] + [f"- and {hidden} more partition rows"]
    return [*lines, UNAVAILABLE_LIMITS]


def run() -> str:
    """
    Entry point for the node-info plugin.

    Collects partition resources and limits through the read-only Slurm
    allowlist and formats them into a plugin output block.
    """
    result = slurm_readonly.run("partition_resources")

    body_lines = ["Partitions and resources:"]
    body_lines.extend(_get_node_info_lines(result))
    body_lines.extend(source_lines(result))

    return format_plugin_output("Node / Partition Information", body_lines)
