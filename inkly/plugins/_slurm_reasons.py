"""Plain-language notes for Slurm's pending-job reason codes.

The notes are written here, not left to the language model, so researchers
get the same correct explanation every time. Unknown codes fall back to a
note based on the code's prefix, then to a generic note.
"""

from __future__ import annotations

REASONS = {
    "Priority": ("other jobs ahead of it have higher priority, so it waits its turn"),
    "Resources": (
        "it is next in line and waiting for enough free CPUs, memory or nodes"
    ),
    "Dependency": "it is waiting for a job it depends on to finish",
    "DependencyNeverSatisfied": (
        "the job it depends on did not end as required, so this job will never "
        "start on its own; you may want to cancel it"
    ),
    "BeginTime": "it was submitted to start later, at a set time",
    "JobHeldUser": "you put it on hold; it starts only after you release it",
    "JobHeldAdmin": "an administrator put it on hold; ask HPC support",
    "launch failed requeued held": (
        "it failed to start, was put back in the queue and held; ask HPC support "
        "or release it"
    ),
    "ReqNodeNotAvail": (
        "some nodes it can use are down, drained or reserved (often for maintenance)"
    ),
    "Reservation": "it is waiting for its reservation to begin",
    "PartitionDown": "its partition is down",
    "PartitionInactive": "its partition is not accepting jobs to run right now",
    "PartitionTimeLimit": (
        "its time limit is longer than the partition allows, so it cannot start "
        "until the limit is lowered"
    ),
    "PartitionNodeLimit": (
        "it asks for more nodes than the partition allows per job, so it cannot "
        "start as submitted"
    ),
    "QOSMaxJobsPerUserLimit": (
        "you already have the most running jobs your QOS allows; it starts when "
        "one of them ends"
    ),
    "QOSMaxSubmitJobPerUserLimit": ("you have the most queued jobs your QOS allows"),
    "QOSMaxCpuPerUserLimit": (
        "your running jobs already use the most CPUs your QOS allows"
    ),
    "QOSMaxNodePerUserLimit": (
        "your running jobs already use the most nodes your QOS allows"
    ),
    "QOSMaxMemoryPerUser": (
        "your running jobs already use the most memory your QOS allows"
    ),
    "QOSMaxWallDurationPerJobLimit": (
        "its time limit is longer than your QOS allows, so it cannot start as submitted"
    ),
    "QOSGrpCpuLimit": "the CPUs shared by your QOS group are all in use",
    "QOSGrpNodeLimit": "the nodes shared by your QOS group are all in use",
    "QOSGrpJobsLimit": "your QOS group already has the most running jobs it may",
    "AssocMaxJobsLimit": (
        "your account already has the most running jobs it may; it starts when one ends"
    ),
    "AssocGrpCpuLimit": "the CPUs allowed for your account are all in use",
    "AssocGrpNodeLimit": "the nodes allowed for your account are all in use",
    "AssocGrpJobsLimit": "your account group already has the most running jobs",
    "AssocGrpCPUMinutesLimit": (
        "your account's CPU-time allocation is used up; ask your PI or HPC support"
    ),
    "AssocGrpBillingMinutes": (
        "your account's allocation is used up; ask your PI or HPC support"
    ),
    "AssocMaxWallDurationPerJobLimit": (
        "its time limit is longer than your account allows, so it cannot start "
        "as submitted"
    ),
    "InvalidAccount": "its account is not valid, so it will not start",
    "InvalidQOS": "its QOS is not valid, so it will not start",
    "BadConstraints": (
        "no node matches the features or resources it asks for, so it cannot "
        "start as submitted"
    ),
    "NodeDown": "a node it needs is down",
    "Cleaning": "it was requeued and Slurm is still cleaning up its last run",
}

PREFIX_NOTES = (
    ("QOS", "a limit of your quality of service (QOS) has been reached"),
    ("Assoc", "a limit on your account or association has been reached"),
    ("Partition", "a limit of its partition prevents it from starting now"),
)


def reason_code(reason: str) -> str:
    """The code part of squeue's reason, without node lists or parentheses."""
    text = reason.strip().strip("()")
    for separator in (",", "("):
        text = text.split(separator, 1)[0]
    return text.strip()


def explain(code: str) -> str:
    if code in REASONS:
        return REASONS[code]
    for prefix, note in PREFIX_NOTES:
        if code.startswith(prefix):
            return note
    return "Slurm gave this code; the squeue manual page lists what it means"
