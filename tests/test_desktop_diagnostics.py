"""Synthetic Desktop logging; no SSH, cluster storage or scientific payloads."""

import json
import os
from pathlib import Path
import threading
import subprocess
import sys
from types import SimpleNamespace

import pytest

from inkly.core import desktop_diagnostics as desktop
from inkly.core.diagnostics import DiagnosticSession, EventSink


def records(directory):
    return [
        json.loads(line)
        for path in Path(directory).glob("inkly-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def config(directory, shared=None):
    return SimpleNamespace(
        state=SimpleNamespace(log_dir=directory),
        logging=SimpleNamespace(
            enabled=True,
            per_user_logs=True,
            max_log_file_mb=1,
            max_log_files=5,
            level="info",
            shared=shared,
        ),
    )


def test_local_journal_uses_existing_schema_and_excludes_sensitive_fields(tmp_path):
    journal = desktop.DesktopJournal(tmp_path)
    journal.emit(
        "desktop_preview",
        {
            "count": 2,
            "path": "PRIVATE_SENTINEL",
            "password": "PRIVATE_SENTINEL",
            "input": "PRIVATE_SENTINEL",
            "exception": "PRIVATE_SENTINEL",
        },
    )
    journal.emit("desktop_error", {"code": "PRIVATE_SENTINEL", "action": "preview"})
    journal.close()
    values = records(tmp_path)
    assert {v["event_type"] for v in values} >= {
        "desktop_session_start",
        "desktop_preview",
        "desktop_session_end",
    }
    assert "PRIVATE_SENTINEL" not in json.dumps(values)
    preview = next(v for v in values if v["event_type"] == "desktop_preview")
    assert preview["schema_version"] == 1
    assert preview["session"]["user_id"] is None
    assert preview["payload"]["client_session"] == journal.session_id
    assert preview["payload"]["sequence"] == 2
    assert preview["payload"]["occurred_at"].endswith("+00:00")
    if os.name == "posix":
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.glob("*.jsonl"))


def test_transfer_is_bounded_and_consumed_once_without_replay(tmp_path):
    journal = desktop.DesktopJournal(tmp_path)
    for _ in range(300):
        journal.emit("desktop_preview", {"count": 1})
    batch = journal.take()
    assert len(batch) == 256
    assert journal.take() == []
    journal.close()


def test_multiple_sessions_are_distinct_and_disabled_journal_writes_nothing(tmp_path):
    first, second = desktop.DesktopJournal(tmp_path), desktop.DesktopJournal(tmp_path)
    first.close()
    second.close()
    assert first.session_id != second.session_id
    disabled = desktop.DesktopJournal(tmp_path / "disabled", enabled=False)
    disabled.emit("desktop_preview", {"count": 2})
    disabled.close()
    assert disabled.take() == []
    assert not (tmp_path / "disabled").exists()


def test_environment_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("INKLY_DESKTOP_DIAGNOSTICS", "0")
    journal = desktop.DesktopJournal(tmp_path / "disabled")
    journal.close()
    assert not (tmp_path / "disabled").exists()


def test_existing_session_transports_metadata_once_without_extra_job_requests(
    tmp_path, monkeypatch
):
    from tests.test_tensor_bridge import bridge

    capture = tmp_path / "requests.jsonl"
    peer = """
import json, sys
with open(sys.argv[1], 'w') as capture:
    for raw in sys.stdin:
        request = json.loads(raw)
        capture.write(raw)
        capture.flush()
        print(json.dumps({'ok': True, 'bundle': request.get('bundle')}), flush=True)
"""
    popen = subprocess.Popen
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda args, **kwargs: popen(
            [sys.executable, "-u", "-c", peer, str(capture)], **kwargs
        ),
    )
    receipt = {"host": "synthetic", "ink_path": "~/.inkly/bin/ink", "bundle": "a" * 32}
    journal = desktop.DesktopJournal(tmp_path / "local")
    with bridge.Session(receipt, "ssh") as session:
        session.diagnostics = journal
        session.exchange({"action": "submit", "confirm": True})
        session.exchange({"action": "status"})
        journal.close()
    requests = [json.loads(raw) for raw in capture.read_text().splitlines()]
    assert [r["action"] for r in requests] == [
        "submit",
        "status",
        "desktop_diagnostics",
    ]
    assert "desktop_diagnostics" not in requests[1]
    values = [e for r in requests for e in r.get("desktop_diagnostics", [])]
    assert [v["event_type"] for v in values] == [
        "desktop_session_start",
        "desktop_session_end",
    ]


