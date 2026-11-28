from __future__ import annotations

from inkly.plugins import queue_status
from tests.slurm_fakes import TIMEOUT, install_fake_slurm


QUEUE = "RUNNING\nRUNNING\nRUNNING\nPENDING\nPENDING\nCOMPLETING\n"
PARTITIONS = "general*|up|4/5/1/10|120/136/0/256\ngpu|up|2/2/0/4|16/48/0/64\n"


def test_run_reports_queue_counts_and_partition_load(monkeypatch):
    install_fake_slurm(
        monkeypatch, {"queue_states": QUEUE, "partition_load": PARTITIONS}
    )

    output = queue_status.run()

    assert output.startswith("Queue Status")
    assert "Running jobs: 3" in output
    assert "Pending jobs: 2" in output
    assert "Jobs in other states (completing, suspended and so on): 1" in output
    assert "cover the jobs your account can see" in output
    assert (
        "- general (default, up): nodes 4 in use, 5 idle, 1 unavailable, 10 total; "
        "CPUs 120 in use, 136 idle, 0 unavailable, 256 total"
    ) in output
    assert "- gpu (up): nodes 2 in use, 2 idle, 0 unavailable, 4 total" in output
    assert "Slurm command: squeue --format %T" in output
    assert "Slurm command: sinfo --format '%P|%a|%F|%C'" in output


def test_run_asks_slurm_only_for_job_states(monkeypatch):
    calls = install_fake_slurm(
        monkeypatch, {"queue_states": QUEUE, "partition_load": PARTITIONS}
    )

    queue_status.run()

    squeue_argv = next(argv for name, argv in calls if name == "queue_states")
    assert squeue_argv[-2:] == ["--format", "%T"]
    assert "--user" not in squeue_argv


def test_run_handles_missing_squeue_and_sinfo(monkeypatch):
    install_fake_slurm(monkeypatch, {}, missing={"squeue", "sinfo"})

    output = queue_status.run()

    assert "Queue counts unavailable: `squeue` is not installed here" in output
    assert "Partitions:" in output
    assert "Partition summary unavailable: `sinfo` is not installed here" in output
    assert "Slurm command:" not in output


def test_run_handles_a_slow_scheduler(monkeypatch):
    install_fake_slurm(
        monkeypatch, {"queue_states": TIMEOUT, "partition_load": PARTITIONS}
    )

    output = queue_status.run()

    assert "did not answer within 10 seconds" in output
    assert "- general (default, up)" in output
    assert "Slurm command: squeue" not in output


def test_run_handles_sinfo_parse_failure(monkeypatch):
    install_fake_slurm(
        monkeypatch,
        {
            "queue_states": "RUNNING\n",
            "partition_load": "badly-formatted-line-with-no-separators\n",
        },
    )

    output = queue_status.run()

    assert "Running jobs: 1" in output
    assert "Pending jobs: 0" in output
    assert "Partition summary unavailable: could not parse sinfo output." in output


def test_run_handles_empty_output(monkeypatch):
    install_fake_slurm(monkeypatch, {"queue_states": "", "partition_load": ""})

    output = queue_status.run()

    assert "Running jobs: 0" in output
    assert "Partition summary unavailable: sinfo returned no data." in output


def test_run_limits_the_number_of_partitions(monkeypatch):
    rows = "".join(f"p{n}|up|0/1/0/1|0/8/0/8\n" for n in range(12))
    install_fake_slurm(monkeypatch, {"queue_states": "", "partition_load": rows})

    output = queue_status.run()

    assert "- p7 (up)" in output
    assert "- p8 (up)" not in output
    assert "- and 4 more partitions" in output
