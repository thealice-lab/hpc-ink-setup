"""Corpus lifecycle tests use synthetic documentation, never live scraping or Slurm."""

from datetime import datetime, timedelta, timezone
from contextlib import closing
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time

import pytest

from inkly import gaussian_docs as corpus


def write_database(
    path, labels=("Synthetic Gaussian guide",), text="Gaussian DFT documentation"
):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY, label TEXT, source_type TEXT,"
            "url TEXT, site TEXT, tag TEXT, tool TEXT, content_hash TEXT);"
            "CREATE TABLE passages (source_id INTEGER REFERENCES sources(id), position INTEGER, text TEXT);"
        )
        for i, label in enumerate(labels):
            db.execute(
                "INSERT INTO sources VALUES (?, ?, 'html', 'https://example.org/gaussian', NULL, NULL, NULL, ?)",
                (i, label, hashlib.sha256(text.encode()).hexdigest()),
            )
            db.execute("INSERT INTO passages VALUES (?, 0, ?)", (i, text))
        db.commit()


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / "shared"
    root.mkdir(mode=0o700)
    config = tmp_path / "gaussian.toml"
    config.write_text(
        'name = "gaussian"\nkeywords = ["gaussian"]\n'
        '[[html_sources]]\nlabel = "Synthetic Gaussian guide"\n'
        'url = "https://example.org/gaussian"\n',
        encoding="utf-8",
    )
    spec = corpus.specification(config, "a" * 40)
    return root, config, spec


def publish(environment):
    root, _, spec = environment
    return corpus.ensure_snapshot(root, spec, build=write_database, timeout=0)


def test_complete_snapshot_copy_and_repeated_install(environment, tmp_path):
    snapshot = publish(environment)
    root, _, spec = environment
    assert not list(root.glob("*.db"))
    manifest = corpus.validate_snapshot(snapshot, spec)
    assert manifest["source_count"] == manifest["passage_count"] == 1
    target = tmp_path / "private/gaussian.db"
    assert corpus.copy_private(snapshot, target, spec) == manifest
    assert corpus.copy_private(snapshot, target, spec) == manifest
    assert corpus.digest(target) == manifest["database_sha256"]
    assert (
        corpus.ensure_snapshot(
            root, spec, build=lambda _: pytest.fail("duplicate build")
        )
        == snapshot
    )
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
        assert (snapshot / "gaussian.db").stat().st_mode & 0o777 == 0o440


@pytest.mark.skipif(os.name != "posix", reason="Unix permission bits required")
def test_published_snapshot_readable_with_private_umask(environment):
    root, _, spec = environment
    root.chmod(0o2750)
    previous = os.umask(0o077)
    try:
        snapshot = corpus.ensure_snapshot(root, spec, build=write_database, timeout=0)
    finally:
        os.umask(previous)

    assert snapshot.stat().st_gid == root.stat().st_gid
    assert snapshot.stat().st_mode & 0o2000
    for name in ("gaussian.db", "manifest.json"):
        assert (snapshot / name).stat().st_gid == root.stat().st_gid
    assert (root / "current.json").stat().st_gid == root.stat().st_gid
    assert snapshot.stat().st_mode & 0o777 == 0o750
    assert (snapshot / "gaussian.db").stat().st_mode & 0o777 == 0o440
    assert (snapshot / "manifest.json").stat().st_mode & 0o777 == 0o440
    assert (root / "current.json").stat().st_mode & 0o777 == 0o640
    assert (root / ".build.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="Unix permission bits required")
def test_legacy_build_lock_is_made_private(environment):
    root, _, _ = environment
    lock = root / ".build.lock"
    lock.touch()
    lock.chmod(0o660)
    with corpus._try_lock(root) as fd:
        assert fd is not None
        assert lock.stat().st_mode & 0o777 == 0o600


def test_missing_root_is_not_created(tmp_path, environment):
    with pytest.raises(corpus.CorpusError, match="provisioned"):
        corpus.ensure_snapshot(tmp_path / "missing", environment[2], timeout=0)
    assert not (tmp_path / "missing").exists()


def test_wait_only_never_starts_build(environment):
    root, _, spec = environment
    with pytest.raises(corpus.CorpusError, match="approved rebuild"):
        corpus.ensure_snapshot(root, spec, timeout=0.02, poll=0.01)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("content", [b"", b"not a SQLite database"])
def test_invalid_build_is_never_published(environment, content):
    root, _, spec = environment
    with pytest.raises((corpus.CorpusError, sqlite3.DatabaseError)):
        corpus.ensure_snapshot(root, spec, build=lambda db: db.write_bytes(content))
    assert not (root / "current.json").exists()


def test_failed_build_preserves_previous_and_releases_lock(environment):
    old = publish(environment)
    root, _, spec = environment
    changed = {**spec, "scraper_revision": "b" * 40}

    def failure(_):
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError):
        corpus.ensure_snapshot(root, changed, build=failure)
    assert corpus.current_snapshot(root, spec)[0] == old
    new = corpus.ensure_snapshot(root, changed, build=write_database)
    assert new != old
    assert old.exists()


