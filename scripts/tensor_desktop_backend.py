"""GUI-independent controller. All methods except stop run on one worker thread."""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
from pathlib import Path
import queue
import shutil
import threading
import uuid

import tensor_bridge as bridge
import tensor_desktop_jobs as jobs
import tensor_picker as picker
from inkly.core.desktop_diagnostics import DesktopJournal


class DesktopController:
    def __init__(self, emit, askpass=None, interval=30, journal=None, index=None):
        self.emit = emit
        self.askpass = askpass
        self.interval = interval
        self.cancelled = threading.Event()
        self.stack = None
        self.session = None
        self.target = None
        self.items = []
        self.review_id = None
        self.journal = journal if journal is not None else DesktopJournal()
        # Which input file on this computer each recorded bundle belongs to.
        self.index = index if index is not None else jobs.Index()
        self.connection_verified = False
        # Jobs cancelled through Inkly in this window; monitoring then skips
        # repeating the cancellation message.
        self.cancelled_paths = set()
        # Operations requested while monitoring are run by the monitor loop on
        # its own SSH session; `wake` ends its wait between checks early.
        self.requests = queue.Queue()
        self.wake = threading.Event()
        # Stop watching: end the monitor loop but keep the SSH session open.
        self.paused = threading.Event()

    def remember(self, item):
        try:
            self.index.remember(item["path"], item["receipt"]["bundle"])
        except Exception:
            pass  # The list of files is a convenience; it never stops a handoff.

    def audit(self, event_type, payload=None):
        try:
            self.journal.emit(event_type, payload)
        except Exception:
            pass  # Diagnostics can never fail a scientific workflow operation.

    def close(self):
        stack, self.stack = self.stack, None
        self.session = None
        self.target = None
        if stack:
            self.audit("desktop_connection", {"status": "closed"})
            stack.close()

    def stop(self):
        self.audit("desktop_stop")
        self.cancelled.set()
        self.wake.set()
        self.drop_requests()
        # Closing SSH never issues scancel. Persisted receipts recover uncertain calls.
        process = getattr(self.session, "process", None)
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass

    def pause(self):
        """Stop watching without disconnecting. Never cancels a Slurm job.

        The monitor loop ends after the check in progress, and Check & return
        results can resume on the same connection without a new sign-in.
        """
        self.audit("desktop_stop")
        self.paused.set()
        self.wake.set()
        self.drop_requests()

    def unhandled_requests(self):
        """A request made just as the watch finished: never run it later."""
        if not self.requests.empty():
            self.drop_requests()
            self.emit(
                "notice",
                "The watch finished before your last request ran, so nothing was "
                "done. Please choose it again.",
            )

    def drop_requests(self):
        """Forget cancel requests the watch has not handled; rows show what happened."""
        while True:
            try:
                self.requests.get_nowait()
            except queue.Empty:
                return

    def check_stop(self):
        if self.cancelled.is_set():
            raise InterruptedError(
                "Stopped watching. Any submitted HPC jobs continue; preview the same files to recover."
            )

    def connect(self, connection):
        target = tuple(connection.get(key) for key in ("host", "ink_path", "auth"))
        if self.session and target == self.target:
            return
        self.close()
        self.audit("desktop_connection", {"status": "attempt"})
        self.connection_verified = False
        ssh = shutil.which("ssh")
        if not ssh:
            raise ValueError(
                "Windows OpenSSH Client is missing. Install it through your institution's software setup."
            )
        self.stack = ExitStack()
        self.emit(
            "message",
            "Connecting. Complete the separate SSH sign-in dialog if prompted.",
        )
        try:
            self.stack.enter_context(picker.authentication(connection, self.askpass))
            self.session = self.stack.enter_context(bridge.Session(connection, ssh))
            self.session.diagnostics = self.journal
            self.target = target
        except Exception:
            self.audit("desktop_connection", {"status": "failed"})
            self.close()
            raise

    def row(self, item, state=None, output=None):
        response = item["response"]
        if not self.connection_verified:
            self.audit("desktop_connection", {"status": "connected"})
            self.connection_verified = True
        current = state or response.get("state", response["submission"])
        if current != item.get("_logged_state"):
            self.audit(
                "desktop_job_state",
                {"state": picker.state_name(current), **self.job_metadata(response)},
            )
            item["_logged_state"] = current
        self.emit(
            "row",
            {
                "path": str(item["path"]),
                "name": response["name"],
                "state": state or response.get("state", response["submission"]),
                "job_id": response.get("job_id", ""),
                "reason": response.get("reason"),
                "start_time": response.get("start_time"),
                "elapsed": response.get("elapsed"),
                "exit_code": response.get("exit_code"),
                "observed_at": response.get("observed_at"),
                "resources": deepcopy(response["resources"]),
                "stages": response["stages"],
                "script": response.get("script", ""),
                "output": str(output) if output else "",
                # The file was edited after this bundle was prepared.
                "input_changed": bool(item.get("changed")),
            },
        )

    @staticmethod
    def job_metadata(response):
        job_id = response.get("job_id")
        if (
            isinstance(job_id, str)
            and job_id.isascii()
            and job_id.isdigit()
            and len(job_id) <= 10
        ):
            return {"job_id": int(job_id)}
        return {}

    # Operations Test connection checks for. Optional ones only switch off a
    # feature; Desktop still asks the endpoint before using each of them.
    REQUIRED_OPERATIONS = ("prepare", "submit", "status", "fetch")
    OPTIONAL_OPERATIONS = {
        "cancel": "Cancel job",
        "job_details": "Job details",
        "jobs": "Load my jobs and Job history",
    }

    def test_connection(self, settings):
        """Check only the configured SSH endpoint handshake; no bundle or Slurm work."""
        settings = picker.validate_settings(dict(settings))
        self.connect(settings)
        receipt = {**settings, "bundle": uuid.uuid4().hex}
        try:
            response = self.session.exchange({"action": "version"}, receipt=receipt)
        except ValueError as error:
            if str(error) != "Unsupported operation":
                raise
            self.emit(
                "notice",
                "SSH connected, but this HPC endpoint cannot report its version. Ask the maintainer for a matching Desktop endpoint.",
            )
            return
        version = response.get("version")
        capabilities = response.get("capabilities")
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 80
            or not all(c.isalnum() or c in ".+-" for c in version)
            or not isinstance(capabilities, list)
        ):
            raise ValueError("The HPC endpoint returned an invalid version handshake.")
        from inkly import __version__

        # What matters is whether the HPC copy offers each operation this
        # Desktop uses; release numbers may differ between compatible copies.
        offered = {value for value in capabilities if isinstance(value, str)}
        missing = [op for op in self.REQUIRED_OPERATIONS if op not in offered]
        if missing:
            message = (
                f"Connected, but the HPC copy of Inkly ({version}) lacks operations "
                f"this Desktop ({__version__}) needs: {', '.join(missing)}. Ask the "
                "maintainer to update the HPC copy of Inkly before submitting jobs."
            )
        else:
            message = (
                f"Connected. The HPC copy of Inkly ({version}) supports what this "
                f"Desktop ({__version__}) needs to preview, submit and return jobs."
            )
            absent = [
                name
                for op, name in self.OPTIONAL_OPERATIONS.items()
                if op not in offered
            ]
            if absent:
                message += (
                    " Not available until the HPC copy of Inkly is updated: "
                    + ", ".join(absent)
                    + "."
                )
        message += " No input was prepared and no Slurm command was run."
        self.emit("notice", message)

    def preview(self, paths, settings, recover_bundles=None):
        self.check_stop()
        self.review_id = None
        settings = picker.validate_settings(dict(settings))
        paths = picker.selections(paths)
        if not paths:
            raise ValueError("Add at least one Gaussian input file first.")
        self.audit("desktop_preview", {"count": len(paths)})
        self.items = picker.plan(paths, settings, "preview")
        connection = self.items[0]["receipt"] or settings
        self.connect(connection)
        self.emit(
            "target", {key: connection.get(key) for key in ("host", "ink_path", "auth")}
        )
        approved_bundles = set(recover_bundles or ())
        if approved_bundles:
            self.audit(
                "desktop_recovery",
                {"status": "approved", "count": len(approved_bundles)},
            )
        missing = []
        for item in self.items:
            self.check_stop()
            try:
                picker.prepare(
                    self.session,
                    item,
                    settings,
                    report=lambda value: self.emit("message", value),
                    recover_bundles=approved_bundles,
                )
            except bridge.MissingBundleError:
                missing.append(item)
                continue
            except picker.InputChangedError as error:
                # Show the recorded job; the edited file is never submitted
                # under the old bundle. Run again prepares it as a new job.
                item["response"] = error.response
                item["changed"] = True
                self.remember(item)
                self.row(item)
                self.emit(
                    "message",
                    f"{item['path'].name} was edited after it was prepared. Its recorded "
                    "job is unchanged and will not be submitted with the edits. If that "
                    "job was cancelled, failed or never submitted, select the row and "
                    "choose Run again to prepare the edited file as a new job.",
                )
                continue
            resources = item["response"]["resources"]
            self.audit(
                "desktop_prepared",
                {
                    "cpus": resources["cpus"],
                    "memory_mib": resources["slurm_memory_mib"],
                    "hours": resources["hours"],
                    "stages": item["response"]["stages"],
                },
            )
            if item.get("recovered"):
                self.audit("desktop_recovery", {"status": "prepared", "count": 1})
            self.remember(item)
            self.row(item)
        if missing:
            self.audit(
                "desktop_recovery", {"status": "required", "count": len(missing)}
            )
            self.items = [item for item in self.items if item not in missing]
            self.emit(
                "recovery_required",
                {
                    "paths": paths,
                    "settings": settings,
                    "bundles": [item["receipt"]["bundle"] for item in missing],
                    "files": [item["path"].name for item in missing],
                },
            )
            return
        self.review_id = uuid.uuid4().hex
        self.emit("review", self.review())
        self.emit(
            "message",
            "Preview complete. No jobs were submitted. Review the resources before choosing Submit prepared jobs.",
        )

    @staticmethod
    def ready(item):
        """An unchanged, never-submitted bundle awaiting approval."""
        return item["response"]["submission"] == "PREPARED" and not item.get("changed")

    def review(self):
        ready = [item for item in self.items if self.ready(item)]
        return {
            "id": self.review_id,
            "count": len(ready),
            "cpus": sum(item["response"]["resources"]["cpus"] for item in ready),
            "memory_mib": sum(
                item["response"]["resources"]["slurm_memory_mib"] for item in ready
            ),
            "jobs": [
                {"name": item["path"].name, **item["response"]["resources"]}
                for item in ready
            ],
            "recovered": [item["path"].name for item in ready if item.get("recovered")],
        }

    def refresh_before_submit(self):
        # Review every file before any submit, including jobs in a mixed selection.
        for item in self.items:
            self.check_stop()
            if item.get("changed"):
                continue  # Shown for information only; never part of a submit.
            previous = item["response"]
            response = self.session.exchange(
                {"action": "status"}, receipt=item["receipt"]
            )
            digest = hashlib.sha256(item["path"].read_bytes()).hexdigest()
            if digest != response["sha256"] or response["sha256"] != previous["sha256"]:
                raise ValueError(
                    "An input changed after preview. No new submission was attempted; review the saved handoff."
                )
            if response["resources"] != previous["resources"] or response.get(
                "script_sha256"
            ) != previous.get("script_sha256"):
                raise ValueError(
                    "Prepared settings changed after review. Preview again before submitting."
                )
            if response["submission"] == "PREPARED":
                output = item["output"] / item["path"].with_suffix(".log").name
                if output.exists():
                    raise ValueError(
                        f"A result already exists and will not be overwritten: {output}"
                    )
            elif picker.state_name(response.get("state")) not in (
                picker.ACTIVE | self.ENDED
            ):
                # Only a job that may or may not have run stops new submissions.
                # Jobs that are running or have definitely ended (completed,
                # failed, timed out, cancelled...) do not.
                raise ValueError(
                    f"{item['path'].name} has an unresolved job state. Inspect it before submitting more jobs."
                )
            item["response"] = response
            self.row(item)

    # Slurm states that mean a job has definitely stopped, with or without a
    # result. A row in one of these never blocks submitting other files.
    ENDED = (picker.RERUNNABLE - {"PREPARED"}) | {"COMPLETED", "RETURNED"}

    def submit(self, review_id, confirmed=False, watch=True):
        if not confirmed or not self.review_id or review_id != self.review_id:
            raise ValueError("Submission needs confirmation of the current preview.")
        self.review_id = None  # Approval is consumed even if an SSH response is lost.
        self.audit(
            "desktop_approval",
            {"count": sum(self.ready(item) for item in self.items)},
        )
        self.emit("review", None)
        self.refresh_before_submit()
        for item in self.items:
            self.check_stop()
            if self.ready(item):
                self.audit("desktop_submission", {"status": "attempt"})
                response = self.session.exchange(
                    {"action": "submit", "confirm": True}, receipt=item["receipt"]
                )
                item["response"].update(response)
                self.audit(
                    "desktop_submission",
                    {"status": "confirmed", **self.job_metadata(response)},
                )
                self.row(item, state="SUBMITTED")
                self.emit(
                    "message",
                    f"Submitted {item['path'].name}: job {response['job_id']}",
                )
        if watch:
            self.monitor()

    # Operations the window may ask for while monitoring.
    WATCH_REQUESTS = (
        "cancel_jobs",
        "preview",
        "submit",
        "rerun",
        "load_jobs",
        "restore_jobs",
        "history",
        "search_jobs",
    )

    def request(self, action, *args):
        """Called from the window thread while monitoring: run this operation next.

        The monitor loop runs it between checks on its open SSH session, so no
        new sign-in is needed, then keeps watching every submitted job.
        """
        if action not in self.WATCH_REQUESTS and action != "job_details":
            raise ValueError("That operation cannot run while watching.")
        self.requests.put((action, args))
        self.wake.set()

    def request_cancel(self, selected, settings):
        self.request("cancel_jobs", list(selected), dict(settings))

    def target_for(self, paths, settings, mode):
        """The connection a list of files would use, without contacting the HPC."""
        settings = picker.validate_settings(dict(settings))
        items = picker.plan(picker.selections(paths), settings, mode)
        connection = items[0]["receipt"] or settings
        return tuple(connection.get(key) for key in ("host", "ink_path", "auth"))

    def handle_requests(self):
        """Run window requests made while monitoring.

        Returns True when the prepared or submitted files may have changed, so
        the watch re-reads them. Problems with one request are reported and do
        not end the watch; a lost connection still does.
        """
        changed = False
        while True:
            try:
                action, args = self.requests.get_nowait()
            except queue.Empty:
                return changed
            if not self.session:
                self.emit(
                    "request_error", "Desktop is not connected; nothing was done."
                )
                continue
            try:
                if action in {"cancel_jobs", "job_details"}:
                    targets = [self.target_for(args[0], args[1], "resume")]
                elif action == "rerun":
                    targets = [
                        self.target_for(args[0], args[2], "resume"),
                        self.target_for(args[1], args[2], "preview"),
                    ]
                elif action == "preview":
                    targets = [self.target_for(args[0], args[1], "preview")]
                elif action in {"load_jobs", "history", "restore_jobs"}:
                    settings = picker.validate_settings(
                        dict(args[2] if action == "restore_jobs" else args[0])
                    )
                    targets = [
                        tuple(settings.get(key) for key in ("host", "ink_path", "auth"))
                    ]
                else:
                    targets = []  # Submit uses the current preview's connection.
                if any(target != self.target for target in targets):
                    # Switching connections would end the watch; do it separately.
                    self.emit(
                        "notice",
                        "Those files use a different HPC connection from the jobs "
                        "being watched, so nothing was done. Stop watching first, "
                        "then try again.",
                    )
                    continue
                if action == "submit":
                    self.submit(*args, watch=False)
                else:
                    getattr(self, action)(*args)
            except InterruptedError:
                raise
            except ValueError as error:
                self.emit("request_error", str(error))
                continue
            changed = changed or action in {"preview", "submit", "rerun"}

    def monitor(self):
        if not self.items or not self.items[0].get("receipt"):
            raise ValueError("Preview the files first to load their saved handoffs.")
        if not self.session:
            # Watching again after the connection closed: sign in for these jobs.
            connection = self.items[0]["receipt"]
            self.connect(connection)
            self.emit(
                "target",
                {key: connection.get(key) for key in ("host", "ink_path", "auth")},
            )
        pending = list(self.items)
        while not self.paused.is_set():
            if self.handle_requests():
                pending = list(self.items)  # A preview or submit changed the list.
            for item in list(pending):
                if self.paused.is_set():
                    break
                self.check_stop()
                response = self.session.exchange(
                    {"action": "status"}, receipt=item["receipt"]
                )
                item["response"].update(response)
                state = response["state"]
                self.row(item)
                if state == "PREPARED":
                    pending.remove(item)  # Monitoring can never submit.
                elif state == "COMPLETED" and response.get("exit_code") == "0:0":
                    path = picker.collect(
                        self.session,
                        item,
                        report=lambda value: self.emit("message", value),
                    )
                    self.audit("desktop_retrieval", self.job_metadata(item["response"]))
                    self.row(item, state="RETURNED", output=path)
                    pending.remove(item)
                elif picker.state_name(state) == "CANCELLED":
                    if item["path"] not in self.cancelled_paths:
                        self.emit(
                            "message",
                            f"{item['path'].name}: job {response.get('job_id', '')} was cancelled. "
                            "Inkly never resubmits it; select the row and choose Run again "
                            "to submit it as a new job.",
                        )
                    pending.remove(item)
                elif picker.state_name(state) in self.ENDED:
                    # Ended without a result: say so once and keep watching the
                    # others. Inkly never resubmits it.
                    self.emit(
                        "message",
                        f"{item['path'].name}: job {response.get('job_id', '')} ended as "
                        f"{picker.state_name(state)} (exit {response.get('exit_code')}) "
                        "without a returned result. Select the row and choose Job "
                        "details to see why, or Run again to prepare it as a new job.",
                    )
                    pending.remove(item)
                elif state not in picker.ACTIVE:
                    # A job that may or may not have run: stop for inspection.
                    raise ValueError(
                        f"{item['path'].name}: {state} (exit {response.get('exit_code')}). Other jobs may continue. Keep receipts and inspect this job."
                    )
            if self.paused.is_set():
                break
            if not pending and self.requests.empty():
                self.emit(
                    "message",
                    "Check complete. Open verified returned logs in TENSOR's Analysis & 3D view. Prepared jobs remain unsubmitted.",
                )
                return
            if pending:
                # Wait for the next check; Stop watching or a request wakes early.
                self.wake.wait(self.interval)
                self.wake.clear()
                self.check_stop()
        self.paused.clear()
        self.drop_requests()
        self.emit(
            "message",
            "Stopped watching. Submitted jobs continue on the HPC and the connection "
            "stays open; choose Check & return results to watch again.",
        )

    def job_details(self, selected, settings):
        """On-demand excerpts stay out of receipts, Activity and diagnostic events."""
        paths = set(map(str, selected))
        items = [item for item in self.items if str(item["path"]) in paths]
        if len(items) != 1 or len(paths) != 1:
            raise ValueError("Select one previewed row to inspect its job details.")
        item = items[0]
        self.connect(item["receipt"])
        if self.endpoint_supports("job_details", item["receipt"]):
            details = self.session.exchange(
                {"action": "job_details"}, receipt=item["receipt"]
            )
        else:
            details = {
                **item["response"],
                "logs": [],
                "unavailable": "This HPC endpoint does not support job details. Ask the maintainer for a matching endpoint.",
            }
        self.emit("job_details", details)

    def listing(self, settings, feature):
        """The HPC's list of recorded handoffs, or None on an older HPC copy."""
        self.connect(settings)
        receipt = {**settings, "bundle": uuid.uuid4().hex}
        if not self.endpoint_supports("jobs", receipt):
            self.emit(
                "notice",
                f"{feature} needs an updated HPC copy of Inkly. Ask the maintainer "
                "to update it; Add files works as before.",
            )
            return None
        return self.session.exchange({"action": "jobs"}, receipt=receipt)

    def load_jobs(self, settings, bundles=None):
        """Resolve default recent work, or explicit History selections."""
        settings = picker.validate_settings(dict(settings))
        listing = self.listing(settings, "Load my jobs")
        if listing is None:
            return
        all_jobs = listing["jobs"]
        from_history = bundles is not None
        chosen = (
            jobs.load_set(all_jobs)
            if not from_history
            else [job for job in all_jobs if job["bundle"] in set(bundles)]
        )
        folders = self.index.folders()
        replaced, candidates = 0, []
        for job in chosen:
            kind, path = self.index.locate(job["bundle"])
            if (
                kind == "replaced"
                and self.index.is_replaced(job["bundle"])
                and not from_history
                and picker.state_name(job.get("state"))
                not in (set(picker.ACTIVE) | {"UNKNOWN", "SUBMISSION_UNKNOWN"})
            ):
                replaced += 1
                continue
            candidates.append(job)
            if path is not None:
                folders.append(path.parent)
        report = {}
        found, restore = jobs.find_in_folders(
            candidates,
            folders,
            self.index,
            report=report,
            connection=settings,
            all_jobs=all_jobs,
            include_replaced=from_history,
        )
        # Legacy copy mappings get one chance to locate originals. Once the
        # researcher searches or accepts the copies, future loads are quiet.
        for job in candidates:
            kind, path = self.index.locate(job["bundle"])
            if (
                kind == "found"
                and jobs.is_copy(path, job["bundle"])
                and not self.index.original_checked(job["bundle"])
            ):
                if path in found:
                    found.remove(path)
                    restore.append({**job, "local_copy": str(path)})
        self.audit("desktop_jobs", {"action": "load", "count": len(chosen)})
        self.emit(
            "jobs_found",
            {
                "paths": [str(path) for path in found],
                "restore": restore,
                "replaced": replaced + report.get("replaced", 0),
                "moves": report.get("moves", []),
                "cancelled": sum(
                    picker.state_name(job.get("state")) == "CANCELLED"
                    for job in all_jobs
                )
                if not from_history
                else 0,
                "settings": settings,
                "jobs": all_jobs,
                "unavailable": listing.get("unavailable", []),
                "from_history": from_history,
            },
        )

    def restore_jobs(self, restore, folder, settings):
        """Copy each job's input from the HPC into its own folder here."""
        settings = picker.validate_settings(dict(settings))
        self.connect(settings)
        if not self.endpoint_supports(
            "input", {**settings, "bundle": uuid.uuid4().hex}
        ):
            self.emit(
                "notice",
                f"{len(restore)} job(s) are not on this computer, and copying them "
                "needs an updated HPC copy of Inkly. Ask the maintainer to update it.",
            )
            self.emit("jobs_restored", {"paths": []})
            return
        paths = []
        for job in restore:
            self.check_stop()
            try:
                copy = self.session.exchange(
                    {"action": "input"}, receipt={**settings, "bundle": job["bundle"]}
                )
                path = jobs.restore(folder, job, copy, settings, self.index)
            except (ValueError, OSError) as error:
                self.emit(
                    "message", f"{job.get('name', 'A job')} was not copied: {error}"
                )
                continue
            paths.append(str(path))
            self.emit("message", f"Copied {job['name']} from the HPC to {path.parent}")
        self.audit("desktop_jobs", {"action": "restore", "count": len(paths)})
        self.emit("jobs_restored", {"paths": paths})

    def search_jobs(self, missing, root, context=None):
        """Search local receipts only; never opens or changes an HPC connection."""
        context = context or {}
        report = {}
        found, missing, complete = jobs.search(
            Path(root),
            missing,
            self.index,
            check=self.check_stop,
            report=report,
            connection=context.get("settings"),
            all_jobs=context.get("jobs"),
            include_replaced=context.get("from_history", False),
        )
        self.audit("desktop_jobs", {"action": "search", "count": len(found)})
        self.emit(
            "jobs_searched",
            {
                "paths": [str(path) for path in found],
                "missing": missing,
                "complete": complete,
                "root": str(root),
                "replaced": report.get("replaced", 0),
                "found_count": report.get("found_count", len(found)),
                "copies": report.get("copies", 0),
                "moves": report.get("moves", []),
                "context": context,
            },
        )

    def history(self, settings):
        """Every recorded handoff for the Job history window."""
        settings = picker.validate_settings(dict(settings))
        listing = self.listing(settings, "Job history")
        if listing is None:
            return
        self.audit("desktop_jobs", {"action": "history", "count": len(listing["jobs"])})
        self.emit("history", {**listing, "settings": settings})

    def endpoint_supports(self, operation, receipt):
        """Ask the HPC copy of Inkly; older copies answer "Unsupported operation"."""
        try:
            response = self.session.exchange({"action": "version"}, receipt=receipt)
        except bridge.MissingBundleError:
            raise
        except ValueError as error:
            if str(error) == "Unsupported operation":
                return False
            raise
        return operation in response.get("capabilities", ())

    def selected_items(self, selected, settings, empty_message):
        settings = picker.validate_settings(dict(settings))
        chosen = picker.selections(selected)
        if not chosen:
            raise ValueError(empty_message)
        # "resume" requires a saved receipt for every selected file.
        items = picker.plan(chosen, settings, "resume")
        connection = items[0]["receipt"]
        self.connect(connection)
        self.emit(
            "target", {key: connection.get(key) for key in ("host", "ink_path", "auth")}
        )
        for item in items:
            self.check_stop()
            response = self.session.exchange(
                {"action": "status"}, receipt=item["receipt"]
            )
            if response["name"] != item["path"].name:
                raise ValueError("The receipt belongs to a different input filename")
            item["response"] = response
            digest = hashlib.sha256(item["path"].read_bytes()).hexdigest()
            item["changed"] = digest != response["sha256"]
            self.row(item)
        return settings, items, connection

    def sync_items(self, items):
        """Keep the last preview's items in step with rows changed elsewhere."""
        by_path = {item["path"]: item for item in items}
        for item in self.items:
            update = by_path.get(item["path"])
            if update is not None:
                item.update(
                    receipt=update["receipt"],
                    response=update["response"],
                    changed=update.get("changed", False),
                )

    @staticmethod
    def not_cancellable(item, state):
        """Plain explanation for a selected job that is no longer waiting."""
        name, job = item["path"].name, item["response"].get("job_id")
        if state == "PREPARED" and not job:
            return f"{name} was never submitted, so there is nothing to cancel."
        if not job or state in {"SUBMISSION_UNKNOWN", "UNKNOWN"}:
            return (
                f"{name}: Inkly cannot confirm a Slurm job for it ({state}), so nothing "
                "was cancelled. Check your jobs on the HPC before trying again."
            )
        if state == "COMPLETED":
            return (
                f"{name}: job {job} already completed, so there was nothing to cancel. "
                "Check & return results brings back its log."
            )
        if state == "CANCELLED":
            return f"{name}: job {job} was already cancelled."
        if state in picker.ACTIVE:
            return (
                f"{name}: job {job} has already started ({state}). Only jobs that are "
                "still waiting can be cancelled, so it keeps running."
            )
        return f"{name}: job {job} already ended ({state}), so there was nothing to cancel."

    def cancel_jobs(self, selected, settings):
        """Cancel the selected jobs that Slurm still reports as PENDING. Never submits.

        Each selected job is checked on the HPC first. Jobs that already started,
        finished or were never submitted are explained in a notice rather than
        treated as errors, and the connection stays open.
        """
        self.check_stop()
        _, items, connection = self.selected_items(
            selected, settings, "Select the PENDING rows to cancel first."
        )
        notes, waiting = [], []
        for item in items:
            response = item["response"]
            state = picker.state_name(response.get("state", response["submission"]))
            if state == "PENDING" and response.get("job_id"):
                waiting.append(item)
            else:
                notes.append(self.not_cancellable(item, state))
        if waiting and not self.endpoint_supports("cancel", connection):
            self.audit(
                "desktop_cancel", {"status": "unsupported", "count": len(waiting)}
            )
            jobs = " ".join(item["response"]["job_id"] for item in waiting)
            notes.append(
                f"The Inkly copy on the HPC ({connection['ink_path']}) is too old to "
                "cancel jobs, so nothing was cancelled. Update that copy, or cancel "
                f"on the HPC yourself with: scancel {jobs}"
            )
            waiting = []
        if waiting:
            self.audit("desktop_cancel", {"status": "approved", "count": len(waiting)})
        cancelled = 0
        for item in waiting:
            self.check_stop()
            name, job = item["path"].name, item["response"]["job_id"]
            try:
                response = self.session.exchange(
                    {"action": "cancel", "confirm": True}, receipt=item["receipt"]
                )
            except ValueError as error:
                if not str(error).startswith("Only PENDING jobs can be cancelled"):
                    raise
                # It started between the check and the request.
                response = self.session.exchange(
                    {"action": "status"}, receipt=item["receipt"]
                )
                item["response"].update(response)
                self.row(item)
                notes.append(
                    self.not_cancellable(item, picker.state_name(response.get("state")))
                )
                continue
            item["response"].update(response)
            self.audit(
                "desktop_cancel",
                {"status": "requested", **self.job_metadata(item["response"])},
            )
            self.row(item)
            state = picker.state_name(response.get("state"))
            if state == "CANCELLED":
                cancelled += 1
                self.cancelled_paths.add(item["path"])
                self.emit("message", f"Cancelled {name}: job {job} is CANCELLED.")
            elif state == "PENDING":
                self.cancelled_paths.add(item["path"])
                self.emit(
                    "message",
                    f"Cancellation requested for {name}: job {job} still shows PENDING. "
                    "Check again in a minute.",
                )
            else:
                # Slurm started it first; scancel --state=PENDING left it running.
                notes.append(self.not_cancellable(item, state))
        self.sync_items(items)
        if cancelled:
            self.emit(
                "message",
                "Inkly never resubmits cancelled jobs. To run a file again, select it "
                "and choose Run again.",
            )
        for note in notes:
            self.emit("message", note)
        if notes:
            self.emit("notice", "\n\n".join(notes))

    def rerun(self, selected, paths, settings):
        """Prepare selected ended or unsubmitted files as new jobs; never submits."""
        self.check_stop()
        self.review_id = None
        self.emit("review", None)
        settings, items, _ = self.selected_items(
            selected, settings, "Select the rows to run again first."
        )
        # Check every selected file before changing any receipt.
        for item in items:
            response = item["response"]
            state = picker.state_name(response.get("state", response["submission"]))
            if state not in picker.RERUNNABLE:
                advice = (
                    "Cancel it first, or wait for it to finish."
                    if state in picker.ACTIVE or state == "SUBMITTED"
                    else "Its result is kept; copy the input to a new file name to run it again."
                    if state in {"COMPLETED", "RETURNED"}
                    else "Check your Slurm jobs first; it may still be running."
                )
                raise ValueError(
                    f"{item['path'].name} is {state}, so it was not prepared again. "
                    + advice
                )
            output = item["output"] / item["path"].with_suffix(".log").name
            if output.exists():
                raise ValueError(
                    f"A returned log already exists and will not be overwritten: {output}"
                )
        self.audit("desktop_rerun", {"status": "approved", "count": len(items)})
        for item in items:
            self.check_stop()
            old_bundle = item["receipt"]["bundle"]
            picker.prepare_again(
                self.session,
                item,
                settings,
                report=lambda value: self.emit("message", value),
            )
            try:
                self.index.replaced(old_bundle)
            except Exception:
                pass
            self.audit("desktop_rerun", {"status": "prepared", "count": 1})
        # Re-read the whole list so the rows and the approval cover every file.
        self.preview(paths, settings)

    def execute(self, action, *args):
        try:
            if action == "test_connection":
                self.test_connection(*args)
            elif action == "preview":
                self.preview(*args)
            elif action == "submit":
                self.submit(*args)
                self.unhandled_requests()
            elif action == "monitor":
                self.monitor()
                self.unhandled_requests()
            elif action == "cancel_jobs":
                self.cancel_jobs(*args)
            elif action == "job_details":
                self.job_details(*args)
            elif action == "rerun":
                self.rerun(*args)
            elif action in {"load_jobs", "restore_jobs", "history", "search_jobs"}:
                getattr(self, action)(*args)
            elif action == "close":
                # Record SSH closure while the journal can still accept events;
                # final metadata is handed off by the existing session close.
                if self.stack is not None:
                    self.audit("desktop_connection", {"status": "closed"})
                try:
                    self.journal.close()
                except Exception:
                    pass
                self.close()
            else:
                raise ValueError("Unknown application action")
        except Exception as error:
            code = (
                "interrupted"
                if isinstance(error, InterruptedError)
                else "permission"
                if isinstance(error, PermissionError)
                else "io"
                if isinstance(error, OSError)
                else "validation"
                if isinstance(error, ValueError)
                else "unexpected"
            )
            self.audit("desktop_error", {"action": action, "code": code})
            self.review_id = None
            self.emit("review", None)
            self.drop_requests()  # Never run a stale cancel during a later check.
            self.close()
            self.emit(
                "error",
                str(error)
                + "\nKeep the receipts beside your inputs. Preview the same files to check their recorded jobs before retrying.",
            )
        finally:
            self.emit("done", action)
