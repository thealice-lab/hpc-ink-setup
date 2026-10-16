"""Deterministic Gaussian SBATCH generation for verified Cuttlefish settings."""

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import re
import shlex

from inkly.gaussian.job_spec import GaussianJobSpec


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_SAFE_SCRATCH = re.compile(r"/[A-Za-z0-9_/-]+/\{user\}")


_RUNTIME = r"""if [[ ! -f "$INPUT_FILE" ]]; then
    echo "Gaussian input file not found: $INPUT_FILE" >&2
    exit 1
fi

if ! id -nG | tr ' ' '\n' | grep -Fxq -- "$GAUSSIAN_GROUP"; then
    echo "Gaussian access group is required" >&2
    exit 1
fi

module load "$GAUSSIAN_MODULE"
export OMP_NUM_THREADS=1
if ! command -v g16 >/dev/null 2>&1; then
    echo "Gaussian executable g16 is not available" >&2
    exit 1
fi

REAL_USER="$(id -un)"
if [[ ! "$REAL_USER" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "Unsafe cluster username" >&2
    exit 1
fi

SCRATCH_ROOT="__SCRATCH_ROOT__"
mkdir -p -- "$SCRATCH_ROOT"
JOB_SCRATCH="$(mktemp -d "$SCRATCH_ROOT/inkly_XXXXXXXX")"
cleanup() {
    if [[ -n "${JOB_SCRATCH:-}" && -d "$JOB_SCRATCH" ]]; then
        rm -rf -- "$JOB_SCRATCH"
    fi
}
trap cleanup EXIT
export GAUSS_SCRDIR="$JOB_SCRATCH"
RUNTIME_INPUT="$JOB_SCRATCH/gaussian_input.com"

# Select one *allowed* logical CPU from each distinct physical core.
CPU_IDS="$(python3 - "$REQUESTED_CPUS" <<'PY_CPUS'
import os
from pathlib import Path
import sys

requested = int(sys.argv[1])
try:
    allocated = sorted(os.sched_getaffinity(0))
    if not allocated:
        raise RuntimeError("No CPUs in current affinity set")
    physical_cores = set()
    selected = []
    for cpu in allocated:
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        package = (topology / "physical_package_id").read_text().strip()
        core = (topology / "core_id").read_text().strip()
        core_key = (package, core)
        if core_key not in physical_cores:
            physical_cores.add(core_key)
            selected.append(cpu)
        if len(selected) == requested:
            break
    if len(selected) != requested:
        raise RuntimeError("Insufficient allocated physical cores")
    print(",".join(map(str, selected)))
except (OSError, ValueError, RuntimeError) as exc:
    sys.exit(f"Cannot select Gaussian CPUs: {exc}")
PY_CPUS
)"

# Read the original input and write a separate runtime copy.
# Update resource directives in each Link1 section, not just the first.
python3 - "$INPUT_FILE" "$RUNTIME_INPUT" "$GAUSSIAN_MEM_MB" "$REQUESTED_CPUS" <<'PY_INPUT'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
memory_mb = int(sys.argv[3])
cpus = int(sys.argv[4])

resource_directive = re.compile(r"^\s*%(?:mem|nproc|nprocshared)\s*=", re.I)
unsupported_directive = re.compile(
    r"^\s*%(?:cpu|lindaworkers|nproclinda)\s*=", re.I
)


def update_section(lines):
    header = True
    output = [f"%mem={memory_mb}MB\n", f"%nprocshared={cpus}\n"]
    has_route = False
    for line in lines:
        if header and line.lstrip().startswith("#"):
            header = False
            has_route = True
        if header and unsupported_directive.match(line):
            raise ValueError("Unsupported Gaussian CPU/Linda directive")
        if header and resource_directive.match(line):
            continue
        output.append(line)
    if not has_route:
        raise ValueError("Gaussian input section has no route line")
    return output


try:
    input_lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    updated = []
    section = []
    for line in input_lines:
        if line.strip().lower() == "--link1--":
            updated.extend(update_section(section))
            updated.append("--Link1--\n")
            section = []
        else:
            section.append(line)
    updated.extend(update_section(section))
    target.write_text("".join(updated), encoding="utf-8")
except (OSError, UnicodeError, ValueError) as exc:
    sys.exit(f"Cannot prepare Gaussian runtime input: {exc}")
PY_INPUT

echo "Starting Gaussian with $REQUESTED_CPUS physical cores"
g16 -c="$CPU_IDS" < "$RUNTIME_INPUT"
"""


