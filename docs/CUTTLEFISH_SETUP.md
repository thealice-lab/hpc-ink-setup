# Inkly Setup on Cuttlefish

This is the main setup guide for installing and validating Inkly on the Cuttlefish HPC cluster.

## Requirements

Before starting, you need:

- your own Cuttlefish account
- GitHub access to the ALICE Lab organization
- access to the hpc-ink-setup repository
- Python 3
- Git

Do not use another person's Cuttlefish account or Inkly installation.

## 1. Clone Inkly

```bash
cd ~
git clone https://github.com/thealice-lab/hpc-ink-setup.git
cd hpc-ink-setup
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
```

## 5. Basic Inkly Tests

```bash
ink hello
ink "What is Gaussian?"
ink "How do I run Gaussian on Cuttlefish?"
```

Cuttlefish-specific answers must use trusted local cluster information. Documentation from other institutions must not be presented as verified Cuttlefish commands or policies.

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

- ./ink runs the launcher from the repository checkout.
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
