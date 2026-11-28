from __future__ import annotations

from datetime import datetime

import pytest

from inkly.plugins import _slurm_reasons, my_jobs
from tests.slurm_fakes import TIMEOUT, install_fake_slurm


# Made-up squeue rows: %i|%T|%P|%r|%M|%L|%l|%S|%D|%C|%m
QUEUE = "\n".join(
    [
        "200101|RUNNING|general|None|2:03:04|21:56:56|1-00:00:00|"
        "2026-11-25T12:56:56|1|4|16G",
        "200102|PENDING|general|Priority|0:00|1-00:00:00|1-00:00:00|"
        "2026-11-25T18:20:00|1|8|32G",
        "200103_[1-20]|PENDING|gpu|Resources|0:00|2:00:00|2:00:00|N/A|1|2|0",
        "200104|PENDING|general|QOSMaxJobsPerUserLimit|0:00|UNLIMITED|UNLIMITED|"
        "N/A|2|16|4000M",
        "200105|COMPLETING|general|None|5:00|0:00|10:00|2026-11-25T14:50:00|1|1|1G",
    ]
)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(my_jobs, "_now", lambda: datetime(2026, 11, 25, 15, 0))


def test_reports_running_and_pending_jobs_in_plain_words(monkeypatch):
    install_fake_slurm(monkeypatch, {"my_queue": QUEUE})

    output = my_jobs.run("what are my jobs doing?")

    assert output.startswith("Your Slurm Jobs (live)")
    assert "You have 5 jobs in the queue: 1 running, 3 pending, 1 completing." in output
    assert "Job names are not included." in output
    assert (
        "- 200101 on general: 4 CPUs, 1 node, 16G memory; running for 2 h 3 m "
        "(1 d limit, 21 h 56 m left)"
    ) in output
    assert (
        "- 200102 on general: 8 CPUs, 1 node, 32G memory, 1 d limit; reason "
        "Priority; expected start 2026-11-25 18:20 (in about 3 h 20 m)"
    ) in output
    assert (
        "- 200103_[1-20] on gpu: 2 CPUs, 1 node, 2 h limit; reason Resources; "
        "no start estimate yet"
    ) in output
    assert "200104 on general: 16 CPUs, 2 nodes, 4000M memory, no time limit" in output
    assert "- 200105 on general: completing" in output
    assert "- Priority: other jobs ahead of it have higher priority" in output
    assert "- QOSMaxJobsPerUserLimit: you already have the most running jobs" in output
    assert "Start times are the scheduler's current estimate" in output


def test_asks_only_for_the_users_own_jobs_and_shows_the_command(monkeypatch):
    calls = install_fake_slurm(monkeypatch, {"my_queue": QUEUE}, uid="5150")

    output = my_jobs.run("")

    ((_, argv),) = calls
    assert argv[argv.index("--user") + 1] == "5150"
    assert (
        "Slurm command: squeue --user $USER --format '%i|%T|%P|%r|%M|%L|%l|%S|%D|%C|%m'"
    ) in output
    assert "5150" not in output


def test_the_job_a_question_names_comes_first(monkeypatch):
    install_fake_slurm(monkeypatch, {"my_queue": QUEUE})

    output = my_jobs.run("why is job 200104 still waiting?")

    pending = output.split("Pending:\n", 1)[1]
    assert pending.startswith("- 200104 on general")


def test_array_task_and_unknown_ids_in_the_question(monkeypatch):
    install_fake_slurm(monkeypatch, {"my_queue": QUEUE})

    output = my_jobs.run("is 200103 or 999999 waiting? I asked 3 days ago")

    assert output.split("Pending:\n", 1)[1].startswith("- 200103_[1-20] on gpu")
    assert "Job 999999 is not among your jobs in the queue" in output
    assert "Job 3 " not in output


def test_no_jobs_in_the_queue(monkeypatch):
    install_fake_slurm(monkeypatch, {"my_queue": ""})

    output = my_jobs.run("")

    assert "You have no jobs in the queue right now." in output
    assert "Slurm command: squeue --user $USER" in output


def test_missing_squeue_and_timeout(monkeypatch):
    install_fake_slurm(monkeypatch, {}, missing={"squeue"})
    assert "Your jobs are unavailable: `squeue` is not installed here" in my_jobs.run()

    install_fake_slurm(monkeypatch, {"my_queue": TIMEOUT})
    output = my_jobs.run()
    assert "did not answer within 10 seconds" in output
    assert "Slurm command:" not in output


def test_unparseable_output(monkeypatch):
    install_fake_slurm(monkeypatch, {"my_queue": "JOBID STATE\n"})

    assert "could not parse squeue output" in my_jobs.run()


def test_long_lists_are_cut(monkeypatch):
    rows = "\n".join(
        f"3000{n:02d}|PENDING|general|Priority|0:00|1:00:00|1:00:00|N/A|1|1|1G"
        for n in range(11)
    )
    install_fake_slurm(monkeypatch, {"my_queue": rows})

    output = my_jobs.run()

    assert "You have 11 jobs in the queue: 0 running, 11 pending." in output
    assert "- 300007 on general" in output
    assert "- 300008 on general" not in output
    assert "- and 3 more jobs not listed" in output


@pytest.mark.parametrize(
    ("reason", "note"),
    [
        ("ReqNodeNotAvail, UnavailableNodes:cf[01-04]", "some nodes it can use"),
        ("QOSSomeNewLimit", "a limit of your quality of service"),
        ("AssocGrpGRES", "a limit on your account"),
        ("WaitingForSomethingNew", "the squeue manual page lists what it means"),
    ],
)
def test_reason_codes_always_get_a_note(reason, note):
    code = _slurm_reasons.reason_code(reason)
    assert note in _slurm_reasons.explain(code)
