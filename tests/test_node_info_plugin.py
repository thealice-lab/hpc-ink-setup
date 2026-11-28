from __future__ import annotations

from inkly.plugins import node_info
from tests.slurm_fakes import FAILED, install_fake_slurm


RESOURCES = (
    "general*|up|10|32|128000|(null)|7-00:00:00|1-00:00:00|1-4\n"
    "gpu|up|4|64|256000|gpu:4|2-00:00:00|n/a|1-infinite\n"
    "scavenge|down|1|16|64000|(null)|infinite|n/a|1-1\n"
)


def test_run_reports_partition_resources_and_limits(monkeypatch):
    install_fake_slurm(monkeypatch, {"partition_resources": RESOURCES})

    output = node_info.run()

    assert output.startswith("Node / Partition Information")
    assert "Partitions and resources:" in output
    assert (
        "- general (default, up): 10 nodes, 32 CPUs/node, 128000 MB/node, "
        "no GPUs; time limit 7 d (default 1 d); jobs may use 1-4 nodes"
    ) in output
    assert (
        "- gpu (up): 4 nodes, 64 CPUs/node, 256000 MB/node, GPUs/GRES gpu:4; "
        "time limit 2 d"
    ) in output
    assert "gpu:4; time limit 2 d\n" in output
    assert "- scavenge (down): 1 node, 16 CPUs/node" in output
    assert "no time limit; jobs may use 1 node" in output
    assert "per-partition memory defaults and QOS limits" in output
    assert "Slurm command: sinfo --format '%P|%a|%D|%c|%m|%G|%l|%L|%s'" in output


def test_run_handles_missing_sinfo(monkeypatch):
    install_fake_slurm(monkeypatch, {}, missing={"sinfo"})

    output = node_info.run()

    assert "Partitions and resources:" in output
    assert "Node information unavailable: `sinfo` is not installed here" in output
    assert "Slurm command:" not in output


def test_run_handles_sinfo_failure(monkeypatch):
    install_fake_slurm(monkeypatch, {"partition_resources": FAILED})

    output = node_info.run()

    assert "Node information unavailable: `sinfo` failed" in output


def test_run_handles_unparseable_sinfo_output(monkeypatch):
    install_fake_slurm(
        monkeypatch, {"partition_resources": "general|10|32|128000|(null)\n"}
    )

    output = node_info.run()

    assert "Node information unavailable: could not parse sinfo output." in output
