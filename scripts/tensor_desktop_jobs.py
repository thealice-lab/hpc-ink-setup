"""Which recorded HPC jobs belong to which files on this computer.

Desktop remembers the input file of every handoff it prepares or checks, and
every folder files were added from, in %LOCALAPPDATA%\\Inkly\\desktop-jobs.json,
so Load my jobs and Job history can put a job back in the list without Add
files. A job not in that list is looked for in those folders, by the receipt
saved beside its input, and the researcher can search a folder for the rest.
Only a job whose files are not on this computer at all gets a copy of its
input, in a folder chosen once, so its result has somewhere to go. This module
never talks to the HPC.
"""

import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

import tensor_picker as picker

# Load adds active jobs and recent ended jobs, excluding cancelled handoffs.
RECENT_DAYS = 3
MAX_REMEMBERED = 5000
# Folders checked automatically for a job's receipt; newest kept.
MAX_FOLDERS = 200
# Bounds for Find files: how deep and how many folder entries are looked at.
SEARCH_DEPTH = 8
SEARCH_ENTRIES = 100_000
# Never searched: system and tool folders that hold no inputs.
SEARCH_SKIP = {
    "node_modules",
    "__pycache__",
    "venv",
    "appdata",
    "system volume information",
}
MAX_RECEIPT = 64 * 1024
MAX_ARCHIVES = 64
MAX_INPUT = 5 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:gjf|com)", re.I)
BUNDLE = re.compile(r"[0-9a-f]{32}")
# The window thread and the worker thread share the file.
_lock = threading.Lock()

FAILED = {
    "FAILED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
    "BOOT_FAIL",
    "DEADLINE",
}
STATE_FILTERS = {
    "Finished and cancelled": {"COMPLETED", "RETURNED", "CANCELLED"} | FAILED,
    "Completed": {"COMPLETED", "RETURNED"},
    "Failed, timed out or out of memory": FAILED,
    "Cancelled": {"CANCELLED"},
    "Pending or running": set(picker.ACTIVE) | {"SUBMITTED"},
    "Needs checking": {"UNKNOWN", "SUBMISSION_UNKNOWN"},
    "Not submitted": {"PREPARED"},
    "All": None,
}
PERIODS = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "All time": None,
}


def state_path():
    return picker.config_path().with_name("desktop-jobs.json")


def receipt_for(path):
    return path.with_suffix(path.suffix + ".inkly.json")


def is_copy(path, bundle):
    """Recognize the generated layout, including legacy lists without a root."""
    path = Path(path)
    return (
        bool(BUNDLE.fullmatch(str(bundle)))
        and path.parent.name == f"{path.stem}-{str(bundle)[:8]}"
    )


def _folders(data):
    copies = {}
    for bundle, entry in data["handoffs"].items():
        if isinstance(entry, dict) and isinstance(entry.get("input"), str):
            path = Path(entry["input"])
            if is_copy(path, bundle):
                copies[str(path.parent)] = str(path.parent.parent)
    seen = {}
    for folder, stamp in data["folders"].items():
        folder = copies.get(folder, folder)
        seen[folder] = max(str(stamp), seen.get(folder, ""))
    return seen


def search_summary(
    root, found, missing, replaced=0, originals=0, complete=True, copies=0
):
    text = f"Found {found} job(s) in {root}; {missing} still not found."
    if replaced:
        text += f"\n{replaced} older job(s) were left in Job history because their files have newer handoffs."
    if originals:
        text += f"\nSwitched {originals} job(s) from copies to originals."
    if copies:
        text += f"\nKept {copies} existing copy/copies where no original was found."
    if not complete:
        text += (
            "\nThe search reached its limit; other folders may not have been checked."
        )
    return text


