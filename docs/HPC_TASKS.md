# HPC / Inkly Tasks

## Phase 1 — Gaussian Documentation Integration

- [x] Integrate the Gaussian documentation scraper with Inkly.
- [x] Use scraper-produced SQLite documentation locally.
- [x] Add provenance and source scoping.
- [x] Prevent external-cluster commands from being treated as Cuttlefish facts.
- [x] Add missing and corrupt database handling.
- [x] Add retrieval regression tests.
- [x] Benchmark local documentation retrieval.
- [x] Validate the installed Inkly runtime.

## Phase 1B — Cuttlefish Gaussian Operational Validation

- [x] Add trusted cluster-profile support.
- [x] Verify Gaussian access behavior.
- [x] Verify Gaussian module loading.
- [x] Validate real Gaussian execution.
- [x] Verify Gaussian scratch behavior.
- [x] Validate CPU placement behavior.
- [x] Validate physical-core-aware Gaussian CPU selection.
- [x] Validate dynamic Gaussian CPU binding.
- [x] Verify Slurm memory enforcement.
- [x] Verify Slurm memory and Gaussian internal memory are separate controls.
- [x] Validate conservative Gaussian memory sizing.
- [x] Add trusted Cuttlefish CPU and memory rules to Inkly.
- [x] Pass automated regression and formatting checks.

## User-Facing Gaussian Generation

- [ ] Validate Inkly's complete Gaussian SBATCH response.
- [ ] Confirm generated jobs use the verified Cuttlefish partition.
- [ ] Confirm generated jobs use the physical-core CPU rule.
- [ ] Confirm Gaussian CPU IDs are determined dynamically.
- [ ] Confirm generated memory follows the conservative Cuttlefish policy.
- [ ] Confirm external-cluster commands never appear as local Cuttlefish commands.
- [ ] Add regression coverage for final job generation.

## Installation / Onboarding

- [ ] Create docs/CUTTLEFISH_SETUP.md.
- [ ] Make the README direct Cuttlefish users to the setup guide.
- [ ] Validate the setup guide with a fresh Cuttlefish user account.
- [ ] Verify installed ink works outside the repository.
- [ ] Document common setup failures and troubleshooting.
- [ ] Validate local and non-HPC Inkly setup separately.

## Repository / Security

- [x] Protect raw Slurm and Gaussian runtime artifacts with .gitignore.
- [x] Remove account-specific usernames and host examples from tracked files.
- [x] Separate detailed cluster validation records from public documentation.
- [ ] Run a final public-repository information audit.
- [ ] Review existing Git history if previously published operational details require removal.

## Integration

- [ ] Complete Phase 1B validation.
- [ ] Merge validated work through the ALICE development branch.
- [ ] Validate a fresh install from the merged branch.
- [ ] Update main after the development branch is fully validated.

## Future Work

- [ ] Build reusable cluster onboarding and validation.
- [ ] Add an ink init or similar workflow.
- [ ] Generate trusted profiles for additional HPC systems.
- [ ] Keep verified local facts separate from general and external documentation.
