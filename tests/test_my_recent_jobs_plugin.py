from __future__ import annotations

import pytest

from inkly.plugins import my_recent_jobs
from tests.slurm_fakes import FAILED, install_fake_slurm


# Made-up sacct allocation rows:
# JobIDRaw|JobID|State|ExitCode|Partition|Elapsed|Timelimit|AllocCPUS|NNodes|
# TotalCPU|AllocTRES
ALLOCATIONS = "\n".join(
    [
        "200090|200090|COMPLETED|0:0|general|02:00:00|1-00:00:00|4|1|07:20:00|"
        "billing=4,cpu=4,mem=16G,node=1",
        "200091|200091|OUT_OF_MEMORY|0:125|general|00:40:00|1-00:00:00|8|1|04:00:00|"
        "billing=8,cpu=8,mem=8G,node=1",
        "200092|200092|FAILED|1:0|general|00:10:00|1-00:00:00|8|1|00:30:00|"
        "billing=8,cpu=8,mem=32G,node=1",
        "200093|200093|CANCELLED by 4242|0:15|gpu|00:00:00|02:00:00|2|1|00:00:00|",
        "200094|200089_3|TIMEOUT|0:0|general|1-00:00:00|1-00:00:00|4|1|1-20:00:00|"
        "billing=4,cpu=4,mem=4000M,node=1",
        "200095|200095|RUNNING|0:0|general|00:30:00|1-00:00:00|4|1|00:00:00|"
        "billing=4,cpu=4,mem=16G,node=1",
        "200096|200096|PENDING|0:0|general|00:00:00|1-00:00:00|4|1|00:00:00|",
    ]
)
# JobIDRaw|TotalCPU|MaxRSS for the steps of the finished jobs.
STEPS = "\n".join(
    [
        "200090|00:00:00|",
        "200090.batch|07:10:00|3355443K",
        "200090.extern|00:00.010|1024K",
        "200091.batch|04:00:00|8000M",
        "200092.batch|00:30:00|2G",
        "200094.batch|1-20:00:00|3500M",
    ]
)


def test_reports_outcomes_and_efficiency(monkeypatch):
    install_fake_slurm(
        monkeypatch, {"my_recent_jobs": ALLOCATIONS, "my_job_steps": STEPS}
    )

    output = my_recent_jobs.run("how did my jobs go?")

    assert output.startswith("Your Recent Slurm Jobs (live)")
    assert (
        "In the last 7 days you had 7 jobs: 1 completed, 1 out of memory, 1 failed, "
        "1 cancelled, 1 timed out, 2 still running or waiting."
    ) in output
    assert "Job names are not included." in output
    assert (
        "- 200090: completed; general, 4 CPUs, 16.0 GiB; ran 2 h of 1 d; "
        "CPU use 90%; memory peak 3.2 GiB, 20% of the request (much less than "
        "requested)"
    ) in output
    assert "- 200091: out of memory, signal 125;" in output
    assert "it ran out of memory; it needs a larger memory request" in output
    assert "- 200092: failed, exit code 1; general, 8 CPUs, 32.0 GiB" in output
    assert "CPU use 38% (requested more CPUs than it used)" in output
    assert "- 200093: cancelled, signal 15; gpu, 2 CPUs; ran 0 s of 2 h" in output
    assert "4242" not in output
    assert "- 200089_3: timed out; general, 4 CPUs, 3.9 GiB; ran 1 d of 1 d" in output
    assert "it hit its time limit" in output
    assert "200095" not in output
    assert "Across the jobs listed: average CPU use" in output


def test_steps_are_looked_up_only_for_the_listed_finished_jobs(monkeypatch):
    calls = install_fake_slurm(
        monkeypatch, {"my_recent_jobs": ALLOCATIONS, "my_job_steps": STEPS}
    )

    output = my_recent_jobs.run("")

    allocations_argv = calls[0][1]
    steps_argv = calls[1][1]
    assert allocations_argv[allocations_argv.index("--starttime") + 1] == "2026-11-18"
    assert allocations_argv[allocations_argv.index("--user") + 1] == "4242"
    jobs = steps_argv[steps_argv.index("--jobs") + 1].split(",")
    assert sorted(jobs) == ["200090", "200091", "200092", "200093", "200094"]
    assert steps_argv[steps_argv.index("--user") + 1] == "4242"
    assert "Slurm command: sacct --user $USER --starttime 2026-11-18" in output
    assert "Slurm command: sacct --user $USER --jobs " in output


