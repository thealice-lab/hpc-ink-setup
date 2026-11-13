"""GUI-independent controller. All methods except stop run on one worker thread."""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
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
        # Closing SSH never issues scancel. Persisted receipts recover uncertain calls.
        process = getattr(self.session, "process", None)
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass

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
                "desktop_job_state", {"state": current, **self.job_metadata(response)}
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

    def review(self):
        ready = [
            item for item in self.items if item["response"]["submission"] == "PREPARED"
        ]
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
            elif response.get("state") not in picker.ACTIVE and not (
                response.get("state") == "COMPLETED"
                and response.get("exit_code") == "0:0"
            ):
                raise ValueError(
                    f"{item['path'].name} has an unresolved job state. Inspect it before submitting more jobs."
                )
            item["response"] = response
            self.row(item)

    def submit(self, review_id, confirmed=False):
        if not confirmed or not self.review_id or review_id != self.review_id:
            raise ValueError("Submission needs confirmation of the current preview.")
        self.review_id = None  # Approval is consumed even if an SSH response is lost.
        self.audit(
            "desktop_approval",
            {
                "count": sum(
                    item["response"]["submission"] == "PREPARED" for item in self.items
                )
            },
        )
        self.emit("review", None)
        self.refresh_before_submit()
        for item in self.items:
            self.check_stop()
            if item["response"]["submission"] == "PREPARED":
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
        self.monitor()

    def monitor(self):
        if not self.session or not self.items:
            raise ValueError("Preview the files first to load their saved handoffs.")
        pending = list(self.items)
        while pending:
            for item in list(pending):
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
                elif state not in picker.ACTIVE:
                    raise ValueError(
                        f"{item['path'].name}: {state} (exit {response.get('exit_code')}). Other jobs may continue. Keep receipts and inspect this job."
                    )
            if pending and self.cancelled.wait(self.interval):
                self.check_stop()
        self.emit(
            "message",
            "Check complete. Open verified returned logs in TENSOR's Analysis & 3D view. Prepared jobs remain unsubmitted.",
        )

    def execute(self, action, *args):
        try:
            if action == "preview":
                self.preview(*args)
            elif action == "submit":
                self.submit(*args)
            elif action == "monitor":
                self.monitor()
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
            self.close()
            self.emit(
                "error",
                str(error)
                + "\nKeep the receipts beside your inputs. Preview the same files to check their recorded jobs before retrying.",
            )
        finally:
            self.emit("done", action)
