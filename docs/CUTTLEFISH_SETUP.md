# Inkly Setup on Cuttlefish

This is the main setup guide for installing and validating Inkly on the Cuttlefish HPC cluster.

`dev` is the researcher release candidate. A successful installation does not
by itself establish documentation availability, independent account access, or
Windows Desktop acceptance. Complete the checks below before researcher use.

## Requirements

Before starting, you need:

- your own Cuttlefish account
- GitHub access to the ALICE Lab organization
- access to the hpc-ink-setup repository
- Python 3.9 or newer with `venv` and `pip`
- Git
- access to the cluster-managed model command configured in `config.toml`

Do not use another person's Cuttlefish account or Inkly installation.

## 1. Clone Inkly

```bash
cd ~
git clone --branch dev https://github.com/thealice-lab/hpc-ink-setup.git
cd hpc-ink-setup
git rev-parse HEAD
```

If you were assigned a development branch, switch to it before installing:

```bash
git fetch origin
git checkout <assigned-branch>
git pull --ff-only origin <assigned-branch>
```

## 2. Run Setup

```bash
bash setup.sh
```

The setup process creates Inkly's private Python environment, installs required Python packages, installs the Gaussian documentation scraper dependency, installs the Inkly runtime, and creates the ink launcher.

Setup installs into your own `~/.inkly`, preserves an existing configuration,
and may add its bin directory to your shell startup file. Review the shared
logging notice and [logging settings](SHARED_OBSERVABILITY.md): the template
enables lab-scoped collection where access is permitted. To test without
touching an existing installation or shared storage, use the isolated validation
procedure below instead of running setup in your usual home.

## 3. Reload Your Shell

```bash
source ~/.bashrc
```

Then verify the installed command:

```bash
which ink
type -a ink
readlink -f "$(which ink)"
```

The normal installed launcher should be under your own ~/.inkly directory.

## 4. Verify the Inkly Environment

```bash
~/.inkly/venv/bin/python --version
~/.inkly/venv/bin/python -c "from gaussian_scraper.search import search_docs; print('Gaussian scraper import: OK')"
head -n 1 ~/.inkly/bin/ink
```

From outside the source checkout, check installed profile detection:

```bash
~/.inkly/venv/bin/python -c 'import sys; from pathlib import Path; sys.path.insert(0, str(Path.home()/".inkly/lib")); from inkly.cluster_profile import resolve_cluster_profile; p=resolve_cluster_profile(); print(p["name"] if p else "NO MATCH")'
```

The installed shebang should name your `~/.inkly/venv/bin/python`; the detected
profile should be `Cuttlefish` without a personal profile environment override.

**A successful scraper import does not mean documentation is installed.** The
scraper normally reads `~/.inkly/gaussian.db`. This is separate from
`~/.inkly/jobs.db`, which contains personal job analytics. A missing or empty
Gaussian corpus is a release blocker, even when setup succeeds. Use the approved
shared documentation bootstrap when available; do not copy another researcher's
personal database or initiate a scraper build on the login node without approval.

Verify actual retrieval without printing document contents:

```bash
~/.inkly/venv/bin/python -c 'from pathlib import Path; import sqlite3; p=Path.home()/".inkly/gaussian.db"; assert p.is_file(), "Gaussian corpus is missing"; c=sqlite3.connect(p.as_uri()+"?mode=ro", uri=True); assert c.execute("PRAGMA integrity_check").fetchone()==("ok",); c.close(); from gaussian_scraper.search import search_docs; hits=search_docs("gaussian", "Gaussian geometry optimization", top_k=5); assert any(h.score>0 for h in hits), "No relevant passages"; print("Gaussian integrity and retrieval: OK")'
```

This is a runtime smoke check; it does not replace a versioned completeness
manifest or prove every configured source was indexed.

## 5. Basic Inkly Tests

```bash
ink "Reply with exactly INKLY_INSTALL_OK"
ink "What is Gaussian?"
ink "How do I run Gaussian on Cuttlefish?"
```

Cuttlefish-specific answers must use trusted local cluster information. Documentation from other institutions must not be presented as verified Cuttlefish commands or policies.

Confirm the first response is `INKLY_INSTALL_OK`. Review the Gaussian answers
against the bundled trusted profile. Do not submit a calculation to check
installation. Model availability and an occasional correct answer do not prove
all scientific or cluster guidance is correct.

## 6. Developer Validation

If you are working on Inkly source code, run:

```bash
python3 -m venv "$HOME/.venvs/inkly-dev"
"$HOME/.venvs/inkly-dev/bin/python" -m pip install -r requirements.txt pytest==8.4.2 ruff==0.16.7

"$HOME/.venvs/inkly-dev/bin/python" -m pytest -q
"$HOME/.venvs/inkly-dev/bin/python" -m ruff check .
"$HOME/.venvs/inkly-dev/bin/python" -m ruff format --check .
git diff --check
```

## 7. Repository vs Installed Inkly

The repository and the installed Inkly runtime are separate.

- ./ink runs the runtime from the repository checkout. When started with the
  system Python after setup, it re-executes through `~/.inkly/venv/bin/python`
  so the scraper and its dependencies are available. An explicitly activated
  developer virtual environment remains authoritative.
- ink runs the installed Inkly launcher.

After changing runtime code, rerun:

```bash
bash setup.sh
```

Then verify which installed launcher is being used with:

```bash
which ink
readlink -f "$(which ink)"
```

## Isolated fresh-install validation

Prefer a separate authorized research account for acceptance. When unavailable,
run this from a reviewed candidate checkout on Cuttlefish:

```bash
python3 scripts/validate_fresh_install.py --ref dev --parent "$HOME" --check-model
```

Use the candidate feature branch with `--ref` when validating an unmerged fix.
The tool creates a new private `inkly-fresh-*` directory, clones that ref, and
runs setup twice with a sanitized child-process environment and a fresh HOME.
It disables all logging and shell modifications in the test configuration,
uses no existing scraper checkout or database, and never activates this HOME
in your interactive shell. It checks the original launcher, configuration and
shell-startup fingerprints afterward. Nothing is deleted automatically.

The test prints and saves `report.json`; command output stays in a private
`commands.log` inside the test directory. Do not publish that log. A nonzero
exit and `gaussian_database_exists=false` explicitly identify unprovisioned
documentation rather than calling the installation ready. Without
`--check-model`, the model is recorded as untested. This procedure submits no
Slurm jobs, starts no scraper build, and does not enable shared logging.

The result is **same-account isolation**, not cross-account proof. It cannot
establish another user's group membership, storage access, model entitlement,
or Windows onboarding. Those checks remain required on real authorized
researcher accounts. Retain the test directory until its report is reviewed.

## Troubleshooting

### ink: command not found

Reload your shell:

```bash
source ~/.bashrc
```

Then verify that the launcher exists:

```bash
ls -l ~/.inkly/bin/ink
```

### Gaussian documentation import fails

Rerun:

```bash
bash setup.sh
```

Then repeat the Gaussian scraper import check.

### Repository behavior differs from installed ink

Rerun setup.sh so the installed runtime matches the validated repository checkout.

## Security

Do not commit or publish raw Slurm output, Gaussian runtime logs, account-specific information, validation artifacts, credentials, or unnecessary cluster infrastructure details.
