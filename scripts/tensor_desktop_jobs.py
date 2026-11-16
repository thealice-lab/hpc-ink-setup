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
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

import tensor_picker as picker

# Load my jobs adds active jobs and jobs that ended within this many days.
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

    def remember(self, path, bundle):
        if not BUNDLE.fullmatch(str(bundle)):
            return
        resolved = Path(path).resolve()
        now = datetime.now(timezone.utc).isoformat()
        entry = {"input": str(resolved), "seen": now}

        def change(data):
            data["handoffs"][bundle] = entry
            data["folders"][str(resolved.parent)] = now

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
        seen = dict(data["folders"])
        for entry in data["handoffs"].values():
            if isinstance(entry, dict) and isinstance(entry.get("input"), str):
                parent = str(Path(entry["input"]).parent)
                seen.setdefault(parent, str(entry.get("seen", "")))
        ordered = sorted(seen, key=lambda key: str(seen[key]), reverse=True)
        return [Path(folder) for folder in ordered[:MAX_FOLDERS]]

    def replaced(self, bundle):
        """Run again gave this file a new bundle; the old job stays in history."""

        def change(data):
            entry = data["handoffs"].get(bundle)
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
        try:
            saved = json.loads(receipt_for(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None, None  # Moved or deleted: not on this computer any more.
        if not isinstance(saved, dict) or not path.is_file():
            return None, None
        if saved.get("bundle") != bundle or entry.get("replaced"):
            # The file now belongs to a newer job.
            return "replaced", path
        return "found", path


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
    """Active jobs and jobs that ended within `days`; never unsubmitted ones."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    chosen = []
    for job in jobs:
        if job.get("submission") == "PREPARED":
            continue
        state = picker.state_name(job.get("state"))
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
    index.remember(path, job["bundle"])
    return path


def _matches(receipt_path, job):
    """The input beside this receipt, if the receipt records this job."""
    try:
        if receipt_path.stat().st_size > MAX_RECEIPT:
            return None
        saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    path = receipt_path.with_name(job["name"])
    if not isinstance(saved, dict) or saved.get("bundle") != job["bundle"]:
        return None
    return path if path.is_file() else None


def _searchable(job):
    return NAME.fullmatch(job.get("name", "")) and BUNDLE.fullmatch(
        job.get("bundle", "")
    )


def find_in_folders(jobs, folders, index):
    """Split jobs into inputs found in these folders and jobs still missing.

    Only `<input>.inkly.json` beside each input is read, so this is quick.
    Found inputs are remembered for next time.
    """
    found, missing = [], []
    for job in jobs:
        match = None
        if _searchable(job):
            for folder in folders:
                match = _matches(Path(folder) / f"{job['name']}.inkly.json", job)
                if match:
                    break
        if match:
            index.remember(match, job["bundle"])
            found.append(match)
        else:
            missing.append(job)
    return found, missing


def search(root, jobs, index, check=lambda: None):
    """Look through `root` and its subfolders for these jobs' receipts.

    Bounded by SEARCH_DEPTH and SEARCH_ENTRIES; never follows links or enters
    hidden, system or tool folders, and reads only receipts named after a
    wanted input. Returns (found paths, jobs still missing, complete), where
    complete is False if a bound stopped the search early.
    """
    wanted = {}
    for job in jobs:
        if _searchable(job):
            wanted.setdefault(f"{job['name']}.inkly.json".lower(), []).append(job)
    found, matched = [], set()
    entries, complete = 0, True
    pending = [(Path(root), 0)]
    while pending and len(matched) < len(jobs):
        folder, depth = pending.pop()
        check()
        try:
            with os.scandir(folder) as listing:
                items = list(listing)
        except OSError:
            continue
        for entry in items:
            entries += 1
            if entries > SEARCH_ENTRIES:
                missing = [job for job in jobs if job["bundle"] not in matched]
                return found, missing, False
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.startswith((".", "$")):
                        continue
                    if entry.name.lower() in SEARCH_SKIP:
                        continue
                    if depth >= SEARCH_DEPTH:
                        complete = False
                        continue
                    pending.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            for job in wanted.get(entry.name.lower(), []):
                if job["bundle"] in matched:
                    continue
                path = _matches(Path(entry.path), job)
                if path:
                    matched.add(job["bundle"])
                    index.remember(path, job["bundle"])
                    found.append(path)
    return found, [job for job in jobs if job["bundle"] not in matched], complete