def _profile_dict(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid {name} configuration")
    return value


def _safe_name(value, name):
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"Unsafe or missing {name}")
    return value


def generate_sbatch(spec: GaussianJobSpec, profile: dict) -> str:
    """Generate a standalone, single-node Cuttlefish Gaussian SBATCH script."""
    if not isinstance(spec, GaussianJobSpec):
        raise TypeError("spec must be a GaussianJobSpec")
    if not isinstance(profile, dict):
        raise ValueError("A verified Cuttlefish profile is required")
    if (
        profile.get("schema_version") != 1
        or profile.get("name") != "Cuttlefish"
        or profile.get("scheduler") != "slurm"
    ):
        raise ValueError("A verified Cuttlefish Slurm profile is required")

    software = _profile_dict(profile.get("software"), "software")
    gaussian = _profile_dict(software.get("gaussian"), "Gaussian")
    slurm = _profile_dict(gaussian.get("slurm"), "Gaussian Slurm")
    if (
        gaussian.get("installed") is not True
        or gaussian.get("runtime_verified") is not True
    ):
        raise ValueError("Gaussian runtime must be verified before generating jobs")

    partition = _safe_name(slurm.get("verified_partition"), "partition")
    module = _safe_name(gaussian.get("advertised_default_module"), "module")
    group = _safe_name(gaussian.get("access_group"), "access group")
    scratch = gaussian.get("scratch_template")
    if not isinstance(scratch, str) or not _SAFE_SCRATCH.fullmatch(scratch):
        raise ValueError("Unsafe or missing Gaussian scratch template")
    if slurm.get("cpu_hint") != "nomultithread":
        raise ValueError("Unsupported CPU hint")
    if (
        slurm.get("cpu_selection")
        != "one logical CPU per unique physical core from the allocated cpuset"
    ):
        raise ValueError("Unsupported CPU selection policy")
    if slurm.get("cpu_launch") != "g16 -c=<selected_cpu_ids>":
        raise ValueError("Unsupported Gaussian CPU launch policy")
    if slurm.get("memory_rounding") != "down":
        raise ValueError("Unsupported memory rounding")

    try:
        fraction = Decimal(str(slurm["auto_memory_fraction"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("Invalid Gaussian memory fraction") from exc
    if not fraction.is_finite() or not Decimal("0") < fraction <= Decimal("0.75"):
        raise ValueError("Gaussian memory fraction must be in (0, 0.75]")

    # Gaussian MB is conservatively treated as decimal MB, below Slurm MiB.
    memory_mb = int(
        (Decimal(spec.slurm_memory_mib) * fraction).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if memory_mb < 1:
        raise ValueError("Slurm memory too small for Gaussian")

    job_name = spec.input_file.rsplit(".", 1)[0]
    lines = [
        "#!/bin/bash -l",
        f"#SBATCH --job-name=gaussian_{job_name}",
        f"#SBATCH --partition={partition}",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={spec.cpus}",
        f"#SBATCH --mem={spec.slurm_memory_mib}M",
        "#SBATCH --hint=nomultithread",
        "",
        "set -euo pipefail",
        "umask 077",
        f"INPUT_FILE={shlex.quote(spec.input_file)}",
        f"REQUESTED_CPUS={spec.cpus}",
        f"GAUSSIAN_MEM_MB={memory_mb}",
        f"GAUSSIAN_MODULE={shlex.quote(module)}",
        f"GAUSSIAN_GROUP={shlex.quote(group)}",
        "",
    ]
    return "\n".join(lines) + _RUNTIME.replace(
        "__SCRATCH_ROOT__", scratch.replace("{user}", "${REAL_USER}")
    )
