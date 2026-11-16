"""GUI-independent controller. All methods except stop run on one worker thread."""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import queue
import shutil
import threading
import uuid

import tensor_bridge as bridge
import tensor_picker as picker
from inkly.core.desktop_diagnostics import DesktopJournal


class DesktopController:
    def __init__(self, emit, askpass=None, interval=30, journal=None):
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

    def my_jobs(self, settings):
        """List this authenticated account's jobs without loading local inputs."""
        settings = picker.validate_settings(dict(settings))
        self.connect(settings)
        receipt = {**settings, "bundle": uuid.uuid4().hex}
        if not self.endpoint_supports("jobs", receipt):
            self.emit(
                "notice",
                "This HPC endpoint does not support My jobs. Ask the maintainer for a matching endpoint.",
            )
            return
        response = self.session.exchange({"action": "jobs"}, receipt=receipt)
        self.emit("my_jobs", response)

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
            elif (
                response.get("state") not in picker.ACTIVE
                and picker.state_name(response.get("state")) != "CANCELLED"
                and not (
                    response.get("state") == "COMPLETED"
                    and response.get("exit_code") == "0:0"
                )
            ):
                raise ValueError(
                    f"{item['path'].name} has an unresolved job state. Inspect it before submitting more jobs."
                )
            item["response"] = response
            self.row(item)

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
    WATCH_REQUESTS = ("cancel_jobs", "preview", "submit", "rerun", "my_jobs")

    def request(self, action, *args):
        """Called from the window thread while monitoring: run this operation next.

        The monitor loop runs it between checks on its open SSH session, so no
        new sign-in is needed, then keeps watching every submitted job.
        """
        if action not in self.WATCH_REQUESTS:
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
                if action == "cancel_jobs":
                    targets = [self.target_for(args[0], args[1], "resume")]
                elif action == "my_jobs":
                    settings = picker.validate_settings(dict(args[0]))
                    targets = [
                        tuple(settings.get(key) for key in ("host", "ink_path", "auth"))
                    ]
                elif action == "rerun":
                    targets = [
                        self.target_for(args[0], args[2], "resume"),
                        self.target_for(args[1], args[2], "preview"),
                    ]
                elif action == "preview":
                    targets = [self.target_for(args[0], args[1], "preview")]
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
                elif state not in picker.ACTIVE:
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
            picker.prepare_again(
                self.session,
                item,
                settings,
                report=lambda value: self.emit("message", value),
            )
            self.audit("desktop_rerun", {"status": "prepared", "count": 1})
        # Re-read the whole list so the rows and the approval cover every file.
        self.preview(paths, settings)

    def execute(self, action, *args):
        try:
            if action == "my_jobs":
                self.my_jobs(*args)
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
            elif action == "rerun":
                self.rerun(*args)
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
