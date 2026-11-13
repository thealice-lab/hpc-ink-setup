"""Verified Gaussian corpus snapshots; never shares the personal jobs database."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

try:
    import tomllib
except ImportError:
    import tomli as tomllib


class CorpusError(ValueError):
    """The corpus is unavailable or unsafe to use."""


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise CorpusError("Corpus artifacts must be regular files, not links.")


def _read_json(path: Path) -> dict:
    _regular(path)
    if path.stat().st_size > 2_000_000:
        raise CorpusError("Corpus metadata exceeds the supported size.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CorpusError("Corpus metadata must be an object.")
    return value


def specification(config: Path, revision: str, exceptions: Path | None = None) -> dict:
    """Bind a source inventory and reviewed omissions to exact config bytes."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise CorpusError("A full scraper Git commit is required.")
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    if raw.get("name") != "gaussian" or not raw.get("keywords"):
        raise CorpusError("Use a validated Gaussian scraper configuration.")
    sources = {}
    for key, kind, fields in (
        ("html_sources", "html", ("url",)),
        ("se_sources", "se", ("site", "tag")),
    ):
        for source in raw.get(key, []):
            label = source.get("label")
            if not isinstance(label, str) or not label.strip() or label in sources:
                raise CorpusError(
                    "Configured source labels must be unique and nonempty."
                )
            if any(not isinstance(source.get(f), str) or not source[f] for f in fields):
                raise CorpusError("Configured source identity is incomplete.")
            sources[label] = {
                "source_type": kind,
                **{f: source.get(f) for f in ("url", "site", "tag", "tool")},
            }
    if not sources:
        raise CorpusError("No Gaussian documentation sources are configured.")
    config_hash = digest(config)
    missing = {}
    if exceptions:
        policy = _read_json(exceptions)
        missing = policy.get("allowed_missing")
        if policy.get("config_sha256") != config_hash or not isinstance(missing, dict):
            raise CorpusError("Reviewed omissions must match this configuration hash.")
        if any(
            label not in sources or not isinstance(reason, str) or not reason.strip()
            for label, reason in missing.items()
        ):
            raise CorpusError(
                "Every reviewed omission needs a configured label and reason."
            )
    return {
        "config_sha256": config_hash,
        "scraper_revision": revision,
        "sources": sources,
        "allowed_missing": missing,
    }


def inspect_database(path: Path, spec: dict) -> dict:
    """Read-only structural/content checks against the scraper's real schema."""
    _regular(path)
    if any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise CorpusError("A database with live SQLite sidecars cannot be published.")
    before = digest(path)
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise CorpusError("Gaussian database integrity check failed.")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise CorpusError("Gaussian database contains broken source references.")
        rows = db.execute(
            "SELECT id,label,source_type,url,site,tag,tool,content_hash FROM sources"
        ).fetchall()
        inventory = {}
        total = 0
        for source_id, label, kind, url, site, tag, tool, content_hash in rows:
            identity = dict(source_type=kind, url=url, site=site, tag=tag, tool=tool)
            if label in inventory or spec["sources"].get(label) != identity:
                raise CorpusError(
                    "Stored source identity does not match the configuration."
                )
            passages = db.execute(
                "SELECT position,text FROM passages WHERE source_id=? ORDER BY position",
                (source_id,),
            ).fetchall()
            if not passages or any(
                position != i or not isinstance(text, str) or not text.strip()
                for i, (position, text) in enumerate(passages)
            ):
                raise CorpusError(
                    "Every stored source needs nonempty ordered passages."
                )
            actual_hash = hashlib.sha256(
                "\n".join(sorted(text for _, text in passages)).encode("utf-8")
            ).hexdigest()
            if actual_hash != content_hash:
                raise CorpusError("Stored passage content checksum failed.")
            inventory[label] = {
                "passages": len(passages),
                "content_sha256": actual_hash,
            }
            total += len(passages)
        if (
            not inventory
            or db.execute("SELECT count(*) FROM passages").fetchone()[0] != total
        ):
            raise CorpusError("Gaussian database is empty or has orphan passages.")
    missing = set(spec["sources"]) - set(inventory)
    if missing - set(spec["allowed_missing"]):
        raise CorpusError(
            "Configured sources are missing; review failures and supply config-bound omissions."
        )
    if before != digest(path):
        raise CorpusError("Gaussian database changed during validation.")
    return {
        "database_sha256": before,
        "source_count": len(inventory),
        "passage_count": total,
        "stored_sources": inventory,
        "missing_sources": {
            label: spec["allowed_missing"][label] for label in sorted(missing)
        },
    }


