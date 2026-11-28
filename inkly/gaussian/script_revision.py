"""Checks for a researcher's own edits to a prepared job script.

Every part of the script can be changed or replaced. Only problems that make
the script unusable as a Slurm batch script stop a save: text that is not
UTF-8, NUL bytes, an oversized script, a missing ``#!`` line, or a script the
scheduler itself refuses (``sbatch --test-only``, which submits nothing).
Everything else becomes a plain warning, so the researcher decides.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import os
import pwd
import re
import shlex
import subprocess

MAX_SCRIPT = 64 * 1024
# Options whose value may follow as the next word ("--time 2:00:00").
_LONG_VALUES = {
    "account",
    "array",
    "chdir",
    "cpus-per-task",
    "error",
    "hint",
    "job-name",
    "mem",
    "mem-per-cpu",
    "nodes",
    "ntasks",
    "output",
    "partition",
    "qos",
    "time",
}
_SHORT = {
    "A": "account",
    "a": "array",
    "c": "cpus-per-task",
    "D": "chdir",
    "e": "error",
    "J": "job-name",
    "N": "nodes",
    "n": "ntasks",
    "o": "output",
    "p": "partition",
    "q": "qos",
    "t": "time",
}
# sbatch says these when Slurm's controller cannot be reached, not when the
# script is wrong; the check then counts as not run and the save goes ahead.
_UNAVAILABLE = (
    "unable to contact slurm controller",
    "socket timed out",
    "backup controller in standby",
    "zero bytes were transmitted",
    "connection refused",
    "connection timed out",
    "temporarily unable",
)
_MEMORY = re.compile(r"([0-9]+)([KMGT]?)B?", re.I)
_TIME_PARTS = re.compile(r"[0-9]+(?::[0-9]+){0,2}")


def normalize(script):
    """Return the text to store, or raise ValueError for an unusable script."""
    if not isinstance(script, str):
        raise ValueError("The script must be text")
    try:
        size = len(script.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("The script must be UTF-8 text") from None
    if size > MAX_SCRIPT:
        raise ValueError("The script must be at most 64 KiB")
    if "\x00" in script:
        raise ValueError("The script must be a text file without NUL bytes")
    text = script.replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("The script is empty")
    if not text.startswith("#!"):
        raise ValueError(
            "The script's first line must start with #! (for example #!/bin/bash -l)"
        )
    return text if text.endswith("\n") else text + "\n"


def directives(text):
    """Read #SBATCH options the way sbatch does: before the first command."""
    options = {}
    unreadable = []
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        if not line.startswith("#SBATCH"):
            continue
        try:
            words = shlex.split(line[len("#SBATCH") :], comments=True)
        except ValueError:
            unreadable.append(line.strip())
            continue
        index = 0
        while index < len(words):
            word = words[index]
            index += 1
            name = value = None
            if word.startswith("--"):
                name, equals, value = word[2:].partition("=")
                if not equals and name in _LONG_VALUES and index < len(words):
                    value = words[index]
                    index += 1
            elif word.startswith("-") and len(word) > 1 and word[1] in _SHORT:
                name = _SHORT[word[1]]
                value = word[2:].lstrip("=")
                if not value and index < len(words):
                    value = words[index]
                    index += 1
            if name:
                options[name] = value or ""
    return options, unreadable


def minutes(value):
    """Slurm time limit in whole minutes; None for unlimited or unreadable."""
    value = value.strip().lower()
    if value in {"infinite", "unlimited", ""}:
        return None
    days = 0
    if "-" in value:
        day, value = value.split("-", 1)
        if not day.isdigit() or not _TIME_PARTS.fullmatch(value):
            return None
        days = int(day)
        parts = [int(part) for part in value.split(":")] + [0, 0]
        hours, mins, secs = parts[:3]
    else:
        if not _TIME_PARTS.fullmatch(value):
            return None
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 1:
            hours, mins, secs = 0, parts[0], 0
        elif len(parts) == 2:
            hours, (mins, secs) = 0, parts
        else:
            hours, mins, secs = parts
    return days * 1440 + hours * 60 + mins + (1 if secs else 0)


def memory_mib(value):
    match = _MEMORY.fullmatch(value.strip())
    if not match or int(match[1]) == 0:
        return None  # 0 asks for all of a node's memory.
    amount, unit = int(match[1]), match[2].upper() or "M"
    scale = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit]
    return max(1, int(amount * scale))


