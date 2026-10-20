from __future__ import annotations

import json
import os
import re
import stat
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from inkly.core.permissions import open_shared_directory, validate_shared_acl
from inkly.core.storage import private_directory


EVENT_FIELDS = {
    "session_start": {"status": {"started"}},
    "session_end": {"status": {"success", "error"}, "duration_ms": int},
    "user_prompt": {"chars": int},
    "ai_response": {"chars": int},
    "guardrail_block": {"code": {"cluster_scope_withheld"}},
    "error": {
        "component": {"runtime", "backend", "plugin", "retrieval", "storage"},
        "code": {
            "request_failed",
            "backend_failed",
            "plugin_failed",
            "retrieval_failed",
            "history_unavailable",
            "shared_storage_unavailable",
        },
    },
}
COMPLETED_LOG = re.compile(r"inkly-[0-9a-f]{32}-[0-9]{6}\.jsonl")


def _warn(message):
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except RuntimeWarning:
        pass


class EventSink:
    """One invocation owns each file; other writers never append to it."""

    def __init__(self, directory, session_id, max_bytes, max_files, shared=None):
        self.directory = Path(directory).expanduser()
        self.session_id = session_id
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.shared = shared
        self.root_descriptor = None
        self.descriptor = None
        self.segment = 0
        self.active_name = None

    def _open_root(self):
        if self.shared is not None:
            self.root_descriptor = open_shared_directory(
                self.directory, self.shared.group
            )
            return
        private_directory(self.directory)
        if os.name == "posix":
            self.root_descriptor = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )

    def _open_file(self):
        if self.root_descriptor is None:
            self._open_root()
        self.active_name = f"inkly-{self.session_id}-{self.segment:06d}.active.jsonl"
        mode = 0o640 if self.shared is not None else 0o600
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "posix":
            self.descriptor = os.open(
                self.active_name, flags, mode, dir_fd=self.root_descriptor
            )
            os.fchmod(self.descriptor, mode)
            info = os.fstat(self.descriptor)
            root_info = os.fstat(self.root_descriptor)
            if (
                info.st_uid != os.getuid()
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (self.shared is not None and info.st_gid != root_info.st_gid)
            ):
                raise PermissionError("Diagnostic file ownership is invalid")
            if self.shared is not None:
                validate_shared_acl(self.descriptor, directory=False)
        else:
            self.descriptor = os.open(self.directory / self.active_name, flags, mode)

    def _prune(self):
        if self.shared is not None and self.shared.retention_days == 0:
            return
        names = os.listdir(
            self.root_descriptor if os.name == "posix" else self.directory
        )
        completed = []
        for name in names:
            if not COMPLETED_LOG.fullmatch(name):
                continue
            try:
                if os.name == "posix":
                    info = os.stat(
                        name, dir_fd=self.root_descriptor, follow_symlinks=False
                    )
                else:
                    info = (self.directory / name).lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and (os.name != "posix" or info.st_uid == os.getuid())
            ):
                completed.append((info.st_mtime, name))
        completed.sort(reverse=True)
        cutoff = (
            time.time() - self.shared.retention_days * 86400
            if self.shared is not None
            else float("-inf")
        )
        for index, (modified, name) in enumerate(completed):
            if (
                self.shared is not None or index < self.max_files
            ) and modified >= cutoff:
                continue
            try:
                if os.name == "posix":
                    os.unlink(name, dir_fd=self.root_descriptor)
                else:
                    (self.directory / name).unlink()
            except FileNotFoundError:
                pass

    def _finish_file(self):
        if self.descriptor is None:
            return
        os.close(self.descriptor)
        self.descriptor = None
        completed_name = self.active_name.replace(".active.jsonl", ".jsonl")
        if os.name == "posix":
            os.rename(
                self.active_name,
                completed_name,
                src_dir_fd=self.root_descriptor,
                dst_dir_fd=self.root_descriptor,
            )
        else:
            (self.directory / self.active_name).rename(self.directory / completed_name)
        self._prune()
        self.segment += 1

    def append(self, line: bytes):
        if len(line) > self.max_bytes:
            raise ValueError("Diagnostic event exceeds the file-size limit")
        if self.descriptor is None:
            self._open_file()
        if os.fstat(self.descriptor).st_size + len(line) > self.max_bytes:
            self._finish_file()
            self._open_file()
        offset = os.lseek(self.descriptor, 0, os.SEEK_END)
        try:
            pending = memoryview(line)
            while pending:
                written = os.write(self.descriptor, pending)
                if written == 0:
                    raise OSError("Diagnostic write made no progress")
                pending = pending[written:]
            os.fsync(self.descriptor)
        except OSError:
            try:
                os.ftruncate(self.descriptor, offset)
            except OSError:
                pass
            raise

    def close(self):
        try:
            self._finish_file()
        finally:
            if self.root_descriptor is not None:
                os.close(self.root_descriptor)
                self.root_descriptor = None


