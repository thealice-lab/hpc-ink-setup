from __future__ import annotations

import subprocess
from datetime import date

import pytest

from inkly import slurm_readonly
from inkly.slurm_readonly import SlurmQueryRefused


# Programs that submit, cancel, hold or change jobs or accounts, or start work.
CHANGING_PROGRAMS = {
    "sbatch",
    "scancel",
    "scontrol",
    "srun",
    "salloc",
    "sacctmgr",
    "strigger",
    "sbcast",
    "sattach",
    "scrontab",
}


@pytest.fixture
def fake_slurm(monkeypatch):
    """Pretend every Slurm program exists and record each call."""
    calls = []
    state = {"stdout": b"", "returncode": 0, "raise": None}

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if state["raise"] is not None:
            raise state["raise"]
        kwargs["stdout"].write(state["stdout"])
        return subprocess.CompletedProcess(argv, state["returncode"])

    monkeypatch.setattr(slurm_readonly.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(slurm_readonly.subprocess, "run", fake_run)
    monkeypatch.setattr(slurm_readonly, "_uid", lambda: "4242")
    monkeypatch.setattr(slurm_readonly, "_today", lambda: date(2026, 11, 25))
    return calls, state


def test_allowlist_contains_only_read_only_programs():
    programs = {program for program, _ in slurm_readonly.QUERIES.values()}

    assert programs <= slurm_readonly.READ_ONLY_PROGRAMS
    assert not slurm_readonly.READ_ONLY_PROGRAMS & CHANGING_PROGRAMS
    for _, template in slurm_readonly.QUERIES.values():
        for arg in template:
            assert not any(word in arg for word in CHANGING_PROGRAMS)


def test_allowlist_cannot_be_extended_at_run_time():
    with pytest.raises(TypeError):
        slurm_readonly.QUERIES["cancel"] = ("scancel", ("{uid}",))


@pytest.mark.parametrize(
    "name", ["scancel", "sbatch", "squeue --user root", "", None, "my_queue "]
)
def test_unknown_queries_are_refused_without_running_anything(fake_slurm, name):
    calls, _ = fake_slurm

    with pytest.raises(SlurmQueryRefused):
        slurm_readonly.run(name)

    assert calls == []


@pytest.mark.parametrize("days", [0, 32, -1, "7", 7.0, True])
def test_invalid_look_back_is_refused(fake_slurm, days):
    with pytest.raises(SlurmQueryRefused):
        slurm_readonly.run("my_recent_jobs", days=days)


@pytest.mark.parametrize(
    "job_ids",
    [
        "123456",
        ["123456;rm -rf ~"],
        ["123456_7"],
        ["--user=root"],
        [123456],
        [],
        [str(n) for n in range(100000, 100021)],
    ],
)
def test_invalid_job_ids_are_refused(fake_slurm, job_ids):
    calls, _ = fake_slurm

    with pytest.raises(SlurmQueryRefused):
        slurm_readonly.run("my_job_steps", job_ids=job_ids)

    assert calls == []


def test_values_are_refused_for_queries_that_do_not_take_them(fake_slurm):
    with pytest.raises(SlurmQueryRefused):
        slurm_readonly.run("my_queue", days=7)
    with pytest.raises(SlurmQueryRefused):
        slurm_readonly.run("partition_load", job_ids=["123456"])


def test_runs_without_a_shell_with_a_time_limit(fake_slurm):
    calls, state = fake_slurm
    state["stdout"] = b"RUNNING\n"

    result = slurm_readonly.run("queue_states")

    assert result.ok
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/squeue", "--noheader", "--format", "%T"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == slurm_readonly.COMMAND_TIMEOUT_SECONDS
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_user_comes_from_the_os_and_is_hidden_in_the_shown_command(fake_slurm):
    calls, _ = fake_slurm

    result = slurm_readonly.run("my_queue")

    argv, _ = calls[0]
    assert argv[argv.index("--user") + 1] == "4242"
    assert "4242" not in result.shown_command
    assert result.shown_command.startswith("squeue --user $USER --format ")
    assert "--noheader" not in result.shown_command


def test_start_date_and_job_ids_are_filled_from_validated_values(fake_slurm):
    calls, _ = fake_slurm

    recent = slurm_readonly.run("my_recent_jobs", days=7)
    steps = slurm_readonly.run("my_job_steps", job_ids=["123456", "123457", "123456"])

    recent_argv, _ = calls[0]
    steps_argv, _ = calls[1]
    assert recent_argv[recent_argv.index("--starttime") + 1] == "2026-11-18"
    assert steps_argv[steps_argv.index("--jobs") + 1] == "123456,123457"
    assert "--starttime 2026-11-18" in recent.shown_command
    assert "--jobs 123456,123457" in steps.shown_command


def test_missing_program_is_reported_without_running(fake_slurm, monkeypatch):
    calls, _ = fake_slurm
    monkeypatch.setattr(slurm_readonly.shutil, "which", lambda cmd: None)

    result = slurm_readonly.run("partition_load")

    assert result.status == "missing"
    assert calls == []
    assert "`sinfo` is not installed" in slurm_readonly.unavailable_reason(result)


def test_timeout_is_reported(fake_slurm):
    _, state = fake_slurm
    state["raise"] = subprocess.TimeoutExpired("squeue", 10)

    result = slurm_readonly.run("queue_states")

    assert result.status == "timeout"
    assert result.output == ""
    assert "10 seconds" in slurm_readonly.unavailable_reason(result)


def test_failure_is_reported(fake_slurm):
    _, state = fake_slurm
    state["returncode"] = 1
    state["stdout"] = b"partial\n"

    result = slurm_readonly.run("queue_states")

    assert result.status == "failed"
    assert result.output == ""
    assert "unreachable" in slurm_readonly.unavailable_reason(result)


def test_output_is_capped_at_whole_lines(fake_slurm, monkeypatch):
    _, state = fake_slurm
    monkeypatch.setattr(slurm_readonly, "MAX_OUTPUT_BYTES", 20)
    state["stdout"] = b"PENDING\nRUNNING\nRUNNING\nRUNNING\n"

    result = slurm_readonly.run("queue_states")

    assert result.ok
    assert result.truncated
    assert result.output == "PENDING\nRUNNING"


def test_environment_variables_that_change_output_are_removed(fake_slurm, monkeypatch):
    calls, _ = fake_slurm
    monkeypatch.setenv("SQUEUE_FORMAT", "%j %u")
    monkeypatch.setenv("SACCT_FORMAT", "JobName")
    monkeypatch.setenv("SINFO_SORT", "-P")
    monkeypatch.setenv("SLURM_TIME_FORMAT", "relative")
    monkeypatch.setenv("SLURM_CONF", "/etc/slurm/slurm.conf")

    slurm_readonly.run("queue_states")

    _, kwargs = calls[0]
    env = kwargs["env"]
    assert "SQUEUE_FORMAT" not in env
    assert "SACCT_FORMAT" not in env
    assert "SINFO_SORT" not in env
    assert env["SLURM_TIME_FORMAT"] == "standard"
    assert env["SLURM_CONF"] == "/etc/slurm/slurm.conf"


def test_job_ids_are_found_in_question_text():
    found = slurm_readonly.JOB_ID_IN_TEXT.findall(
        "why is 123456 pending, and what about 123460_3 or v1.2?"
    )

    assert found == ["123456", "123460_3"]
