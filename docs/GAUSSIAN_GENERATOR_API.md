# Gaussian V1 — Deterministic SBATCH Generator

## Purpose

Generate a Cuttlefish Gaussian 16 Slurm script from a validated
job specification and a trusted cluster profile.

The language model should interpret the user's request, but it
must not invent the Slurm commands. The deterministic generator
is responsible for producing the script.

## Python Interface

- `inkly.gaussian.job_spec.GaussianJobSpec`
- `inkly.gaussian.sbatch.generate_sbatch(spec, profile)`

### GaussianJobSpec

Required fields:

- `input_file: str` — A .com or .gjf filename, without directory paths.
- `cpus: int` — Positive integer CPU count.
- `slurm_memory_mib: int` — Positive integer total Slurm memory in MiB.

Invalid values raise `ValueError`.

### Example

~~~python
from pathlib import Path

from inkly.cluster_profile import resolve_cluster_profile
from inkly.gaussian.job_spec import GaussianJobSpec
from inkly.gaussian.sbatch import generate_sbatch

profile = resolve_cluster_profile()

if profile is None:
    raise RuntimeError("No trusted cluster profile available")

spec = GaussianJobSpec(
    input_file="job.com",
    cpus=4,
    slurm_memory_mib=8192,
)

script = generate_sbatch(spec, profile)

Path("job.sbatch").write_text(script, encoding="utf-8")
~~~

This example generates a file only. It does not submit a job.

## Cuttlefish Rules

The generator uses the trusted Cuttlefish profile for:

- Verified Slurm partition and Gaussian module.
- One Slurm task with the requested CPU allocation.
- Dynamic CPU selection from allocated physical cores.
- Per-user Gaussian scratch.
- Gaussian internal memory capped by the configured fraction,
  currently 75% of requested Slurm memory.
- Single-node Gaussian 16 execution.

## Input Preservation

The original Gaussian input is never rewritten.

At runtime, a temporary copy is created. The generator applies
the requested memory and processor directives to that copy,
including individual Link1 sections.

Unsupported Gaussian CPU/Linda directives cause the job to fail
rather than silently override unsafe settings.

## Integration Requirements

Member 2 should:

1. Interpret the user request.
2. Resolve missing or ambiguous resources.
3. Create a GaussianJobSpec.
4. Load the trusted Cuttlefish profile.
5. Call generate_sbatch(spec, profile).
6. Present the generated script for review or submission.

Do not construct Slurm commands directly from LLM responses.

Do not accept an LLM-generated dictionary as a trusted profile.

The input file must exist in the Slurm job's working directory
when the generated script runs.

## Current Validation

- 164 project tests passing.
- Ruff lint and formatting passed.
- Generated script passed Bash syntax validation.
- Minimal Gaussian 16 H2 calculation completed successfully
  through Slurm on Cuttlefish.
- Original input checksum remained unchanged.
- No leftover Inkly-specific scratch directories were found.

Independent installation testing and broader integration review
remain outstanding.
