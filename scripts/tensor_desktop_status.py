"""Desktop support contact and job-summary wording. No Tk, network or Slurm calls."""

from collections import Counter

import tensor_picker as picker


# The maintainer approved public display of this address in the Desktop window.
SUPPORT_EMAIL = "rrv9177@uncw.edu"
SUPPORT_TEXT = f"Questions or issues? Contact: {SUPPORT_EMAIL}"
SUPPORT_URL = f"mailto:{SUPPORT_EMAIL}"

# Display order for the summary. Unlisted scheduler states follow alphabetically.
ORDER = (
    "PREPARED",
    "SUBMISSION_UNKNOWN",
    "SUBMITTED",
    "PENDING",
    "CONFIGURING",
    "RUNNING",
    "COMPLETING",
    "SUSPENDED",
    "REQUEUED",
    "RESIZING",
    "COMPLETED",
    "RETURNED",
    "FAILED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "CANCELLED",
    "NODE_FAIL",
    "PREEMPTED",
    "UNKNOWN",
)
# "SUBMITTED" is shown after a confirmed sbatch reply, before the first status read.
ON_HPC = set(picker.ACTIVE) | {"SUBMITTED"}
UNCONFIRMED = {"SUBMISSION_UNKNOWN", "UNKNOWN"}
FINISHED = {"COMPLETED", "RETURNED"}


# Group scheduler detail such as "CANCELLED by 1234" under its state.
state_name = picker.state_name


def state_counts(rows):
    counts = Counter(state_name(row.get("state")) for row in rows)
    ordered = [state for state in ORDER if state in counts]
    ordered += sorted(set(counts) - set(ORDER))
    return ", ".join(f"{counts[state]} {state}" for state in ordered)


def review_overview(review):
    """Pre-submit wording for the current, still-unconsumed preview approval."""
    overview = (
        f"{review['count']} prepared job(s). If all start together: "
        f"{review['cpus']} CPUs and {review['memory_mib']:,} MiB "
        "Slurm memory. Slurm decides start times."
    )
    if review.get("recovered"):
        overview += (
            "\nRecovered handoff(s): "
            + ", ".join(review["recovered"])
            + ". Check for any earlier running calculation before submitting a replacement."
        )
    return overview


def _requested(rows):
    cpus = memory = 0
    for row in rows:
        resources = row.get("resources") or {}
        cpus += int(resources.get("cpus") or 0)
        memory += int(resources.get("slurm_memory_mib") or 0)
    return cpus, memory


def overview_text(review, rows, unconfirmed=(), submitting=False):
    """Describe the displayed rows; never call later work merely "prepared".

    ``rows`` are the latest displayed row payloads for the current file list.
    ``unconfirmed`` holds paths whose PREPARED display predates an interrupted
    submission, so the HPC receipt may already record a submission attempt.
    ``submitting`` means an approved submission is in flight right now.
    Resource figures are requested Slurm limits, never measured usage.
    """
    rows = list(rows)
    if review and review.get("count"):
        overview = review_overview(review)
        recorded = [row for row in rows if state_name(row.get("state")) != "PREPARED"]
        if recorded:
            overview += (
                f"\nAlso in this list: {state_counts(recorded)}. Only the prepared "
                "job(s) are submitted; recorded jobs are never resubmitted."
            )
        return overview
    if not rows:
        return None

    states = {state_name(row.get("state")) for row in rows}
    notes = []
    prepared = [row for row in rows if state_name(row.get("state")) == "PREPARED"]
    if any(row.get("path") in unconfirmed for row in prepared):
        notes.append(
            "The last submission was interrupted, so PREPARED rows may already be "
            "submitted. Preview these files again before any new submission."
        )
    elif prepared and submitting:
        notes.append("Submitting the approved PREPARED files; wait for job IDs.")
    elif prepared:
        notes.append("PREPARED files are not submitted.")
    if states & UNCONFIRMED:
        notes.append(
            "Unconfirmed submissions: check your Slurm jobs before submitting again."
        )
    on_hpc = [row for row in rows if state_name(row.get("state")) in ON_HPC]
    if on_hpc:
        cpus, memory = _requested(on_hpc)
        notes.append(
            f"Requested limits for {len(on_hpc)} submitted job(s): {cpus} CPUs, "
            f"{memory:,} MiB Slurm memory (not measured usage)."
        )
    if "COMPLETED" in states:
        notes.append("COMPLETED logs return after verification.")
    if "CANCELLED" in states:
        notes.append(
            "CANCELLED jobs will not run; to submit one again, select it and choose "
            "Run again."
        )
    if states - ON_HPC - UNCONFIRMED - FINISHED - {"PREPARED", "CANCELLED"}:
        notes.append("Ended jobs need inspection; Inkly never resubmits them.")
    if "RETURNED" in states:
        notes.append("RETURNED logs are verified; use Open results folder.")

    summary = f"Current status of {len(rows)} file(s): {state_counts(rows)}."
    return summary + ("\n" + " ".join(notes) if notes else "")


def job_details_text(details):
    """User-requested output; never pass this text to the diagnostic journal."""
    state = state_name(details.get("state", details.get("submission")))
    lines = [
        f"{details['name']} | Job {details.get('job_id') or 'not submitted'} | {state}"
    ]
    lines += [
        f"Exit code: {details.get('exit_code') or 'Unavailable'}",
        f"Elapsed run time: {details.get('elapsed') or 'Unavailable'}",
        f"Observed (UTC): {details.get('observed_at') or 'Unavailable'}",
    ]
    if state == "PENDING":
        lines += [
            f"Pending reason: {details.get('reason') or 'Unavailable'}",
            f"Estimated start (HPC local time): {details.get('start_time') or 'Unavailable'}",
            "Slurm estimates can change. Elapsed run time does not include time waiting in the queue.",
        ]
    elif details.get("start_time"):
        lines.append(f"Started (HPC local time): {details['start_time']}")
    if details.get("unavailable"):
        lines.append(details["unavailable"])
    if state in {"FAILED", "TIMEOUT", "OUT_OF_MEMORY"}:
        lines += [
            "",
            "Gaussian error line: "
            + (details.get("gaussian_error") or "Not found in the available excerpt."),
        ]
        for log in details.get("logs", ()):
            lines += [
                "",
                log["name"] + (" (excerpt truncated)" if log.get("truncated") else ""),
            ]
            lines.append(
                log.get("text")
                or (
                    "Log is empty."
                    if log.get("available")
                    else "Log unavailable (missing, unreadable or unsafe file)."
                )
            )
        lines += [
            "",
            "Excerpts may contain research data. They are shown here only, not saved to Activity or diagnostics.",
            "A Gaussian error line may not explain a scheduler timeout or memory kill. No job was retried.",
        ]
    return "\n".join(lines)
