# Cuttlefish Gaussian Ground Truth

This public file summarizes the Cuttlefish-specific behavior that Inkly is allowed to treat as trusted local guidance.

Detailed validation evidence, raw scheduler output, node identifiers, job identifiers, account information, and infrastructure investigation records are maintained separately from the public repository.

## Evidence Levels

- VERIFIED_RUNTIME — behavior directly tested successfully on Cuttlefish
- VERIFIED_CONFIG — configuration directly verified on Cuttlefish
- VERIFIED_ACCESS — required user access has been verified
- BLOCKED_BY_ACCESS — software is present but unavailable to the current user
- UNKNOWN — behavior that has not yet been verified locally
- GAUSSIAN_GENERAL — portable Gaussian behavior
- EXTERNAL_CLUSTER — behavior belonging to another HPC system

## Verified Operational Behavior

- Cuttlefish uses Slurm.
- Gaussian 16 execution has been successfully validated.
- Inkly must verify that the current user has access before presenting Gaussian execution guidance as runnable.
- Gaussian jobs use a per-user scratch location rather than another user's path.
- CPU placement must be derived from the CPUs actually allocated to the running Slurm job.
- Gaussian CPU IDs must be selected dynamically and must never be copied from a previous validation run.
- Slurm memory and Gaussian internal memory are separate controls.
- Gaussian internal memory must leave headroom below the complete Slurm allocation.
- Inkly currently uses a conservative automatic Gaussian-memory policy based on validated Cuttlefish behavior.

## Source Safety

External Gaussian documentation may be used for general application knowledge, but commands and policies from another institution must not be presented as verified Cuttlefish configuration.

Inkly must keep these evidence types separate:

- verified Cuttlefish configuration
- verified Cuttlefish runtime behavior
- general Gaussian knowledge
- external-cluster documentation
- unknown local behavior

## Detailed Operational Configuration

The machine-readable Cuttlefish profile contains the minimum cluster-specific settings Inkly needs for operation.

Raw validation evidence remains outside the public project documentation.
