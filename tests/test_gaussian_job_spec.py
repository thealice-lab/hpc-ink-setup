import pytest

from inkly.gaussian.job_spec import GaussianJobSpec


def test_valid_com_input():
    spec = GaussianJobSpec("job.com", 4, 8192)

    assert spec.input_file == "job.com"
    assert spec.cpus == 4
    assert spec.slurm_memory_mib == 8192


def test_valid_gjf_input():
    spec = GaussianJobSpec("calculation_01.gjf", 2, 4096)

    assert spec.input_file == "calculation_01.gjf"


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "job.txt",
        "../job.com",
        "/tmp/job.com",
        "my job.com",
        "job;echo.com",
        "job\n.com",
    ],
)
def test_invalid_filename(filename):
    with pytest.raises(ValueError):
        GaussianJobSpec(filename, 4, 8192)


@pytest.mark.parametrize("cpus", [0, -1, 1.5, True, "4"])
def test_invalid_cpu_count(cpus):
    with pytest.raises(ValueError):
        GaussianJobSpec("job.com", cpus, 8192)


@pytest.mark.parametrize("memory", [0, -1, 1.5, True, "8192"])
def test_invalid_memory(memory):
    with pytest.raises(ValueError):
        GaussianJobSpec("job.com", 4, memory)