def validate_snapshot(snapshot: Path, spec: dict, max_age: float = 0) -> dict:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise CorpusError("Snapshot directory is missing or unsafe.")
    manifest = _read_json(snapshot / "manifest.json")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("config_sha256") != spec["config_sha256"]
        or manifest.get("scraper_revision") != spec["scraper_revision"]
        or manifest.get("configured_sources") != spec["sources"]
    ):
        raise CorpusError("Snapshot version or source configuration does not match.")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("Snapshot timestamp must include a timezone")
        age = (datetime.now(timezone.utc) - created).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise CorpusError("Snapshot timestamp is invalid.") from None
    if age < -300 or (max_age and age > max_age):
        raise CorpusError("Snapshot has expired or has a future timestamp.")
    actual = inspect_database(snapshot / "gaussian.db", spec)
    if any(manifest.get(key) != value for key, value in actual.items()):
        raise CorpusError("Snapshot completeness manifest does not match the database.")
    return manifest


def _root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise CorpusError(
            "Shared corpus root must already be provisioned by the administrator."
        )
    if os.name == "posix" and root.stat().st_mode & stat.S_IWOTH:
        raise CorpusError("Shared corpus root must not be writable by everyone.")


def current_snapshot(root: Path, spec: dict, max_age: float = 0) -> tuple[Path, dict]:
    _root(root)
    pointer = _read_json(root / "current.json").get("snapshot")
    if not isinstance(pointer, str) or not re.fullmatch(
        r"snapshot-[0-9a-f]{32}", pointer
    ):
        raise CorpusError("Shared corpus pointer is invalid.")
    snapshot = root / pointer
    return snapshot, validate_snapshot(snapshot, spec, max_age)


@contextmanager
def _try_lock(root: Path):
    """Never unlink/steal the stable lock inode; process death releases the lock."""
    path = root / ".build.lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o660)
        os.chmod(path, 0o660)
    except FileExistsError:
        fd = os.open(path, flags)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or path.is_symlink():
            raise CorpusError("Build lock is not a regular file.")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, PermissionError):
            pass
        yield fd if acquired else None
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_snapshot(root: Path, spec: dict, build, lock_fd: int) -> Path:
    """Build with the existing scraper callback while holding the elected lock."""
    work = Path(tempfile.mkdtemp(prefix=".building-", dir=root))
    try:
        stage = work / "snapshot"
        stage.mkdir(mode=0o750)
        database = stage / "gaussian.db"
        database.touch(mode=0o600)
        # Raw scraper diagnostics stay private and are removed after normal exit.
        with (work / "build.log").open("x", encoding="utf-8") as log:
            os.chmod(work / "build.log", 0o600)
            with redirect_stdout(log), redirect_stderr(log):
                build(database)
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config_sha256": spec["config_sha256"],
            "scraper_revision": spec["scraper_revision"],
            "configured_sources": spec["sources"],
            **inspect_database(database, spec),
        }
        _atomic_json(stage / "manifest.json", manifest, 0o440)
        os.chmod(database, 0o440)
        validate_snapshot(stage, spec)
        # A scratch purge must not let a detached lock publish over a new builder.
        if not os.path.samestat(os.fstat(lock_fd), (root / ".build.lock").stat()):
            raise CorpusError(
                "The build lock was removed during the build; publication refused."
            )
        published = root / ("snapshot-" + uuid4().hex)
        stage.rename(published)
        _atomic_json(root / "current.json", {"snapshot": published.name}, 0o640)
        return published
    finally:
        # Don't leave large failed/aborted *normal* build trees on shared scratch.
        # A process killed without cleanup still leaves a private 0700 directory.
        shutil.rmtree(work)


def ensure_snapshot(
    root: Path, spec: dict, *, build=None, timeout=120, poll=1, max_age=0
):
    """Read/wait by default. A supplied approved builder elects exactly one writer."""
    if timeout < 0 or poll <= 0 or max_age < 0:
        raise CorpusError("Timeout/age must be nonnegative and poll interval positive.")
    _root(root)
    deadline = time.monotonic() + timeout
    while True:
        try:
            return current_snapshot(root, spec, max_age)[0]
        except (OSError, ValueError, sqlite3.DatabaseError):
            pass
        if build is not None:
            with _try_lock(root) as lock_fd:
                if lock_fd is not None:
                    try:
                        return current_snapshot(root, spec, max_age)[0]
                    except (OSError, ValueError, sqlite3.DatabaseError):
                        return build_snapshot(root, spec, build, lock_fd)
        if time.monotonic() >= deadline:
            raise CorpusError(
                "No valid shared Gaussian corpus became available before timeout. "
                "Ask the maintainer to run the approved rebuild; no database was copied."
            )
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