def test_expired_snapshot_rebuilt_without_deleting_old(environment):
    old = publish(environment)
    root, _, spec = environment
    path = old / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["created_at"] = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).isoformat()
    os.chmod(path, 0o600)
    path.write_text(json.dumps(manifest))
    with pytest.raises(corpus.CorpusError, match="expired"):
        corpus.validate_snapshot(old, spec, max_age=60)
    assert corpus.ensure_snapshot(root, spec, build=write_database, max_age=60) != old
    assert old.exists()


def test_config_version_and_reviewed_omissions(environment, tmp_path):
    root, config, spec = environment
    config.write_text(
        config.read_text()
        + '\n[[html_sources]]\nlabel="Missing guide"\nurl="https://example.org/missing"\n'
    )
    changed = corpus.specification(config, "a" * 40)
    with pytest.raises(corpus.CorpusError, match="sources are missing"):
        corpus.ensure_snapshot(root, changed, build=write_database)
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(
        json.dumps(
            {
                "config_sha256": corpus.digest(config),
                "allowed_missing": {"Missing guide": "Reviewed HTTP 404; issue 123"},
            }
        )
    )
    reviewed = corpus.specification(config, "a" * 40, exceptions)
    snapshot = corpus.ensure_snapshot(root, reviewed, build=write_database)
    assert (
        corpus.validate_snapshot(snapshot, reviewed)["missing_sources"]
        == reviewed["allowed_missing"]
    )
    with pytest.raises(corpus.CorpusError):
        corpus.validate_snapshot(snapshot, spec)
    with pytest.raises(corpus.CorpusError, match="sources are missing"):
        corpus.validate_snapshot(snapshot, changed)
    config.write_text(config.read_text() + "\n# changed configuration\n")
    with pytest.raises(corpus.CorpusError, match="configuration hash"):
        corpus.specification(config, "a" * 40, exceptions)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE passages SET text='changed'",
        "INSERT INTO passages VALUES (999,0,'orphan')",
        "UPDATE sources SET url='https://wrong.example'",
        "DELETE FROM passages",
    ],
)
def test_integrity_and_inventory_reject_corruption(environment, tmp_path, mutation):
    path = tmp_path / "gaussian.db"
    write_database(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(mutation)
        db.commit()
    with pytest.raises(corpus.CorpusError):
        corpus.inspect_database(path, environment[2])


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_count", 100),
        ("database_sha256", "0" * 64),
        ("schema_version", 99),
        ("created_at", "invalid"),
    ],
)
def test_manifest_tampering_is_rejected(environment, field, value):
    snapshot = publish(environment)
    path = snapshot / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    os.chmod(path, 0o600)
    path.write_text(json.dumps(manifest))
    with pytest.raises(corpus.CorpusError):
        corpus.validate_snapshot(snapshot, environment[2])


def test_live_sqlite_sidecar_is_rejected(environment, tmp_path):
    path = tmp_path / "gaussian.db"
    write_database(path)
    Path(str(path) + "-wal").touch()
    with pytest.raises(corpus.CorpusError, match="sidecars"):
        corpus.inspect_database(path, environment[2])


def test_private_copy_never_overwrites_personal_data(environment, tmp_path):
    snapshot = publish(environment)
    destination = tmp_path / "private/gaussian.db"
    destination.parent.mkdir()
    destination.write_bytes(b"existing personal database")
    jobs = destination.with_name("jobs.db")
    jobs.write_bytes(b"personal history")
    with pytest.raises(corpus.CorpusError, match="preserved"):
        corpus.copy_private(snapshot, destination, environment[2])
    with pytest.raises(corpus.CorpusError, match="never jobs.db"):
        corpus.copy_private(snapshot, jobs, environment[2])
    assert destination.read_bytes() == b"existing personal database"
    assert jobs.read_bytes() == b"personal history"


def test_private_copy_is_validated_after_transfer(environment, tmp_path, monkeypatch):
    snapshot = publish(environment)
    original = corpus.shutil.copyfile

    def damaged(source, target):
        original(source, target)
        Path(target).write_bytes(b"damaged transfer")

    monkeypatch.setattr(corpus.shutil, "copyfile", damaged)
    target = tmp_path / "private/gaussian.db"
    with pytest.raises(sqlite3.DatabaseError):
        corpus.copy_private(snapshot, target, environment[2])
    assert not target.exists()


def _concurrent_builder(root, spec, queue):
    def build(path):
        with (root / "build-count").open("a") as stream:
            stream.write("build\n")
        time.sleep(0.15)
        write_database(path)

    try:
        snapshot = corpus.ensure_snapshot(
            root, spec, build=build, timeout=10, poll=0.01
        )
        queue.put(snapshot.name)
    except Exception as exc:
        queue.put(type(exc).__name__ + ": " + str(exc))


