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

## Recovery runbook (readers)

Use this procedure from a reviewed Inkly checkout containing
`inkly/gaussian_docs.py`, with an existing Python environment that can import it.
An older installed runtime may not have this command: do not replace that
installation merely to diagnose a corpus failure. These examples assume Bash
on Cuttlefish, an already installed scraper, and no shell startup changes.
They do not submit jobs, run the scraper, or modify the shared corpus.

Obtain the approved root and reviewed exception-policy file from the maintainer.
The selected corpus root as of 2026-11-13 is
`/scratch/inklys-logs/gaussian-corpus`; this records the selected location, not
proof of a successfully published snapshot or current access. The separate
observability directory `/scratch/inklys-logs/shared` must stay private.

Set these variables to the existing paths for **your own account**:

```bash
CORPUS_ROOT=/APPROVED/CORPUS/ROOT
SCRAPER_DIR="$HOME/.inkly/src/gaussian-docs-scraper"
INKLY_PYTHON="$HOME/.inkly/venv/bin/python"
# Only when omissions were explicitly reviewed for this exact config:
EXCEPTIONS=/PATH/TO/REVIEWED/exceptions.json
```

From the reviewed checkout root, validate the currently published snapshot:

```bash
"$INKLY_PYTHON" -m inkly.gaussian_docs validate \
  --shared-root "$CORPUS_ROOT" --scraper-dir "$SCRAPER_DIR" \
  --exceptions "$EXCEPTIONS"
```

Omit `--exceptions "$EXCEPTIONS"` entirely if no omissions are approved. The
scraper checkout must be clean and at the exact revision used to publish the
snapshot; obtain the reviewed revision from the maintainer instead of updating
or switching an operational checkout. A different revision fails validation
even if its source config happens to match. Do not loosen validation to fix it.

Exit status 0 prints sanitized JSON with source/passage counts, config and DB
hashes, scraper revision, and reviewed omissions. A nonzero exit is a failure;
inspect its explanation before trying installation. `validate` checks only the
shared snapshot and does not install or validate an existing private copy.

After successful validation, install a private copy without rerunning setup:

```bash
"$INKLY_PYTHON" -m inkly.gaussian_docs ensure \
  --shared-root "$CORPUS_ROOT" --scraper-dir "$SCRAPER_DIR" \
  --exceptions "$EXCEPTIONS" \
  --destination "$HOME/.inkly/gaussian.db" --timeout 0
```

Again omit the exceptions arguments when none are approved. `--timeout 0`
makes this a single immediate attempt; the default waits up to 120 seconds
for a publisher. Neither form builds. Repeating installation of identical
validated bytes succeeds. Any different existing private file, including a
corrupt/empty file, is preserved and causes failure. A symlink destination is
rejected. A candidate is validated after copying, then published using an
atomic hard link that refuses to replace a concurrently created destination.
If the private filesystem does not support hard links, stop and ask the
maintainer; do not substitute a force-copy command. `jobs.db` is never a valid
destination and is never part of corpus recovery.

### Failure decisions

| Symptom | Meaning and next action |
|---|---|
| Root missing/not provisioned | Scratch may have been purged, or the approved path is wrong. Confirm with the maintainer; readers must not create a replacement shared root. |
| Permission denied or bounded timeout | Confirm group membership, parent traversal, root listing, and file readability with the administrator. `ensure` can report a generic timeout for an unreadable/invalid current snapshot; `validate` exposes the immediate failure. Do not broaden permissions or take the publisher's lock. |
| Missing/malformed `current.json` or referenced snapshot | No usable publication. Do not edit the pointer or copy a file from `.building-*`. Ask the publisher to review and recover. |
| Hash, schema, SQLite integrity, source or passage failure | Treat the snapshot as unusable. Do not hand-edit the DB/manifest or add omissions to bypass checks. Request a reviewed rebuild. |
| Revision/config mismatch | Align a separate reviewed diagnostic checkout with the published revision and reviewed config; do not change an active installed scraper. |
| Expired timestamp | Expiry is opt-in: add `--max-age-days N` to both validate and ensure when the maintainer specifies a freshness policy. Default `0` disables age expiry, not integrity checks. Future timestamps over five minutes and timezone-naive timestamps are rejected. |
| Existing personal DB preserved | Keep the file. Follow the candidate-copy procedure below and arrange an explicit migration. Never run `rm`, `cp -f`, or overwrite it to make setup pass. |

Validation checks the manifest against the actual config bytes and full scraper
revision, source identity/type/URL/site/tag/tool, every source's nonempty ordered
passages and content hash, foreign keys/orphans, SQLite integrity, and the
database file hash. Active SQLite `-wal`, `-shm`, or `-journal` sidecars are
rejected. Manifest counts are recomputed, not trusted. Every missing configured
label needs a nonempty, reviewed reason bound to the exact config hash; the
historical 100-of-106 audit is not a blanket omission policy. Checksums detect
damage and inconsistency; they are not digital signatures and do not establish
trust in an unapproved publisher or make external text local cluster evidence.

### Preserve an existing private database

