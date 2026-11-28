from __future__ import annotations

from datetime import datetime

import pytest

from inkly.plugins import _slurm_text as text


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("5:07", 307),
        ("02:03:04", 7384),
        ("1-02:03:04", 93784),
        ("01:02.345", 62),
        ("2-12", 216000),
        ("0:00", 0),
        ("UNLIMITED", None),
        ("Partition_Limit", None),
        ("INVALID", None),
        ("", None),
    ],
)
def test_parse_duration_accepts_squeue_and_sacct_forms(value, seconds):
    assert text.parse_duration(value) == seconds


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (45, "45 s"),
        (720, "12 m"),
        (12000, "3 h 20 m"),
        (7200, "2 h"),
        (183600, "2 d 3 h"),
    ],
)
def test_human_duration(seconds, words):
    assert text.human_duration(seconds) == words


@pytest.mark.parametrize(
    ("value", "mib"),
    [("4000M", 4000), ("16G", 16384), ("2048K", 2), ("128000", 128000), ("1.5G", 1536)],
)
def test_parse_memory_mib(value, mib):
    assert text.parse_memory_mib(value) == mib


def test_maxrss_without_a_unit_is_bytes():
    assert text.parse_bytes_as_mib(str(3 * 1024**2)) == 3
    assert text.parse_bytes_as_mib("2048K") == 2
    assert text.parse_bytes_as_mib("") is None


def test_time_parsing_and_relative_words():
    now = datetime(2026, 11, 25, 15, 0)

    assert text.parse_time("2026-11-25T18:20:00") == datetime(2026, 11, 25, 18, 20)
    assert text.parse_time("N/A") is None
    assert text.parse_time("Unknown") is None
    assert (
        text.relative_time(datetime(2026, 11, 25, 18, 20), now) == "in about 3 h 20 m"
    )
    assert text.relative_time(datetime(2026, 11, 25, 14, 50), now) == "about 10 m ago"


def test_partition_label_marks_the_default():
    assert text.partition_label("general*", "up") == "general (default, up)"
    assert text.partition_label("gpu") == "gpu"