def missing_text(missing, summary=None):
    """Describe job instances, keeping repeated names and dates understandable."""
    groups = defaultdict(list)
    for job in missing:
        groups[(job["name"], picker.state_name(job.get("state")))].append(job)
    lines = []
    for (name, state), values in sorted(groups.items()):
        dates = sorted(
            {
                local_time(job.get("submitted_at") or job.get("prepared_at"))
                for job in values
            }
            - {""}
        )
        date = (
            " to ".join([dates[0], dates[-1]])
            if len(dates) > 1
            else dates[0]
            if dates
            else "unavailable"
        )
        repeated = f" \u00d7{len(values)}" if len(values) > 1 else ""
        lines.append(f"{name}{repeated} — {state}; submitted {date}")
    if len(lines) > 8:
        lines = lines[:8] + [f"...and {len(groups) - 8} more file/state groups."]
    copies = sum(bool(job.get("local_copy")) for job in missing)
    intro = summary or (
        f"{len(missing)} job(s) still need original-file locations. {copies} already have local copies."
        if copies
        else f"{len(missing)} job(s) were not found in folders Inkly Desktop has used."
    )
    return intro + "\n\n" + "\n".join(lines)


class Index:
    """Bundle → input file on this computer, plus the folder for copies."""

    def __init__(self, path=None):
        self.path = path or state_path()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("handoffs"), dict):
            data = {"version": 1, "folder": None, "handoffs": {}}
        if not isinstance(data.get("folders"), dict):
            data["folders"] = {}
        return data

    def _update(self, change):
        with _lock:
            data = self._read()
            change(data)
            # Compact legacy copy subfolders without losing project folders.
            data["folders"] = _folders(data)
            handoffs = data["handoffs"]
            if len(handoffs) > MAX_REMEMBERED:
                newest = sorted(
                    handoffs,
                    key=lambda key: str(handoffs[key].get("seen")),
                    reverse=True,
                )
                data["handoffs"] = {
                    key: handoffs[key] for key in newest[:MAX_REMEMBERED]
                }
            folders = data["folders"]
            if len(folders) > MAX_FOLDERS:
                newest = sorted(
                    folders, key=lambda key: str(folders[key]), reverse=True
                )
                data["folders"] = {key: folders[key] for key in newest[:MAX_FOLDERS]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".desktop-jobs-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, indent=1)
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)

    def remember(self, path, bundle, original_checked=None):
        if not BUNDLE.fullmatch(str(bundle)):
            return
        resolved = Path(path).resolve()
        now = datetime.now(timezone.utc).isoformat()
        entry = {"input": str(resolved), "seen": now}

        def change(data):
            previous = data["handoffs"].get(bundle, {})
            if not isinstance(previous, dict):
                previous = {}
            if is_copy(resolved, bundle):
                if previous.get("replaced"):
                    entry["replaced"] = True
                entry["original_checked"] = (
                    original_checked
                    if original_checked is not None
                    else bool(
                        previous.get("original_checked")
                        and previous.get("input") == str(resolved)
                    )
                )
            data["handoffs"][bundle] = entry
            parent = (
                resolved.parent.parent if is_copy(resolved, bundle) else resolved.parent
            )
            data["folders"][str(parent)] = now

        self._update(change)

    def remember_folders(self, folders):
        """Folders files were added from; checked first when loading jobs."""
        now = datetime.now(timezone.utc).isoformat()
        values = {str(Path(folder).resolve()) for folder in folders}

        def change(data):
            for value in values:
                data["folders"][value] = now

        if values:
            self._update(change)

    def folders(self):
        """Remembered folders, newest first, including those of known inputs."""
        with _lock:
            data = self._read()
        seen = _folders(data)
        for bundle, entry in data["handoffs"].items():
            if isinstance(entry, dict) and isinstance(entry.get("input"), str):
                path = Path(entry["input"])
                parent = str(
                    path.parent.parent if is_copy(path, bundle) else path.parent
                )
                seen.setdefault(parent, str(entry.get("seen", "")))
        ordered = sorted(seen, key=lambda key: str(seen[key]), reverse=True)
        return [Path(folder) for folder in ordered[:MAX_FOLDERS]]

    def replaced(self, bundle, path=None):
        """Run again gave this file a new bundle; the old job stays in history."""

        def change(data):
            entry = data["handoffs"].get(bundle)
            if entry is None and path is not None:
                entry = data["handoffs"][bundle] = {
                    "input": str(path),
                    "seen": datetime.now(timezone.utc).isoformat(),
                }
            if isinstance(entry, dict):
                entry["replaced"] = True

        self._update(change)

    def folder(self):
        with _lock:
            folder = self._read().get("folder")
        return Path(folder) if isinstance(folder, str) and folder else None

    def set_folder(self, folder):
        value = str(Path(folder).resolve())
        self._update(lambda data: data.__setitem__("folder", value))

    def locate(self, bundle):
        """("found", path), ("replaced", path) or (None, None) for this bundle."""
        with _lock:
            entry = self._read()["handoffs"].get(bundle)
        if not isinstance(entry, dict) or not isinstance(entry.get("input"), str):
            return None, None
        path = Path(entry["input"])
        saved = _read_receipt(receipt_for(path))
        if not saved or path.is_symlink() or not path.is_file():
            return None, None
        if not isinstance(saved.get("bundle"), str) or not BUNDLE.fullmatch(
            saved["bundle"]
        ):
            return None, None
        if saved.get("bundle") != bundle or entry.get("replaced"):
            # The file now belongs to a newer job.
            return "replaced", path
        return "found", path

    def is_replaced(self, bundle):
        with _lock:
            entry = self._read()["handoffs"].get(bundle, {})
        return bool(isinstance(entry, dict) and entry.get("replaced"))

    def original_checked(self, bundle):
        with _lock:
            entry = self._read()["handoffs"].get(bundle, {})
        return bool(isinstance(entry, dict) and entry.get("original_checked"))

    def mapped(self, bundle):
        with _lock:
            entry = self._read()["handoffs"].get(bundle)
        return (
            Path(entry["input"])
            if isinstance(entry, dict) and isinstance(entry.get("input"), str)
            else None
        )


