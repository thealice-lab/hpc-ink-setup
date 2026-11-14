# Independent Cuttlefish researcher acceptance

Status: **NOT RUN on a second account**. Synthetic clean-HOME subprocess tests
prove application behavior with temporary fixtures; they cannot establish Unix
group membership, NFS semantics, model entitlement, or an installed Windows EXE.
This checklist authorizes no live operation by itself. Run it only under a
separately approved second-account installation test, after a maintainer has
verified a published corpus. Never use another researcher's account or files.

## Prerequisites and stop conditions

- An authorized researcher other than the original developer performs the check
  using their own normal authentication. No passwords/MFA codes go into notes.
- Record the reviewed Inkly ref/commit, scraper revision, exact config SHA-256,
  approved corpus root and reviewed omission-policy hash. Verify pending/active
  builds separately; do not assume job 2406907 finished. Leave 2406905 alone.
- A publisher confirms a fully validated snapshot exists. Group-reader access
  and administrator-confirmed parent traversal/read permissions are required.
  Do not grant access, repair modes, create a shared root or take a build lock
  during this test. Treat an unreadable root/snapshot as BLOCKED.
- Obtain installation/network approval: setup downloads dependencies and the
  scraper. No new Slurm submission, scrape, active runtime replacement or
  logging-policy change is part of this checklist.

## Isolated test on that second account

1. Use a separate reviewed source checkout and the account's approved system
   Python. Do not source a new HOME into the interactive shell. Record existing
   launcher/config/shell fingerprints privately, with existing job/corpus state
   left untouched. Use a new private test parent with enough storage.
2. Verify shared `current.json`, referenced `manifest.json` and `gaussian.db`
   are readable with the exact reviewed scraper/config/omission policy. Run
   `python -m inkly.gaussian_docs validate` from the reviewed checkout with
   `--shared-root`, `--scraper-dir` and, if needed, `--exceptions`. Exit must be
   zero. No shared artifact should be written by this reader.
3. Execute the isolated validator with an explicit approved shared root:

   ```bash
   python3 scripts/validate_fresh_install.py --ref REVIEWED_BRANCH --parent "$HOME" \
     --shared-root /APPROVED/CORPUS/ROOT \
     --exceptions /PATH/TO/REVIEWED/exceptions.json
   ```

   Replace placeholders; omit the exceptions option if none are approved.
   The tool clones the branch into a new directory and reports its actual
   commit. Check that it is the reviewed one. It creates a new HOME, disables
   all logging and shell changes, runs setup twice, and verifies both installed
   and source launcher usage. It copies documentation read-only from the shared
   publication. This does not install into the researcher's existing runtime.
4. Require exit zero and all documented report gates: private runtime/venv,
   preserved config and original installation, unchanged shell files, correct
   installed shebang, both launchers loading, Cuttlefish profile selected,
   private Gaussian DB present/integrity valid with nonzero search matches,
   separate personal jobs DB, and `shared_corpus_requested=true`.
   A script failure remains a failure even if some report gates pass.
5. In a child process configured **only for the isolated HOME**, confirm bare
   `ink` resolves to that HOME's `.inkly/bin/ink` from an unrelated directory;
   confirm `./ink` from the isolated checkout uses its source runtime with
   the private venv's dependencies. Neither no-argument usage check invokes a
   model or submits work. The validator also checks installed/source launchers;
   the automated Linux test covers the bare PATH and `./ink` executable forms.
6. Confirm the copied DB belongs to this researcher, has private mode `0600`,
   and matches the validated shared DB's SHA-256. Confirm it is a separate file,
   not a hard link to shared storage. Repeat setup/copy: identical DB succeeds;
   shared hashes, the account's original files, and private jobs/history remain
   unchanged. Never replace a real DB to exercise the failure case.
7. If separately authorized to query the model, add `--check-model` on a new
   isolated run and require the exact synthetic response `INKLY_INSTALL_OK`.
   Record model behavior separately. Without that option the model is untested.
   Review general Gaussian attribution and local Cuttlefish scoping with benign
   questions; no calculation, SBATCH, cancellation, or job submission is needed.
8. Keep `report.json` and `commands.log` private. Share only sanitized gate
   booleans, exact code/config/database hashes, aggregate counts, date and
   pass/fail category. A successful second account may be recorded manually as
   live acceptance evidence; the script intentionally continues reporting
   `cross_account_verified=false` because it cannot identify an independent
   human/account by itself. Do not mark live acceptance from that flag alone.

## Offline failure coverage and remaining boundaries

`tests/test_independent_researcher_corpus.py` uses real CLI subprocesses, fresh
HOME/USERPROFILE values, synthetic clean scraper Git repositories and tiny
validated snapshots. It covers two independent private copies, repeated install,
missing/empty shared roots, invalid pointers/manifests/databases, opt-in expiry,
existing empty/different private DB preservation, jobs.db rejection, read-only
validation and a portable injected permission failure. Shared artifact hashes
are checked for reader-side mutation. No fixture invokes Nathan's scraper.

Unix mode inheritance, real group-reader and outsider denial, network mounts,
lock/rename/hard-link support, a full network setup in a clean Linux environment,
and actual model retrieval remain external acceptance gates. Do not simulate
live access failure by deleting, corrupting or chmod-ing a shared/private live
DB. Use the synthetic suite instead. Restore no permissions or runtime as part
of the reader test, and retain isolated test directories for review.
