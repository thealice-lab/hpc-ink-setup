# HPC / Inkly Worklog

This is the public high-level development history for Inkly.

Detailed cluster validation evidence, raw scheduler output, node identifiers, job identifiers, account information, and infrastructure investigation records are maintained separately from the public repository.

## Phase 1 — Gaussian Documentation Integration

Completed:

- integrated the Gaussian documentation scraper with Inkly
- connected scraper-produced SQLite documentation to Inkly retrieval
- added source and provenance labeling
- added retrieval limits and failure handling
- added regression coverage
- benchmarked local retrieval performance
- prevented external-cluster documentation from being presented as verified Cuttlefish commands

## Phase 1 — Installation Validation

Completed:

- validated clean Inkly installation
- identified and fixed stale installed-runtime behavior
- verified the installed ink launcher uses the expected runtime
- validated missing and corrupt Gaussian documentation database handling
- verified cluster-specific questions fail safely when trusted local evidence is unavailable

## Phase 1B — Cuttlefish Gaussian Operational Validation

Validated:

- Gaussian availability and access behavior
- Gaussian module loading
- successful Gaussian execution
- Gaussian scratch behavior
- Slurm CPU allocation behavior
- physical-core-aware Gaussian CPU selection
- dynamic Gaussian CPU binding
- Slurm memory enforcement
- separation of Slurm memory and Gaussian internal memory
- conservative automatic Gaussian memory sizing
- trusted Cuttlefish cluster-profile integration

## Current Phase 1B Work

Next:

- validate Inkly's complete user-facing Gaussian SBATCH generation
- verify generated jobs follow the trusted Cuttlefish rules
- add regression coverage for final job-generation behavior
- complete reproducible Cuttlefish setup documentation
- validate setup with a fresh Cuttlefish user account
- merge validated Phase 1B work through the normal ALICE development flow

## Publication Policy

The public repository should contain only the information needed to understand, install, develop, test, and safely use Inkly.

Raw cluster validation artifacts and unnecessary infrastructure details should remain outside the public repository.