def test_simultaneous_installers_elect_one_builder(environment):
    root, _, spec = environment
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(target=_concurrent_builder, args=(root, spec, queue))
        for _ in range(3)
    ]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert len(set(results)) == 1
    assert results[0].startswith("snapshot-")
    assert (root / "build-count").read_text().splitlines() == ["build"]


def _abandoned_lock(root):
    with corpus._try_lock(root) as fd:
        assert fd is not None
        os._exit(0)


def test_abandoned_process_lock_recovers_without_deleting_lock(environment):
    root, _, spec = environment
    context = multiprocessing.get_context("spawn")
    worker = context.Process(target=_abandoned_lock, args=(root,))
    worker.start()
    worker.join(timeout=10)
    assert worker.exitcode == 0
    lock_stat = (root / ".build.lock").stat()
    corpus.ensure_snapshot(root, spec, build=write_database)
    assert os.path.samestat(lock_stat, (root / ".build.lock").stat())


def test_active_builder_timeout_does_not_steal_lock(environment):
    root, _, spec = environment
    with corpus._try_lock(root) as fd:
        assert fd is not None
        with pytest.raises(corpus.CorpusError, match="timeout"):
            corpus.ensure_snapshot(
                root,
                spec,
                build=lambda _: pytest.fail("duplicate build"),
                timeout=0.02,
                poll=0.01,
            )


def test_lock_removed_during_build_refuses_publication(environment):
    if os.name == "nt":
        pytest.skip("Windows forbids removal of an open lock file")
    root, _, spec = environment

    def build(path):
        write_database(path)
        (root / ".build.lock").unlink()
        (root / ".build.lock").touch()

    with pytest.raises(corpus.CorpusError, match="lock was removed"):
        corpus.ensure_snapshot(root, spec, build=build)
    assert not (root / "current.json").exists()


@pytest.mark.parametrize("pointer", ["../escape", "/absolute", None, "snapshot-x"])
def test_unsafe_pointer_is_rejected(environment, pointer):
    root, _, spec = environment
    (root / "current.json").write_text(json.dumps({"snapshot": pointer}))
    with pytest.raises(corpus.CorpusError, match="pointer"):
        corpus.current_snapshot(root, spec)


def test_snapshot_rejects_timezone_naive_timestamp(environment):
    snapshot = publish(environment)
    path = snapshot / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["created_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    os.chmod(path, 0o600)
    path.write_text(json.dumps(manifest))
    with pytest.raises(corpus.CorpusError, match="timestamp"):
        corpus.validate_snapshot(snapshot, environment[2])


def test_completed_build_removes_private_scratch_workspace(environment):
    root, _, _ = environment
    publish(environment)
    assert not list(root.glob(".building-*"))


def test_failed_build_removes_private_scratch_workspace(environment):
    root, _, spec = environment

    def abort(_):
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        corpus.ensure_snapshot(root, spec, build=abort)
    assert not list(root.glob(".building-*"))
    assert not (root / "current.json").exists()


def test_setup_requires_explicit_approved_shared_root():
    from pathlib import Path

    shell = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
    assert 'if [[ -n "${INKLY_GAUSSIAN_SHARED_ROOT:-}" ]]' in shell
    assert '"$VENV_PYTHON" -m inkly.gaussian_docs "${corpus_args[@]}"' in shell
    assert "--allow-build" not in shell
    assert '--destination "$INKLY_HOME/gaussian.db"' in shell


def test_cli_copies_verified_synthetic_snapshot_without_scraping(environment, tmp_path):
    import subprocess
    import sys

    root, config, _ = environment
    scraper = tmp_path / "scraper"
    (scraper / "configs").mkdir(parents=True)
    (scraper / "configs/gaussian.toml").write_bytes(config.read_bytes())
    commands = (
        ["git", "init", "-q"],
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "add",
            "configs/gaussian.toml",
        ],
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "synthetic test fixture",
        ],
    )
    for cmd in commands:
        subprocess.run(cmd, cwd=scraper, check=True, capture_output=True)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=scraper, text=True
    ).strip()
    spec = corpus.specification(scraper / "configs/gaussian.toml", sha)
    snapshot = corpus.ensure_snapshot(root, spec, build=write_database)
    assert snapshot.is_dir()
    destination = tmp_path / "private/gaussian.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "inkly.gaussian_docs",
            "ensure",
            "--shared-root",
            str(root),
            "--scraper-dir",
            str(scraper),
            "--destination",
            str(destination),
            "--timeout",
            "0",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert destination.is_file()
    assert json.loads(result.stdout)["passage_count"] == 1
    assert not (tmp_path / "private/jobs.db").exists()
    assert corpus.digest(destination) == corpus.digest(snapshot / "gaussian.db")
