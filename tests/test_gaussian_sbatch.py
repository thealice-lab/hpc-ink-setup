"""Tests for deterministic, standalone Cuttlefish Gaussian scripts."""

import copy
from pathlib import Path
import subprocess
import sys

import pytest

from inkly.gaussian.job_spec import GaussianJobSpec
from inkly.gaussian.sbatch import generate_sbatch

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@pytest.fixture
def profile():
    config_file = (
        Path(__file__).resolve().parents[1] / "inkly/cluster_profiles/cuttlefish.toml"
    )
    with config_file.open("rb") as handle:
        return tomllib.load(handle)


def sample_script(profile):
    return generate_sbatch(GaussianJobSpec("job.com", 4, 8192), profile)


def test_verified_cuttlefish_resources(profile):
    script = sample_script(profile)
    assert "#SBATCH --partition=general" in script
    assert "#SBATCH --nodes=1" in script
    assert "#SBATCH --ntasks=1" in script
    assert "#SBATCH --cpus-per-task=4" in script
    assert "#SBATCH --mem=8192M" in script
    assert "#SBATCH --hint=nomultithread" in script
    assert "GAUSSIAN_MODULE=gaussian/avx2/g16_rev_c02" in script
    assert 'SCRATCH_ROOT="/scratch/gaussian/${REAL_USER}"' in script
    assert "GAUSSIAN_MEM_MB=6144" in script
    assert "export OMP_NUM_THREADS=1" in script
    assert 'g16 -c="$CPU_IDS" < "$RUNTIME_INPUT"' in script


def test_cpu_ids_are_dynamic_and_from_allocated_core_set(profile):
    script = sample_script(profile)
    assert "os.sched_getaffinity(0)" in script
    assert "physical_package_id" in script
    assert "core_id" in script
    assert "Insufficient allocated physical cores" in script
    assert 'g16 -c="60,61"' not in script


def test_original_is_read_only_and_temp_copy_is_used(profile):
    script = sample_script(profile)
    assert 'JOB_SCRATCH="$(mktemp -d ' in script
    assert "source.read_text(" in script
    assert "target.write_text(" in script
    assert "source.write_text(" not in script
    assert "trap cleanup EXIT" in script


def test_script_syntax(profile, tmp_path):
    path = tmp_path / "job.sbatch"
    path.write_text(sample_script(profile))
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_small_slurm_memory_fails(profile):
    with pytest.raises(ValueError, match="too small"):
        generate_sbatch(GaussianJobSpec("job.com", 4, 1), profile)


@pytest.mark.parametrize(
    "key,value",
    [
        ("name", "Other Cluster"),
        ("scheduler", "pbs"),
    ],
)
def test_untrusted_profile_fails(profile, key, value):
    changed = copy.deepcopy(profile)
    changed[key] = value
    with pytest.raises(ValueError):
        sample_script(changed)


@pytest.mark.parametrize(
    "key,value",
    [
        ("verified_partition", "general\n#SBATCH --mem=99999M"),
        ("cpu_hint", "multithread"),
        ("cpu_launch", "g16 -c=60,61"),
        ("auto_memory_fraction", 1.0),
        ("memory_rounding", "up"),
    ],
)
def test_bad_slurm_policy_fails(profile, key, value):
    changed = copy.deepcopy(profile)
    changed["software"]["gaussian"]["slurm"][key] = value
    with pytest.raises(ValueError):
        sample_script(changed)


@pytest.mark.parametrize(
    "key,value",
    [
        ("scratch_template", "/tmp/../bad/{user}"),
        ("advertised_default_module", "gaussian; rm -rf /"),
        ("runtime_verified", False),
    ],
)
def test_bad_gaussian_configuration_fails(profile, key, value):
    changed = copy.deepcopy(profile)
    changed["software"]["gaussian"][key] = value
    with pytest.raises(ValueError):
        sample_script(changed)


def prepare_runtime(script, source, target):
    program = script.split("<<'PY_INPUT'\n", 1)[1].split("\nPY_INPUT", 1)[0]
    return subprocess.run(
        [sys.executable, "-c", program, str(source), str(target), "6144", "4"],
        capture_output=True,
        text=True,
    )


def test_link1_resources_are_rewritten_only_in_runtime_copy(profile, tmp_path):
    original = (
        "%mem=30000MB\n%nprocshared=20\n%chk=keep.chk\n#p hf/sto-3g\n\n"
        "Job 1\n\n0 1\nH 0 0 0\n\n"
        "--Link1--\n"
        "%Mem=20000MB\n%NProcShared=12\n#p hf/sto-3g\n\n"
        "Job 2\n\n0 1\nH 0 0 0\n\n"
    )
    source = tmp_path / "job.com"
    target = tmp_path / "runtime.com"
    source.write_text(original)
    result = prepare_runtime(sample_script(profile), source, target)
    assert result.returncode == 0, result.stderr
    data = target.read_text()
    assert data.count("%mem=6144MB") == 2
    assert data.count("%nprocshared=4") == 2
    assert "30000MB" not in data and "20000MB" not in data
    assert "%chk=keep.chk" in data
    assert "#p hf/sto-3g" in data
    assert source.read_text() == original


def test_unsupported_cpu_directive_fails_closed(profile, tmp_path):
    source = tmp_path / "job.com"
    target = tmp_path / "runtime.com"
    source.write_text("%cpu=60-63\n#p hf/sto-3g\n\nT\n\n0 1\nH 0 0 0\n")
    result = prepare_runtime(sample_script(profile), source, target)
    assert result.returncode != 0
    assert not target.exists()


def test_empty_section_fails_closed(profile, tmp_path):
    source = tmp_path / "job.com"
    target = tmp_path / "runtime.com"
    source.write_text("%mem=2000MB\n")
    result = prepare_runtime(sample_script(profile), source, target)
    assert result.returncode != 0
    assert not target.exists()