Do not remove or rename the operational file as part of diagnosis. First install
a replacement candidate into a new private directory (same approved spec and
exceptions as above):

```bash
candidate=$(mktemp -d "$HOME/.inkly/gaussian-recovery.XXXXXXXX")
chmod 700 "$candidate"
"$INKLY_PYTHON" -m inkly.gaussian_docs ensure \
  --shared-root "$CORPUS_ROOT" --scraper-dir "$SCRAPER_DIR" \
  --exceptions "$EXCEPTIONS" \
  --destination "$candidate/gaussian.db" --timeout 0
```

Check the command's exit status. A successful candidate is not yet the runtime
database. Record its returned SHA-256 and retain the original private file.
Arrange a user-approved maintenance window: stop any private corpus writer,
preserve and verify a backup of the old DB (including SQLite sidecars if
present), then explicitly choose whether to migrate to the validated candidate.
An old DB with live sidecars needs a consistent SQLite backup by its owner,
not a raw copy of only `gaussian.db`. Do not touch `jobs.db`. This runbook
intentionally performs no automatic operational-file replacement.

### What a scratch purge changes

Shared scratch is a distribution cache, not archival storage. A purge can
remove the root, pointer, snapshots, lock or build staging. An already installed
private copy remains usable if its own storage and contents are intact; normal
retrieval uses that private copy, not the shared pointer. A scratch purge does
not itself validate private contents or prove their freshness. Fresh installs
fail until an approved snapshot is republished. Do not infer that a queued
build succeeded from the presence of a directory or lock file.

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

### Maintainer recovery and publication order

These steps describe a **future, separately authorized** operation, not
permission to run a build now. Existing jobs 2406907 and 2406905 and their
files are outside this runbook's execution scope. Check status read-only if
needed; never cancel/resubmit them or start another build merely because a
reader timed out.

1. Confirm no active/queued approved build already owns this work. Review the
   failure, exact Inkly/scraper revisions, config hash, omission reasons,
   retained-source requirements, source permissions and resource/network policy.
   Keep a durable sanitized record of those inputs outside purgeable scratch.
2. If the root was purged, have the administrator restore the approved group,
   traversal and publisher/reader permissions. Readers need parent traversal,
   root/snapshot listing and manifest/database read access. Restrictive parent
   `0710`, root/snapshot `2750`, pointer `0640`, and artifacts `0440` require
   the correct group; modes alone do not prove independent-reader access.
   The stable publisher lock is `0600`; use the designated publisher account,
   not multiple different accounts trying to acquire another owner's lock.
3. Obtain explicit authorization for a new Slurm allocation before starting
   Nathan's existing scraper. The module's `--allow-build` flag enables code;
   it does **not** enforce Slurm placement or grant authorization. A reviewed
   maintainer invocation uses `ensure` with the same root, scraper, reviewed
   exceptions and a new private validation destination, plus `--allow-build`,
   only inside that authorized allocation. No `sbatch` command is supplied here.
4. Leave `.build.lock` in place, even after interruption. The OS releases the
   process lock on death. Unlinking/stealing it can permit concurrent writers.
   NFS process-lock and atomic-rename/hard-link behavior must be independently
   verified before production reliance; local tests cannot prove that property.
5. The builder works behind a private `.building-*` directory, validates all
   content, sets group-readable immutable snapshot files, atomically renames the
   snapshot, and atomically replaces `current.json` **last**. It confirms the
   lock inode still matches before publication, refusing a lock detached by a
   purge. Readers never read staging. Staging cleanup is best-effort: an NFS
   busy/nonempty directory or another filesystem cleanup error produces a
   warning with only the generated `.building-*` basename and errno. The
   original build exception still propagates; a validated publication remains
   successful even if cleanup fails. Warning-as-error settings cannot change
   that outcome. Failed cleanup or a killed process can leave private staging
   for authorized maintainer review after the build exits. Cleanup does not
   retry, change permissions, remove the lock or publish an invalid snapshot.
   Build callbacks must explicitly close SQLite connections and other handles
   on every exit path (`with sqlite3.connect(...)` alone does not close a
   connection); open handles can prevent NFS from removing temporary files.
   Do not delete live staging or "repair" a pointer by hand.
6. A failed build leaves the previous publication in place. A crash after the
   snapshot rename but before the pointer update can leave an unreferenced
   snapshot; its mere existence is not a publication. Request another approved
   recovery attempt, not reader-side pointer edits. Preserve historical
   snapshots under the administrator's retention policy; the module does not
   garbage-collect them automatically.
7. Validate the published pointer with the exact reviewed inputs, then validate
   a private copy and test actual retrieval. Have a separately authorized
   researcher verify group-reader access without writing the shared root.
   Do not announce live acceptance from publisher-only or synthetic tests.

Offline verification from a disposable development checkout is:

```bash
python -m pytest tests/test_gaussian_docs.py -q
```

Those tests create temporary synthetic databases and exercise missing/invalid
snapshots, damaged transfers, no-overwrite behavior, writer election, lock
recovery and failed-publication preservation. They run no live scraper or jobs.

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