class DiagnosticSession:
    """Schema-v1 diagnostic metadata with no free-text or identity fields."""

    def __init__(self, config):
        self.config = getattr(config, "logging", None)
        self.enabled = bool(self.config and self.config.enabled)
        self.session_id = uuid4().hex
        self.started = time.monotonic()
        self.lock = RLock()
        self.sinks = []
        self.failed_sinks = set()
        if not self.enabled:
            return
        state = getattr(config, "state", None)
        max_bytes = getattr(self.config, "max_log_file_mb", 10) * 1024 * 1024
        max_files = getattr(self.config, "max_log_files", 5)
        if getattr(self.config, "per_user_logs", True):
            self.sinks.append(
                EventSink(
                    getattr(state, "log_dir", Path.home() / ".inkly" / "logs"),
                    self.session_id,
                    max_bytes,
                    max_files,
                )
            )
        private_scratch = getattr(self.config, "private_scratch_dir", "")
        if private_scratch and all(
            Path(private_scratch).expanduser().absolute() != sink.directory.absolute()
            for sink in self.sinks
        ):
            self.sinks.append(
                EventSink(private_scratch, self.session_id, max_bytes, max_files)
            )
        shared = getattr(self.config, "shared", None)
        if shared is not None and shared.enabled:
            self.sinks.append(
                EventSink(
                    shared.directory, self.session_id, max_bytes, max_files, shared
                )
            )

    def emit(self, event_type: str, payload=None):
        if (
            not self.enabled
            or not isinstance(event_type, str)
            or event_type not in EVENT_FIELDS
        ):
            return
        if event_type == "user_prompt" and not getattr(
            self.config, "log_user_prompts", True
        ):
            return
        if event_type == "ai_response" and not getattr(
            self.config, "log_ai_responses", True
        ):
            return
        severity = (
            30
            if event_type == "guardrail_block"
            else 40
            if event_type == "error"
            else 20
        )
        threshold = {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(
            getattr(self.config, "level", "info"), 20
        )
        if severity < threshold:
            return
        clean = {}
        payload = payload if isinstance(payload, dict) else {}
        for name, allowed in EVENT_FIELDS[event_type].items():
            value = payload.get(name)
            if allowed is int:
                if type(value) is int and value >= 0:
                    clean[name] = min(value, 2**31 - 1)
            elif isinstance(value, str) and value in allowed:
                clean[name] = value
        event = {
            "schema_version": 1,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session": {
                "session_id": self.session_id,
                "user_id": None,
                "host": "redacted",
                "pid": os.getpid(),
            },
            "payload": clean,
        }
        line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        with self.lock:
            for index, sink in enumerate(self.sinks):
                if index in self.failed_sinks:
                    continue
                try:
                    sink.append(line)
                except (OSError, ValueError):
                    self.failed_sinks.add(index)
                    _warn(
                        "An Inkly diagnostic destination is unavailable; "
                        "conversation storage is unaffected."
                    )

    def __enter__(self):
        self.emit("session_start", {"status": "started"})
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception_type is not None:
            self.emit("error", {"component": "runtime", "code": "request_failed"})
        self.emit(
            "session_end",
            {
                "status": "error" if exception_type is not None else "success",
                "duration_ms": int((time.monotonic() - self.started) * 1000),
            },
        )
        for sink in self.sinks:
            try:
                sink.close()
            except OSError:
                _warn("Inkly diagnostic finalization failed.")