def test_look_back_follows_the_question(monkeypatch):
    calls = install_fake_slurm(monkeypatch, {"my_recent_jobs": ""})

    output = my_recent_jobs.run("show my jobs from the last 14 days")

    argv = calls[0][1]
    assert argv[argv.index("--starttime") + 1] == "2026-11-11"
    assert "You had no jobs in the last 14 days." in output
    assert len(calls) == 1


def test_named_job_comes_first_and_unknown_ids_are_reported(monkeypatch):
    install_fake_slurm(
        monkeypatch, {"my_recent_jobs": ALLOCATIONS, "my_job_steps": STEPS}
    )

    output = my_recent_jobs.run("why did 200090 work but not 777777?")

    listed = output.split("Most recent finished jobs:\n", 1)[1]
    assert listed.startswith("- 200090:")
    assert "Job 777777 is not among your jobs from the last 7 days." in output


def test_missing_steps_still_reports_outcomes(monkeypatch):
    install_fake_slurm(
        monkeypatch, {"my_recent_jobs": ALLOCATIONS, "my_job_steps": FAILED}
    )

    output = my_recent_jobs.run("")

    assert "- 200090: completed;" in output
    assert "CPU use 92%" in output
    assert "memory peak" not in output.split("Across", 1)[0].split("200090:", 1)[1]


@pytest.mark.parametrize(
    ("missing", "outputs", "message"),
    [
        ({"sacct"}, {}, "`sacct` is not installed here"),
        ((), {"my_recent_jobs": FAILED}, "`sacct` failed"),
        ((), {"my_recent_jobs": "garbage\n"}, "could not parse sacct output"),
    ],
)
def test_unavailable_accounting(monkeypatch, missing, outputs, message):
    install_fake_slurm(monkeypatch, outputs, missing=missing)

    output = my_recent_jobs.run("")

    assert f"Your recent jobs are unavailable: {message}" in output


# Accounting that does not gather usage: zero CPU time, blank or zero MaxRSS.
NO_USAGE_ALLOCATIONS = "\n".join(
    [
        "300001|300001|COMPLETED|0:0|general|02:00:00|1-00:00:00|4|1|00:00:00|"
        "billing=4,cpu=4,mem=16G,node=1",
        "300002|300002|FAILED|1:0|general|00:30:00|1-00:00:00|8|1|00:00:00|"
        "billing=8,cpu=8,mem=8G,node=1",
    ]
)


@pytest.mark.parametrize(
    "steps",
    [
        "300001.batch|00:00:00|\n300002.batch|00:00:00|",
        "300001.batch|00:00:00|0\n300002.batch|00:00:00|0",
        "",
    ],
)
def test_disabled_accounting_makes_efficiency_unavailable_not_zero(monkeypatch, steps):
    install_fake_slurm(
        monkeypatch, {"my_recent_jobs": NO_USAGE_ALLOCATIONS, "my_job_steps": steps}
    )

    output = my_recent_jobs.run("how efficient were my jobs?")

    assert "- 300001: completed; general, 4 CPUs, 16.0 GiB; ran 2 h of 1 d" in output
    assert "CPU use" not in output
    assert "memory peak" not in output.lower()
    assert "0%" not in output
    assert (
        "Efficiency is unavailable: Slurm recorded no CPU time or memory use" in output
    )


@pytest.mark.parametrize("max_rss", ["", "0", "0K"])
def test_blank_max_rss_makes_memory_unavailable_not_zero(monkeypatch, max_rss):
    allocations = NO_USAGE_ALLOCATIONS.replace(
        "|02:00:00|1-00:00:00|4|1|00:00:00|", "|02:00:00|1-00:00:00|4|1|06:00:00|"
    )
    install_fake_slurm(
        monkeypatch,
        {
            "my_recent_jobs": allocations,
            "my_job_steps": f"300001.batch|06:00:00|{max_rss}",
        },
    )

    output = my_recent_jobs.run("")

    assert (
        "- 300001: completed; general, 4 CPUs, 16.0 GiB; ran 2 h of 1 d; CPU use 75%"
        in output
    )
    assert "memory peak 0" not in output
    assert "0% of the request" not in output
    assert "Memory peak is unavailable: Slurm recorded no memory use" in output
    assert "largest step's sampled maximum" not in output
