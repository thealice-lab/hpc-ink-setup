"""Turn Slurm's printed values into plain words for ink's Slurm plugins.

Plugins do the parsing and arithmetic here so the language model only has to
rephrase finished sentences. Every parser returns None for values it does not
understand, so an unusual Slurm version degrades to "unknown", not an error.
"""

from __future__ import annotations

import re
from datetime import datetime

from inkly.plugins.common import SOURCE_COMMAND_PREFIX
from inkly.slurm_readonly import SlurmResult

_DURATION = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)$"
)
_DAYS_HOURS = re.compile(r"^(?P<days>\d+)-(?P<hours>\d+)$")
_MEMORY = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[KMGTP]?)$", re.IGNORECASE)
_MEMORY_UNITS_MIB = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024**2, "P": 1024**3}
NO_VALUE = {"", "n/a", "none", "unknown", "(null)", "invalid", "not_set"}


def clean(value: str) -> str:
    return value.strip()


def is_unset(value: str) -> bool:
    return clean(value).casefold() in NO_VALUE


def parse_duration(value: str) -> int | None:
    """Seconds in a Slurm duration such as 5:07, 2:03:04 or 1-02:03:04.

    Also accepts sacct's fractional seconds (01:02.345) and days-hours.
    UNLIMITED, Partition_Limit and the like give None.
    """
    text = clean(value)
    match = _DURATION.match(text)
    if match:
        days = int(match["days"] or 0)
        hours = int(match["hours"] or 0)
        minutes = int(match["minutes"])
        seconds = float(match["seconds"])
        return int(((days * 24 + hours) * 60 + minutes) * 60 + seconds)
    match = _DAYS_HOURS.match(text)
    if match:
        return (int(match["days"]) * 24 + int(match["hours"])) * 3600
    return None


def human_duration(seconds: int | float | None) -> str:
    """Short plain duration: 2 d 3 h, 3 h 20 m, 12 m, 45 s."""
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} m" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} m"
    return f"{secs} s"


def time_limit_words(value: str) -> str:
    """A time limit in plain words; UNLIMITED/infinite becomes 'no limit'."""
    text = clean(value)
    if text.casefold() in {"unlimited", "infinite"}:
        return "no limit"
    seconds = parse_duration(text)
    return human_duration(seconds) if seconds is not None else text


def parse_memory_mib(value: str) -> float | None:
    """MiB in a Slurm size such as 4000M, 16G, 123456K or a bare number of MB."""
    match = _MEMORY.match(clean(value))
    if not match:
        return None
    unit = match["unit"].upper() or "M"
    return float(match["amount"]) * _MEMORY_UNITS_MIB[unit]


def parse_bytes_as_mib(value: str) -> float | None:
    """MiB in a sacct MaxRSS value; a bare number there means bytes."""
    match = _MEMORY.match(clean(value))
    if not match:
        return None
    if not match["unit"]:
        return float(match["amount"]) / 1024**2
    return parse_memory_mib(value)


def human_memory(mib: float | None) -> str:
    if mib is None:
        return "unknown"
    if mib >= 1024:
        return f"{mib / 1024:.1f} GiB"
    return f"{mib:.0f} MiB"


def parse_time(value: str) -> datetime | None:
    """A Slurm timestamp printed with SLURM_TIME_FORMAT=standard."""
    text = clean(value)
    if is_unset(text):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def relative_time(when: datetime, now: datetime) -> str:
    delta = int((when - now).total_seconds())
    if abs(delta) < 60:
        return "about now"
    if delta > 0:
        return f"in about {human_duration(delta)}"
    return f"about {human_duration(-delta)} ago"


def clock(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


def partition_label(name: str, availability: str = "") -> str:
    """'general*' -> 'general (default, up)'."""
    text = clean(name)
    notes = []
    if text.endswith("*"):
        text = text[:-1]
        notes.append("default")
    availability = clean(availability)
    if availability:
        notes.append(availability)
    return f"{text} ({', '.join(notes)})" if notes else text


def split_fields(line: str, count: int) -> list[str] | None:
    """Split a '|'-separated Slurm line, or None if it has the wrong width."""
    fields = line.rstrip("\n").split("|")
    if len(fields) != count:
        return None
    return [clean(field) for field in fields]


def source_lines(*results: SlurmResult) -> list[str]:
    """Marker lines naming the commands whose data a plugin used."""
    seen = dict.fromkeys(
        result.shown_command for result in results if result and result.ok
    )
    return [f"{SOURCE_COMMAND_PREFIX}{command}" for command in seen]