def script_resources(options):
    """What the saved script asks Slurm for; None where Slurm's default applies."""
    cpus = options.get("cpus-per-task", "")
    cpus = int(cpus) if cpus.isdigit() and int(cpus) > 0 else None
    memory = memory_mib(options["mem"]) if options.get("mem") else None
    per_cpu = options.get("mem-per-cpu")
    if memory is None and per_cpu and memory_mib(per_cpu):
        memory = memory_mib(per_cpu) * (cpus or 1)
    limit = options.get("time")
    total = minutes(limit) if limit else None
    return {
        "cpus": cpus,
        "slurm_memory_mib": memory,
        "time_limit": limit or None,
        "hours": -(-total // 60) if total else None,
        "partition": options.get("partition") or None,
    }


def _variable(text, name):
    match = re.search(rf"(?m)^{name}=([0-9]+)\s*$", text)
    return int(match[1]) if match else None


def _inkly_changes(text, generated, options):
    lines = set(text.splitlines())
    original = generated.splitlines()
    warnings = []
    checksum = [line for line in original if "sha256sum -c" in line]
    if checksum and checksum[0] not in lines:
        warnings.append(
            "The check that the job runs exactly the prepared input was changed or "
            "removed."
        )
    output = [line for line in original if line.startswith("exec 3> ")]
    if output and output[0] not in lines:
        name = output[0].split(" ", 2)[2].strip("'")
        warnings.append(
            f"Inkly no longer writes Gaussian's output to {name}. Check & return "
            f"results brings a job back only from {name} in the job folder, with "
            "Gaussian's normal-termination lines."
        )
    launch = [line for line in original if 'g16 -c="$CPU_IDS"' in line]
    if launch and not lines & set(launch):
        warnings.append(
            "Gaussian is no longer started the way Inkly starts it, on one CPU per "
            "physical core of the job's allocation."
        )
    if options.get("nodes", "1") != "1" or options.get("ntasks", "1") != "1":
        warnings.append(
            "Inkly runs Gaussian as one task on one node; more nodes or tasks are "
            "not used by Gaussian started this way."
        )
    if "#SBATCH --hint=nomultithread" in original and (
        options.get("hint") != "nomultithread"
    ):
        warnings.append(
            "Without --hint=nomultithread, Gaussian's CPUs may share physical cores."
        )
    for name, effect in [
        (
            "array",
            "Desktop tracks one job per file, so it will not follow each array task",
        ),
        ("chdir", "Desktop looks for results in the job folder Inkly prepared"),
        ("output", "Job details shows Slurm's log only from slurm-<job ID>.out"),
        ("error", "Job details shows Slurm's log only from slurm-<job ID>.out"),
    ]:
        if name in options:
            warnings.append(f"--{name} is set. {effect}.")
    return warnings


def _consistency(text, resources, fraction):
    warnings = []
    requested = _variable(text, "REQUESTED_CPUS")
    cpus = resources["cpus"]
    if requested and cpus and cpus < requested:
        warnings.append(
            f"The script asks Slurm for {cpus} CPUs but starts Gaussian on "
            f"{requested} (REQUESTED_CPUS). The job will stop before Gaussian runs."
        )
    elif requested and cpus and cpus > requested:
        warnings.append(
            f"The script asks Slurm for {cpus} CPUs but Gaussian uses only "
            f"{requested} (REQUESTED_CPUS)."
        )
    gaussian = _variable(text, "GAUSSIAN_MEM_MB")
    memory = resources["slurm_memory_mib"]
    if gaussian and memory:
        # Gaussian MB are decimal megabytes; Slurm M are MiB.
        if gaussian * 10**6 > memory * 2**20:
            warnings.append(
                f"Gaussian is given {gaussian} MB (GAUSSIAN_MEM_MB), more than the "
                f"{memory} MiB the script asks Slurm for. Slurm will likely stop the "
                "job for running out of memory."
            )
        elif fraction is not None and Decimal(gaussian) > (
            Decimal(memory) * fraction
        ).to_integral_value(rounding=ROUND_FLOOR):
            warnings.append(
                f"Gaussian's {gaussian} MB leaves less room than Inkly's usual "
                f"headroom within {memory} MiB. Gaussian uses some memory beyond "
                "%mem, so Slurm may stop the job for running out of memory."
            )
    if warnings:
        warnings.append(
            "Desktop's CPU and memory fields prepare these values together; "
            "they are the safer way to change them."
        )
    return warnings


def partitions(scheduler, directory):
    """{name: limits} from sinfo, plus the default name; None if unavailable."""
    try:
        result = scheduler(["sinfo", "--noheader", "-o", "%P|%l|%c|%m"], directory)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0:
        return None, None
    found, default = {}, None
    for line in result.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 4 or not fields[0]:
            continue
        name = fields[0].rstrip("*")
        if fields[0].endswith("*"):
            default = name
        cpus = fields[2].rstrip("+")
        memory = fields[3].rstrip("+")
        entry = found.setdefault(name, {"minutes": None, "cpus": 0, "memory_mib": 0})
        limit = minutes(fields[1])
        if limit is not None:
            entry["minutes"] = max(entry["minutes"] or 0, limit)
        elif fields[1].strip().lower() in {"infinite", "unlimited"}:
            entry["minutes"] = None
        if cpus.isdigit():
            entry["cpus"] = max(entry["cpus"], int(cpus))
        if memory.isdigit():
            entry["memory_mib"] = max(entry["memory_mib"], int(memory))
    return (found or None), default


def _limits(resources, listed, default):
    name = resources["partition"] or default
    limits = (listed or {}).get(name)
    if not limits:
        return []
    warnings = []
    wanted = minutes(resources["time_limit"]) if resources["time_limit"] else None
    if wanted and limits["minutes"] and wanted > limits["minutes"]:
        warnings.append(
            f"The time limit {resources['time_limit']} is longer than partition "
            f"{name} allows. Slurm may refuse the job or leave it waiting."
        )
    if resources["cpus"] and limits["cpus"] and resources["cpus"] > limits["cpus"]:
        warnings.append(
            f"No node in partition {name} has {resources['cpus']} CPUs "
            f"(largest: {limits['cpus']})."
        )
    memory = resources["slurm_memory_mib"]
    if memory and limits["memory_mib"] and memory > limits["memory_mib"]:
        warnings.append(
            f"No node in partition {name} has {memory} MiB of memory "
            f"(largest: {limits['memory_mib']} MiB)."
        )
    return warnings


def _accounting(scheduler, directory, options):
    """Warn about an --account or --qos that accounting does not list for you.

    Only a warning: clusters that do not enforce accounts accept any name and
    record it, and clusters that do already refuse it in sbatch --test-only.
    """
    account, qos = options.get("account"), options.get("qos")
    if not account and not qos:
        return []
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        result = scheduler(
            [
                "sacctmgr",
                "-n",
                "-P",
                "show",
                "assoc",
                f"user={user}",
                "format=account,qos",
            ],
            directory,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    accounts, allowed = set(), set()
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        fields = line.strip().split("|")
        if len(fields) >= 2 and fields[0].strip():
            accounts.add(fields[0].strip().lower())
            allowed |= {name.strip().lower() for name in fields[1].split(",")}
    allowed.discard("")
    if not accounts:
        return []  # No accounting records to compare with.
    warnings = []
    if account and account.lower() not in accounts:
        warnings.append(
            f"Slurm's accounting lists no account {account} for you (yours: "
            f"{', '.join(sorted(accounts))}). Depending on the cluster, Slurm "
            "refuses the job when it is submitted or runs it anyway."
        )
    if qos and allowed and qos.lower() not in allowed:
        warnings.append(
            f"Slurm's accounting does not list QOS {qos} for you (yours: "
            f"{', '.join(sorted(allowed))}). Depending on the cluster, Slurm "
            "refuses the job when it is submitted or runs it anyway."
        )
    return warnings


def _refusal(stderr, stdout=""):
    """Slurm's own reason, from its error line or else its first line."""
    lines = [line.strip() for line in (stderr + "\n" + stdout).splitlines()]
    lines = [line for line in lines if line]
    errors = [line.split("error:", 1)[1] for line in lines if "error:" in line]
    for text in errors or [line.removeprefix("sbatch:") for line in lines[:1]]:
        text = text.strip().removeprefix("Batch job submission failed:").strip()
        text = "".join(c for c in text if c.isprintable())[:200]
        if text:
            return f"Slurm would refuse this script: {text}"
    return "Slurm would refuse this script; check its #SBATCH lines"


def test_only(scheduler, directory, filename, comment):
    """Ask the scheduler to validate the script; nothing is submitted.

    Returns None when Slurm accepts it, raises ValueError when Slurm refuses,
    and returns False when the check itself could not run: no sbatch, a
    timeout, or a controller that does not answer.
    """
    try:
        result = scheduler(
            ["sbatch", "--test-only", f"--comment={comment}", filename], directory
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0:
        return None
    stderr = result.stderr or ""
    if any(phrase in stderr.lower() for phrase in _UNAVAILABLE):
        return False
    raise ValueError(_refusal(stderr, result.stdout or ""))


def check(text, generated, scheduler, directory, filename, comment, profile):
    """Gate and warnings for a script that differs from the generated one."""
    accepted = test_only(scheduler, directory, filename, comment)
    options, unreadable = directives(text)
    resources = script_resources(options)
    listed, default = partitions(scheduler, directory)
    warnings = []
    if accepted is False:
        name = resources["partition"]
        if name and listed and name not in listed:
            raise ValueError(f"Slurm lists no partition named {name} on this HPC")
        warnings.append(
            "Slurm could not check this script just now. It was saved, but Slurm "
            "may still refuse it when it is submitted."
        )
    for line in unreadable:
        warnings.append(f"Inkly could not read this line: {line}")
    try:
        fraction = Decimal(
            str(profile["software"]["gaussian"]["slurm"]["auto_memory_fraction"])
        )
    except (KeyError, TypeError, InvalidOperation):
        fraction = None
    warnings += _inkly_changes(text, generated, options)
    warnings += _consistency(text, resources, fraction)
    warnings += _limits(resources, listed, default)
    warnings += _accounting(scheduler, directory, options)
    return resources, warnings