def test_broken_journal_cannot_prevent_transport(tmp_path, monkeypatch):
    from tests.test_tensor_bridge import bridge

    session = object.__new__(bridge.Session)

    class BrokenJournal:
        def take(self):
            raise OSError("PRIVATE_SENTINEL")

    session.diagnostics = BrokenJournal()
    assert session._diagnostic_batch() == []


def test_remote_revalidates_metadata_and_deduplicates_sequence(tmp_path):
    journal = desktop.DesktopJournal(tmp_path / "local")
    batch = journal.take()
    batch[0]["payload"]["secret"] = "PRIVATE_SENTINEL"
    batch[0]["payload"]["client_session"] = journal.session_id
    remote = desktop.RemoteDesktopDiagnostics(lambda: config(tmp_path / "remote"))
    remote.accept(batch)
    remote.accept(batch)
    remote.accept(
        [{"event_type": "user_prompt", "payload": {"content": "PRIVATE_SENTINEL"}}]
    )
    remote.accept([{"event_type": [], "payload": {}}])
    remote.close()
    journal.close()
    values = records(tmp_path / "remote")
    assert sum(v["event_type"] == "desktop_session_start" for v in values) == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(values)


def test_broken_config_recovers_for_later_events_without_replaying(tmp_path):
    broken = [True]

    def load():
        if broken[0]:
            raise PermissionError("PRIVATE_CONFIG_PATH")
        return config(tmp_path / "remote")

    journal = desktop.DesktopJournal(tmp_path / "local")
    remote = desktop.RemoteDesktopDiagnostics(load)
    remote.accept(journal.take())
    remote.worker.pending.join()
    broken[0] = False
    journal.emit("desktop_preview", {"count": 3})
    remote.accept(journal.take())
    remote.close()
    journal.close()
    values = records(tmp_path / "remote")
    assert [
        v["payload"]["count"] for v in values if v["event_type"] == "desktop_preview"
    ] == [3]
    assert not any(v["event_type"] == "desktop_session_start" for v in values)
    assert any(
        v["event_type"] == "desktop_session_start" for v in records(tmp_path / "local")
    )


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError, OSError])
def test_local_failure_never_blocks_transport_and_later_writes_recover(
    tmp_path, monkeypatch, failure
):
    append = EventSink.append
    failing = [True]

    def write(sink, line):
        if failing[0]:
            raise failure("PRIVATE_SENTINEL")
        return append(sink, line)

    monkeypatch.setattr(EventSink, "append", write)
    journal = desktop.DesktopJournal(tmp_path)
    journal.worker.pending.join()
    assert journal.take()[0]["event_type"] == "desktop_session_start"
    failing[0] = False
    journal.emit("desktop_preview", {"count": 4})
    journal.close()
    assert any(v["event_type"] == "desktop_preview" for v in records(tmp_path))


def test_stalled_sink_has_bounded_queue_and_does_not_block_producer(
    tmp_path, monkeypatch
):
    release = threading.Event()
    entered = threading.Event()

    def write(*args):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(DiagnosticSession, "emit", write)
    worker = desktop.DiagnosticWorker(lambda: config(tmp_path))
    worker.put([{"event_type": "desktop_stop", "payload": {}}])
    assert entered.wait(2)
    for _ in range(300):
        worker.put([])
    assert worker.pending.qsize() == 256
    worker.close(timeout=0)
    release.set()
    worker.thread.join(2)
    assert not worker.thread.is_alive()


def test_rotation_preserves_other_sessions_active_files_and_limits_completed(tmp_path):
    first = EventSink(tmp_path, "a" * 32, 32, 2)
    other = EventSink(tmp_path, "b" * 32, 32, 2)
    other.append(b'{"active":true}\n')
    for _ in range(8):
        first.append(b'{"synthetic":true}\n')
    first.close()
    assert len(list(tmp_path.glob("inkly-*.jsonl"))) == 3
    assert list(tmp_path.glob("*.active.jsonl"))
    other.close()
    assert len(list(tmp_path.glob("inkly-*.jsonl"))) == 2


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX directory policy")
def test_unavailable_shared_storage_keeps_private_fallback(tmp_path):
    import grp

    shared = SimpleNamespace(
        enabled=True,
        directory=tmp_path / "unprovisioned",
        retention_days=0,
        group=grp.getgrgid(os.getgid()).gr_name,
    )
    remote = desktop.RemoteDesktopDiagnostics(
        lambda: config(tmp_path / "private", shared)
    )
    journal = desktop.DesktopJournal(tmp_path / "local")
    remote.accept(journal.take())
    remote.close()
    journal.close()
    assert not shared.directory.exists()
    assert any(
        v["event_type"] == "desktop_session_start"
        for v in records(tmp_path / "private")
    )
