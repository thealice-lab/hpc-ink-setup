# Gaussian documentation: verified shared snapshot and private copy

This describes the **documentation** database (`gaussian.db`), not Inkly's
per-user Slurm job-history database (`jobs.db`). No other researcher's database
should be read or overwritten.

## What is implemented

`inkly/gaussian_docs.py` checks a pre-provisioned shared snapshot against the
actual scraper configuration and Git revision. It verifies SQLite integrity,
per-source identities, passage ordering and content hashes, the full source
inventory (including reviewed missing-source exceptions), and the manifest's
file hash. It then installs a validated **private copy** without overwriting an
existing different `~/.inkly/gaussian.db`.

A maintainer may initiate an approved build through the same module. Builders
serialize using a stable lock; waiting users only read published snapshots.
The builder creates a private staging area and publishes a finished snapshot
by atomic rename, updating `current.json` only after validation. The normal
installer **never builds or submits a Slurm job**.

## Required administrator decisions

- Identify and explicitly approve a shared Cuttlefish scratch directory for the
  corpus. It must be provisioned ahead of time with appropriate ownership,
  group access, permissions, and a confirmed policy for NFS lock support.
  **Do not reuse** `/scratch/inklys-logs/shared`, which holds observability
  records. Do not assume any particular corpus directory exists.
- Confirm which accounts may publish and which accounts may only read. A root
  that is group-writable must have controlled publisher membership.
- Decide how a failed scrape is reviewed. Nathan's November 1 source audit
  reported 106 configured sources and 100 stored, but a future build must use
  the *current* configuration rather than assuming exactly 100 stored. Missing
  sources require explicit reasons bound to the config SHA-256.
- Approve the Slurm allocation and network use needed for a real rebuild.
  **Do not build on the login node or submit a Slurm job without authorization.**
- Decide whether to retain expired snapshots and build artifacts; Cuttlefish
  scratch may be purged and is not durable archival storage.

## Researcher installation from an existing approved snapshot

Set the **real approved path** supplied by the cluster administrator (replace
`/APPROVED/CORPUS/ROOT`; it is not a path we are claiming exists):

```bash
export INKLY_GAUSSIAN_SHARED_ROOT=/APPROVED/CORPUS/ROOT
bash setup.sh
```

`setup.sh` installs the scraper and Inkly, and then invokes:

```bash
~/.inkly/venv/bin/python -m inkly.gaussian_docs ensure \
  --shared-root "$INKLY_GAUSSIAN_SHARED_ROOT" \
  --scraper-dir "$HOME/.inkly/src/gaussian-docs-scraper" \
  --destination "$HOME/.inkly/gaussian.db"
```

For a private checkout used outside the repository root, execute the module
from the checkout root or put the installed Inkly runtime on Python's module
search path. `setup.sh` handles this automatically.

If the administrator approved specific missing-source exceptions for the
**current** config hash, provide an additional file with this structure:

```json
{
  "config_sha256": "<SHA256 of the reviewed configs/gaussian.toml>",
  "allowed_missing": {
    "Exact configured label": "Reviewed failure and issue reference"
  }
}
```

Then set `INKLY_GAUSSIAN_EXCEPTIONS` to that file before running `setup.sh`.
An omitted or mismatched exception policy causes a partial snapshot to fail
validation. Do not fill in exceptions automatically to make a build pass.

When `INKLY_GAUSSIAN_SHARED_ROOT` is absent, setup prints a clear notice: the
scraper is installed, **not** a searchable corpus. This is not researcher-ready
and the installation acceptance test must fail if `gaussian.db` is missing.

If `~/.inkly/gaussian.db` already exists with different contents, it is
preserved, and setup fails with a migration/backup notice. Never silently
replace an existing researcher's corpus.

## Controlled maintainer build

Only after approval, inside an appropriate Slurm allocation, a maintainer may
run the module with `--allow-build` using the approved shared root, scraper
checkout and reviewed exception policy. The scraper executes Nathan's existing
`run_scrape(..., skip_summary=True)` pipeline, not a second scraper. The
published snapshot is read-only; each researcher's `gaussian.db` remains
private. An interrupted builder's lock releases when its process terminates;
the next approved builder can recover. Publication verifies that the lock
still refers to the same inode.

**No build is implicitly started by researcher installation.** A researcher
without a snapshot gets a bounded timeout and an explicit failure.

## Validation and limitations

Unit tests use a tiny synthetic SQLite corpus and deliberately test corruption,
incomplete sources, unreviewed omissions, expired manifests, damaged copies,
concurrent installers, abandoned locks, and data-preserving failures. They do
**not** establish that the Cuttlefish filesystem honors lock semantics, that
another user has the correct group membership, or that the live scraper output
matches all 106 configured source identities. These require authorized real
cluster acceptance tests.

Vendor documentation is not indexed by this workflow. Do not cache Gaussian,
Inc. manual text without obtaining the needed permission first.