def moment(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def when(job):
    """When the job was submitted, or else when its handoff was prepared."""
    return moment(job.get("submitted_at")) or moment(job.get("prepared_at"))


def local_time(value):
    result = moment(value)
    return result.astimezone().strftime("%Y-%m-%d %H:%M") if result else ""


def load_set(jobs, now=None, days=RECENT_DAYS):
    """Active work and recent ended jobs; cancelled/unsubmitted stay in History."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    chosen = []
    for job in jobs:
        if job.get("submission") == "PREPARED":
            continue
        state = picker.state_name(job.get("state"))
        if state == "CANCELLED":
            continue  # Explicitly selectable in Job history, never default Load.
        latest = moment(job.get("ended_at")) or when(job)
        # Live jobs however old; anything else (including an uncertain
        # submission) only if it is recent, so old ones stay in Job history.
        if state in picker.ACTIVE or (latest and latest >= since):
            chosen.append(job)
    return chosen


def history_rows(jobs, state_filter="All", period="All time", text="", now=None):
    """Jobs matching the Job history filters, keeping newest-first order."""
    now = now or datetime.now(timezone.utc)
    states = STATE_FILTERS[state_filter]
    days = PERIODS[period]
    since = now - timedelta(days=days) if days else None
    text = text.strip().lower()
    rows = []
    for job in jobs:
        if states is not None and picker.state_name(job.get("state")) not in states:
            continue
        started = when(job)
        if since and (not started or started < since):
            continue
        if (
            text
            and text not in job.get("name", "").lower()
            and text != str(job.get("job_id", ""))
        ):
            continue
        rows.append(job)
    return rows


def copy_path(folder, job):
    """One folder per job, so two runs of the same file never collide."""
    name, bundle = job.get("name", ""), job.get("bundle", "")
    if not NAME.fullmatch(name) or not BUNDLE.fullmatch(bundle):
        raise ValueError("The HPC reported an unsafe input name")
    return Path(folder) / f"{Path(name).stem}-{bundle[:8]}" / name


def restore(folder, job, copy, connection, index):
    """Keep a copy of the job's input and receipt here; never overwrite a file.

    A copy made earlier is reused. Returns the input path to add to the list.
    """
    path = copy_path(folder, job)
    if copy.get("name") != job["name"] or copy.get("bundle") != job["bundle"]:
        raise ValueError("The HPC returned a different input")
    data = base64.b64decode(copy.get("data", ""), validate=True)
    if not data or len(data) > MAX_INPUT:
        raise ValueError("The HPC returned an input of an unexpected size")
    if hashlib.sha256(data).hexdigest() != copy.get("sha256"):
        raise ValueError("Input checksum mismatch; no copy was written")
    receipt_path = receipt_for(path)
    receipt = {
        "host": connection["host"],
        "ink_path": connection["ink_path"],
        "bundle": job["bundle"],
        "auth": connection["auth"],
        "output": str((path.parent / "returned").resolve()),
    }
    if receipt_path.exists():
        saved = json.loads(receipt_path.read_text(encoding="utf-8"))
        if saved.get("bundle") != job["bundle"]:
            raise ValueError(
                f"{receipt_path} belongs to a different job; nothing was changed"
            )
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(
                f"A different file already exists at {path}; nothing was changed"
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as out:
            out.write(data)
    if not receipt_path.exists():
        with receipt_path.open("x", encoding="utf-8") as out:
            os.chmod(receipt_path, 0o600)
            json.dump(receipt, out)
            out.flush()
            os.fsync(out.fileno())
    index.remember(path, job["bundle"], original_checked=True)
    return path


def _read_receipt(path):
    try:
        if path.is_symlink() or path.stat().st_size > MAX_RECEIPT:
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        return saved if isinstance(saved, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _connection_matches(saved, connection):
    # Legacy receipts may contain just a bundle. Present connection fields must
    # agree; a same-name file from another endpoint is never a replacement.
    return not connection or all(
        key not in saved or saved[key] == connection.get(key)
        for key in ("host", "ink_path", "auth")
    )


def _matches(receipt_path, job, connection=None):
    saved = _read_receipt(receipt_path)
    path = receipt_path.with_name(job["name"])
    if (
        not saved
        or saved.get("bundle") != job["bundle"]
        or not _connection_matches(saved, connection)
    ):
        return None
    return path if not path.is_symlink() and path.is_file() else None


def _searchable(job):
    return (
        isinstance(job.get("name"), str)
        and isinstance(job.get("bundle"), str)
        and NAME.fullmatch(job["name"])
        and BUNDLE.fullmatch(job["bundle"])
    )


def _archived_bundle(path, bundle, connection):
    try:
        with os.scandir(path.parent) as entries:
            archives = 0
            for number, entry in enumerate(entries):
                if number >= SEARCH_ENTRIES or archives >= MAX_ARCHIVES:
                    break
                if entry.name.startswith(
                    path.name + ".inkly.json."
                ) and entry.name.endswith(".bak"):
                    archives += 1
                    saved = _read_receipt(Path(entry.path))
                    if (
                        saved
                        and saved.get("bundle") == bundle
                        and _connection_matches(saved, connection)
                    ):
                        return True
    except OSError:
        pass
    return False


class _Discovery:
    def __init__(self, jobs, index, connection, all_jobs, include_replaced):
        self.jobs, self.index = jobs, index
        self.connection = connection
        self.known = {
            job["bundle"]: job for job in (all_jobs or jobs) if _searchable(job)
        }
        self.include_replaced = include_replaced
        self.searched = False
        self.wanted = defaultdict(list)
        self.best, self.replaced = {}, {}
        for job in jobs:
            if _searchable(job):
                self.wanted[f"{job['name']}.inkly.json".lower()].append(job)

    def consider(self, receipt_path):
        targets = self.wanted.get(receipt_path.name.lower(), ())
        if not targets:
            return
        saved = _read_receipt(receipt_path)
        if not saved or not _connection_matches(saved, self.connection):
            return
        if not isinstance(saved.get("bundle"), str) or not BUNDLE.fullmatch(
            saved["bundle"]
        ):
            return
        for job in targets:
            path = receipt_path.with_name(job["name"])
            if path.is_symlink() or not path.is_file():
                continue
            if saved.get("bundle") == job["bundle"]:
                previous = self.best.get(job["bundle"])

                def rank(p):
                    return is_copy(p, job["bundle"]), str(p).casefold()

                if previous is None or rank(path) < rank(previous):
                    self.best[job["bundle"]] = path
                continue
            newer = self.known.get(saved.get("bundle"))
            # Never suppress active/uncertain work or infer identity from a name.
            if self.include_replaced or picker.state_name(job.get("state")) not in (
                {"COMPLETED", "RETURNED", "CANCELLED"} | FAILED
            ):
                continue
            if (
                not newer
                or newer["name"] != job["name"]
                or is_copy(path, newer["bundle"])
            ):
                continue
            old_time, new_time = when(job), when(newer)
            if not old_time or not new_time or new_time <= old_time:
                continue
            mapped = self.index.mapped(job["bundle"])
            if mapped == path or _archived_bundle(path, job["bundle"], self.connection):
                self.replaced[job["bundle"]] = path

    def finish(self, report):
        found, missing, moves = [], [], []
        found_count = copies = 0
        for job in self.jobs:
            if not _searchable(job):
                missing.append(job)
                continue
            bundle = job.get("bundle")
            if self.searched and bundle not in self.best and job.get("local_copy"):
                local = Path(job["local_copy"])
                if is_copy(local, bundle) and _matches(
                    receipt_for(local), job, self.connection
                ):
                    self.best[bundle] = local
                    if bundle not in self.replaced:
                        copies += 1
            elif bundle in self.best and bundle not in self.replaced:
                found_count += 1
            if bundle in self.replaced:
                self.index.replaced(bundle, self.replaced[bundle])
            elif bundle in self.best:
                path = self.best[bundle]
                previous = self.index.mapped(bundle)
                # Finding only a copy cannot displace a still-valid original.
                if (
                    previous
                    and not is_copy(previous, bundle)
                    and is_copy(path, bundle)
                    and _matches(receipt_for(previous), job, self.connection)
                ):
                    path = previous
                if previous and previous.resolve() != path.resolve():
                    moves.append(
                        {
                            "bundle": bundle,
                            "old": str(previous),
                            "new": str(path),
                            "original": is_copy(previous, bundle)
                            and not is_copy(path, bundle),
                        }
                    )
                self.index.remember(
                    path,
                    bundle,
                    original_checked=True
                    if self.searched and is_copy(path, bundle)
                    else None,
                )
                found.append(path)
            else:
                missing.append(job)
        if report is not None:
            report.update(
                replaced=len(self.replaced),
                moves=moves,
                found_count=found_count,
                copies=copies,
            )
        return list(dict.fromkeys(found)), missing


def find_in_folders(
    jobs,
    folders,
    index,
    *,
    report=None,
    connection=None,
    all_jobs=None,
    include_replaced=False,
):
    """Check exact sidecars in remembered folders; prefer originals to copies."""
    discovery = _Discovery(jobs, index, connection, all_jobs, include_replaced)
    for folder in dict.fromkeys(map(Path, folders)):
        for name in discovery.wanted:
            # Use the original case on case-sensitive filesystems.
            for job in discovery.wanted[name]:
                discovery.consider(folder / f"{job['name']}.inkly.json")
    return discovery.finish(report)


def search(
    root,
    jobs,
    index,
    check=lambda: None,
    *,
    report=None,
    connection=None,
    all_jobs=None,
    include_replaced=False,
):
    """Bounded receipt search; scan past copies so originals can win."""
    discovery = _Discovery(jobs, index, connection, all_jobs, include_replaced)
    discovery.searched = True
    entries, complete = 0, True
    pending = [(Path(root), 0)]
    while pending:
        folder, depth = pending.pop()
        check()
        if folder.is_symlink():
            continue
        try:
            with os.scandir(folder) as listing:
                for entry in listing:
                    entries += 1
                    if entries > SEARCH_ENTRIES:
                        found, missing = discovery.finish(report)
                        return found, missing, False
                    if entries % 64 == 0:
                        check()
                    if entry.is_dir(follow_symlinks=False):
                        if (
                            entry.name.startswith((".", "$"))
                            or entry.name.lower() in SEARCH_SKIP
                        ):
                            continue
                        if depth >= SEARCH_DEPTH:
                            complete = False
                            continue
                        pending.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        discovery.consider(Path(entry.path))
        except OSError:
            continue
    found, missing = discovery.finish(report)
    return found, missing, complete
