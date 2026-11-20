"""Bounded Desktop metadata delivery through the existing diagnostic sinks."""

from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
from uuid import uuid4

from inkly.core.diagnostics import DESKTOP_FIELDS, DiagnosticSession, clean_payload


class DiagnosticWorker:
    """Disk/ACL failures cannot stall the GUI or the scheduler request thread."""

    def __init__(self, config_loader):
        self.config_loader = config_loader
        self.pending = queue.Queue(maxsize=256)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def put(self, events):
        if self.stopped.is_set():
            return
        try:
            self.pending.put_nowait(events)
        except queue.Full:
            pass  # Bounded best-effort diagnostics, never backpressure Slurm.

    def _run(self):
        session = None
        try:
            while not self.stopped.is_set() or not self.pending.empty():
                try:
                    events = self.pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if session is None:
                        session = DiagnosticSession(self.config_loader())
                    for event in events:
                        session.emit(event["event_type"], event["payload"])
                    if session.failed_sinks:
                        session.__exit__(None, None, None)
                        session = None  # Retry destinations only on future events.
                except Exception:
                    # No exception strings (including paths/config values) in logs.
                    session = None
                finally:
                    self.pending.task_done()
        finally:
            if session is not None:
                session.__exit__(None, None, None)

    def close(self, timeout=1):
        self.stopped.set()
        self.thread.join(timeout=timeout)


class DesktopJournal:
    """Private Windows fallback plus at-most-once batches for authenticated SSH."""

    def __init__(self, directory=None, *, enabled=None):
        if enabled is None:
            enabled = os.environ.get("INKLY_DESKTOP_DIAGNOSTICS", "1") != "0"
        self.enabled = enabled
        self.session_id = uuid4().hex
        self.sequence = 0
        self.lock = threading.Lock()
        self.pending = deque(maxlen=256)
        self.closed = False
        root = (
            Path(directory)
            if directory is not None
            else (
                Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".inkly")
                / "Inkly"
                / "diagnostics"
            )
        )
        config = SimpleNamespace(
            state=SimpleNamespace(log_dir=root),
            logging=SimpleNamespace(
                enabled=enabled,
                per_user_logs=True,
                max_log_file_mb=1,
                max_log_files=5,
                level="info",
                shared=None,
            ),
        )
        self.worker = DiagnosticWorker(lambda: config) if enabled else None
        self.emit("desktop_session_start")

    def emit(self, event_type, payload=None):
        if not self.enabled or self.closed or event_type not in DESKTOP_FIELDS:
            return
        with self.lock:
            self.sequence += 1
            event = {
                "event_type": event_type,
                "payload": clean_payload(
                    event_type,
                    {
                        **(payload or {}),
                        "client_session": self.session_id,
                        "sequence": self.sequence,
                        "occurred_at": datetime.now(timezone.utc).isoformat(
                            timespec="microseconds"
                        ),
                    },
                ),
            }
            self.pending.append(event)
            self.worker.put([event])

    def take(self):
        """Consume once before sending; uncertain SSH writes are never replayed."""
        with self.lock:
            events = list(self.pending)
            self.pending.clear()
            return events

    def close(self):
        if self.closed:
            return
        self.emit("desktop_session_end")
        self.closed = True
        if self.worker is not None:
            self.worker.close()


class RemoteDesktopDiagnostics:
    """Receive only bounded, revalidated metadata; never accepts a log path."""

    def __init__(self, config_loader=None):
        if config_loader is None:

            def config_loader():
                from inkly.config import TomlParser

                return TomlParser(Path.home() / ".inkly" / "config.toml").load()

        self.config_loader = config_loader
        self.worker = None
        self.sequences = {}

    def accept(self, values):
        if not isinstance(values, list) or len(values) > 256:
            return
        events = []
        for value in values:
            if not isinstance(value, dict):
                continue
            event_type = value.get("event_type")
            if not isinstance(event_type, str) or event_type not in DESKTOP_FIELDS:
                continue
            payload = clean_payload(event_type, value.get("payload"))
            session, sequence = payload.get("client_session"), payload.get("sequence")
            if not session or sequence is None:
                continue
            if sequence <= self.sequences.get(session, -1):
                continue
            if session not in self.sequences and len(self.sequences) >= 8:
                continue  # One authenticated stream should need one Desktop ID.
            self.sequences[session] = sequence
            events.append({"event_type": event_type, "payload": payload})
        if events:
            if self.worker is None:
                self.worker = DiagnosticWorker(self.config_loader)
            self.worker.put(events)

    def close(self):
        if self.worker is not None:
            self.worker.close()
