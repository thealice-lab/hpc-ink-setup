from dataclasses import dataclass
import re


_INPUT_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:com|gjf)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GaussianJobSpec:
    """Validated inputs for a Gaussian Slurm job."""

    input_file: str
    cpus: int
    slurm_memory_mib: int

    def __post_init__(self):
        if not isinstance(self.input_file, str) or not _INPUT_NAME.fullmatch(
            self.input_file
        ):
            raise ValueError("input_file must be a safe .com or .gjf filename")

        if type(self.cpus) is not int or self.cpus < 1:
            raise ValueError("cpus must be a positive integer")

        if type(self.slurm_memory_mib) is not int or self.slurm_memory_mib < 1:
            raise ValueError("slurm_memory_mib must be a positive integer")
