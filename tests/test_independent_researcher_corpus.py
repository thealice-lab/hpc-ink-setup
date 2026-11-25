"""Real corpus CLI subprocesses with synthetic snapshots and isolated researcher homes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from inkly import gaussian_docs as corpus
from scripts.validate_fresh_install import isolated_environment
from tests.test_gaussian_docs import write_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def researcher_install(tmp_path):
    scraper = tmp_path / "synthetic scraper"
    config = scraper / "configs/gaussian.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'name="gaussian"\nkeywords=["gaussian"]\n[[html_sources]]\n'
        'label="Synthetic Gaussian guide"\nurl="https://example.org/gaussian"\n',
        encoding="utf-8",
    )
    git = shutil.which("git")
    assert git, "Git is required for the real scraper-revision validation"
    for args in (
        ["init", "-q"],
        ["config", "core.autocrlf", "false"],
        ["add", "configs/gaussian.toml"],
        [
            "-c",
            "user.name=Synthetic Researcher",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Synthetic config",
        ],
    ):
        subprocess.run([git, *args], cwd=scraper, check=True, capture_output=True)
    revision = subprocess.check_output(
        [git, "rev-parse", "HEAD"], cwd=scraper, text=True
    ).strip()
    spec = corpus.specification(config, revision)
    shared = tmp_path / "shared snapshot"
    shared.mkdir()
    snapshot = corpus.ensure_snapshot(shared, spec, build=write_database, timeout=0)
    fixture = scraper, shared, snapshot, spec, git
    # Confirm failures below reach corpus validation rather than an unrelated
    # Git environment error (global CRLF settings must not affect the fixture).
    result = run_corpus(fixture, tmp_path / "fixture validation", action="validate")
    assert result.returncode == 0, result.stderr
    return fixture


def run_corpus(fixture, home, *extra, action="ensure", shared=None):
    scraper, default_shared, _, _, git = fixture
    home.mkdir(parents=True, exist_ok=True)
    env = isolated_environment(home, Path(sys.executable))
    env["USERPROFILE"] = str(home)  # Path.home() uses USERPROFILE on Windows.
    if os.name == "nt":
        # Windows process startup and Git need OS paths, not user overrides.
        for key in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["PATH"] = os.pathsep.join((str(Path(git).parent), os.defpath))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "inkly.gaussian_docs",
            action,
            "--shared-root",
            str(shared if shared is not None else default_shared),
            "--scraper-dir",
            str(scraper),
            "--timeout",
            "0",
            *extra,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def shared_fingerprints(shared):
    return {
        str(path.relative_to(shared)): corpus.digest(path)
        for path in shared.rglob("*")
        if path.is_file()
    }


def test_two_clean_researcher_homes_get_independent_valid_private_copies(
    researcher_install, tmp_path, monkeypatch
):
    _, shared, snapshot, spec, _ = researcher_install
    before = shared_fingerprints(shared)
    monkeypatch.setenv("INKLY_GAUSSIAN_SHARED_ROOT", "wrong-personal-root")
    monkeypatch.setenv("PYTHONPATH", "wrong-personal-package-path")
    targets = []
    for account in ("first researcher", "second researcher"):
        home = tmp_path / account
        state = home / ".inkly"
        state.mkdir(parents=True)
        jobs = state / "jobs.db"
        jobs.write_bytes(b"synthetic private Slurm history")
        for _ in range(2):
            result = run_corpus(researcher_install, home)
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            assert report["source_count"] == report["passage_count"] == 1
            assert jobs.read_bytes() == b"synthetic private Slurm history"
        target = state / "gaussian.db"
        targets.append(target)
        assert corpus.inspect_database(target, spec)[
            "database_sha256"
        ] == corpus.digest(snapshot / "gaussian.db")
        assert not os.path.samefile(target, snapshot / "gaussian.db")
        assert not list(state.glob(".corpus-copy-*"))
        if os.name == "posix":
            assert target.stat().st_mode & 0o777 == 0o600
    assert not os.path.samefile(*targets)
    assert shared_fingerprints(shared) == before


@pytest.mark.parametrize(
    "failure", ["missing-root", "empty-root", "pointer", "database", "manifest"]
)
def test_cli_install_failure_never_leaves_partial_private_database(
    researcher_install, tmp_path, failure
):
    _, shared, snapshot, _, _ = researcher_install
    if failure == "missing-root":
        shared = tmp_path / "not provisioned"
    elif failure == "empty-root":
        shared = tmp_path / "no publication"
        shared.mkdir()
    elif failure == "pointer":
        (shared / "current.json").write_text('{"snapshot":"../escape"}')
    else:
        artifact = snapshot / (
            "gaussian.db" if failure == "database" else "manifest.json"
        )
        artifact.chmod(0o600)
        artifact.write_bytes(b"corrupt synthetic artifact")
    home = tmp_path / "fresh researcher"
    result = run_corpus(researcher_install, home, shared=shared)
    assert result.returncode == 1
    assert "Gaussian documentation setup failed" in result.stderr
    assert not (home / ".inkly/gaussian.db").exists()
    assert not (home / ".inkly/jobs.db").exists()
    assert not list(shared.glob(".building-*"))
    if failure == "missing-root":
        assert not shared.exists()


@pytest.mark.parametrize("existing", [b"", b"existing personal corpus"])
def test_cli_preserves_existing_private_database_and_jobs(
    researcher_install, tmp_path, existing
):
    home = tmp_path / "existing researcher"
    state = home / ".inkly"
    state.mkdir(parents=True)
    database, jobs = state / "gaussian.db", state / "jobs.db"
    database.write_bytes(existing)
    jobs.write_bytes(b"personal job history")
    result = run_corpus(researcher_install, home)
    assert result.returncode == 1
    assert "preserved" in result.stderr
    assert database.read_bytes() == existing
    assert jobs.read_bytes() == b"personal job history"
    assert not list(state.glob(".corpus-copy-*"))


def test_validate_is_read_only_and_jobs_destination_is_rejected(
    researcher_install, tmp_path
):
    _, shared, _, _, _ = researcher_install
    before = shared_fingerprints(shared)
    home = tmp_path / "reader"
    result = run_corpus(researcher_install, home, action="validate")
    assert result.returncode == 0, result.stderr
    assert not (home / ".inkly").exists()
    jobs = home / "jobs.db"
    jobs.write_bytes(b"preserve private history")
    result = run_corpus(researcher_install, home, "--destination", str(jobs))
    assert result.returncode == 1
    assert "never jobs.db" in result.stderr
    assert jobs.read_bytes() == b"preserve private history"
    assert shared_fingerprints(shared) == before


def test_unreadable_snapshot_has_bounded_failure_without_copy(
    researcher_install, tmp_path, monkeypatch
):
    # Portable injected access denial; actual Unix group-reader access is a live gate.
    _, shared, snapshot, spec, _ = researcher_install
    original = Path.open

    def deny_manifest(path, *args, **kwargs):
        if path == snapshot / "manifest.json":
            raise PermissionError("synthetic permission denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_manifest)
    destination = tmp_path / "reader/.inkly/gaussian.db"
    with pytest.raises(corpus.CorpusError, match="timeout"):
        corpus.ensure_snapshot(shared, spec, timeout=0)
    with pytest.raises(PermissionError):
        corpus.copy_private(snapshot, destination, spec)
    assert not destination.exists()


def test_cli_expiry_policy_rejects_without_replacing_private_file(
    researcher_install, tmp_path
):
    from datetime import datetime, timedelta, timezone

    _, _, snapshot, _, _ = researcher_install
    path = snapshot / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["created_at"] = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).isoformat()
    path.chmod(0o600)
    path.write_text(json.dumps(manifest))
    result = run_corpus(researcher_install, tmp_path / "reader", "--max-age-days", "1")
    assert result.returncode == 1
    assert not (tmp_path / "reader/.inkly/gaussian.db").exists()