def copy_private(snapshot: Path, destination: Path, spec: dict, max_age=0) -> dict:
    """Validate a private staged copy and publish exclusively, preserving old data."""
    manifest = validate_snapshot(snapshot, spec, max_age)
    if destination.name != "gaussian.db":
        raise CorpusError(
            "Private destination must be named gaussian.db, never jobs.db."
        )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise CorpusError("Private database parent must not be a symlink.")
    if destination.exists() or destination.is_symlink():
        _regular(destination)
        if digest(destination) == manifest["database_sha256"]:
            inspect_database(destination, spec)
            return manifest
        raise CorpusError(
            "Existing personal gaussian.db was preserved. Review/back it up before migration."
        )
    with tempfile.TemporaryDirectory(
        prefix=".corpus-copy-", dir=destination.parent
    ) as tmp:
        staged = Path(tmp)
        shutil.copyfile(snapshot / "gaussian.db", staged / "gaussian.db")
        os.chmod(staged / "gaussian.db", 0o600)
        _atomic_json(staged / "manifest.json", manifest, 0o600)
        validate_snapshot(staged, spec, max_age)
        # Atomic no-replace publication; concurrent installs cannot clobber a DB.
        try:
            os.link(staged / "gaussian.db", destination)
        except FileExistsError:
            _regular(destination)
            if digest(destination) != manifest["database_sha256"]:
                raise CorpusError(
                    "A different personal database appeared; it was preserved."
                ) from None
    return manifest


def scraper_revision(directory: Path) -> str:
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(directory), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    if git("status", "--porcelain", "--untracked-files=no"):
        raise CorpusError(
            "Use a clean scraper checkout so its revision identifies the code."
        )
    return git("rev-parse", "HEAD")


def scraper_builder(directory: Path, config: Path, spec: dict):
    def build(destination):
        # Preserve Nathan's scraper pipeline; redirect its output path, skip optional LLM work.
        import gaussian_scraper
        from gaussian_scraper.config import load_toml_config

        package = Path(gaussian_scraper.__file__).resolve().parent
        if package != (directory / "gaussian_scraper").resolve():
            raise CorpusError(
                "Installed scraper does not match the selected source checkout."
            )
        if (
            digest(config) != spec["config_sha256"]
            or scraper_revision(directory) != spec["scraper_revision"]
        ):
            raise CorpusError(
                "Scraper configuration or revision changed before the build."
            )
        loaded = load_toml_config(config)
        loaded.output_path = destination
        pipeline = runpy.run_path(str(directory / "scrape.py"))
        pipeline["run_scrape"](loaded, skip_summary=True)
        if (
            digest(config) != spec["config_sha256"]
            or scraper_revision(directory) != spec["scraper_revision"]
        ):
            raise CorpusError(
                "Scraper configuration or revision changed during the build."
            )

    return build


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "validate"))
    parser.add_argument("--shared-root", required=True, type=Path)
    parser.add_argument("--scraper-dir", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--exceptions", type=Path, help="Reviewed config-bound missing sources JSON"
    )
    parser.add_argument(
        "--destination", type=Path, default=Path.home() / ".inkly/gaussian.db"
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-age-days", type=float, default=0)
    parser.add_argument(
        "--allow-build",
        action="store_true",
        help="Only in an approved build allocation",
    )
    args = parser.parse_args(argv)
    try:
        config = args.config or args.scraper_dir / "configs/gaussian.toml"
        spec = specification(
            config, scraper_revision(args.scraper_dir), args.exceptions
        )
        max_age = args.max_age_days * 86400
        if args.action == "validate":
            _, manifest = current_snapshot(args.shared_root, spec, max_age)
        else:
            snapshot = ensure_snapshot(
                args.shared_root,
                spec,
                build=scraper_builder(args.scraper_dir, config, spec)
                if args.allow_build
                else None,
                timeout=args.timeout,
                max_age=max_age,
            )
            manifest = copy_private(snapshot, args.destination, spec, max_age)
        print(
            json.dumps(
                {
                    key: manifest[key]
                    for key in (
                        "schema_version",
                        "source_count",
                        "passage_count",
                        "database_sha256",
                        "config_sha256",
                        "scraper_revision",
                        "missing_sources",
                    )
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        OSError,
        ValueError,
        sqlite3.DatabaseError,
        subprocess.SubprocessError,
        ImportError,
    ) as exc:
        print(f"Gaussian documentation setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
