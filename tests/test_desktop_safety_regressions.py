"""Additional failure boundaries; synthetic transport only, no SSH or Slurm."""

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from inkly.core.desktop_diagnostics import DesktopJournal
from tests.test_desktop_diagnostics import records
from tests import test_tensor_desktop
from tests.test_tensor_desktop import SETTINGS, actions, backend
from tests.test_tensor_bridge import bridge

# Reuse the established synthetic controller fixture without copying its fake HPC.
workflow = test_tensor_desktop.workflow


@pytest.fixture(autouse=True)
def private_test_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("INKLY_DESKTOP_DIAGNOSTICS", "1")


@pytest.mark.parametrize(
    "failure,code",
    [
        (PermissionError, "permission"),
        (ConnectionResetError, "io"),
        (InterruptedError, "interrupted"),
        (ValueError, "validation"),
        (RuntimeError, "unexpected"),
    ],
)
def test_ssh_startup_failure_unwinds_auth_and_cannot_submit_or_log_secrets(
    workflow, monkeypatch, failure, code
):
    w = workflow
    auth = []

    @contextmanager
    def authentication(*_args):
        auth.append("entered")
        try:
            yield
        finally:
            auth.append("restored")

    def fail_enter(_session):
        raise failure("PRIVATE_AUTH_AND_RESEARCH_SENTINEL")

    monkeypatch.setattr(backend.picker, "authentication", authentication)
    with monkeypatch.context() as patch:
        patch.setattr(backend.bridge.Session, "__enter__", fail_enter)
        w.controller.execute("preview", w.paths, SETTINGS)
    assert auth == ["entered", "restored"]
    assert w.controller.session is w.controller.stack is w.controller.target is None
    assert w.controller.review_id is None
    assert w.calls == []
    assert not list(w.paths[0].parent.glob("*.inkly.json"))
    events = w.controller.journal.take()
    errors = [
        event["payload"] for event in events if event["event_type"] == "desktop_error"
    ]
    assert len(errors) == 1 and errors[0]["code"] == code
    assert "PRIVATE_AUTH_AND_RESEARCH_SENTINEL" not in json.dumps(events)
    # Retry is preview-only and creates a fresh approval, never a submission.
    w.controller.execute("preview", w.paths, SETTINGS)
    assert w.controller.review_id
    assert actions(w) == ["prepare", "prepare"]
    w.controller.execute("close")
    directory = Path(w.controller.journal.worker.config_loader().state.log_dir)
    values = records(directory)
    assert any(value["event_type"] == "desktop_error" for value in values)
    assert "PRIVATE_AUTH_AND_RESEARCH_SENTINEL" not in json.dumps(values)


def test_disconnect_on_second_presubmit_check_consumes_approval_before_any_submit(
    workflow,
):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    review = w.controller.review_id
    receipt_paths = [path.with_suffix(".gjf.inkly.json") for path in w.paths]
    receipts = [path.read_bytes() for path in receipt_paths]
    second_bundle = json.loads(receipts[1])["bundle"]
    exchange = w.controller.session.exchange

    def disconnect(request, *, receipt):
        if request["action"] == "status" and receipt["bundle"] == second_bundle:
            raise ConnectionResetError("PRIVATE_SSH_FAILURE")
        return exchange(request, receipt=receipt)

    w.controller.session.exchange = disconnect
    w.controller.execute("submit", review, True)
    assert "submit" not in actions(w)
    assert w.controller.review_id is None and w.controller.session is None
    assert [path.read_bytes() for path in receipt_paths] == receipts
    assert "PRIVATE_SSH_FAILURE" not in json.dumps(w.controller.journal.take())
    w.controller.preview(w.paths, SETTINGS)
    assert w.controller.review_id != review
    w.controller.execute("submit", review, True)
    assert "submit" not in actions(w)
    assert w.controller.review_id is None
    w.controller.preview(w.paths, SETTINGS)
    w.controller.monitor()
    assert "submit" not in actions(w)
    assert [path.read_bytes() for path in receipt_paths] == receipts


def test_restart_after_stop_between_submissions_recovers_without_replay(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    receipt_paths = [path.with_suffix(".gjf.inkly.json") for path in w.paths]
    receipts = [path.read_bytes() for path in receipt_paths]
    old = w.controller
    exchange = old.session.exchange

    def stop_after_first_submit(request, *, receipt):
        response = exchange(request, receipt=receipt)
        if request["action"] == "submit":
            old.stop()
        return response

    old.session.exchange = stop_after_first_submit
    old.execute("submit", old.review_id, True)
    assert actions(w).count("submit") == 1
    assert old.session is None and old.review_id is None
    assert [path.read_bytes() for path in receipt_paths] == receipts
    old.execute("close")
    # New application controller; only persisted receipts and synthetic remote
    # state are reused, as after an application interruption.
    w.controller = backend.DesktopController(
        lambda *event: w.events.append(event), interval=0
    )
    w.controller.preview(w.paths, SETTINGS)
    assert w.controller.review()["count"] == 1
    w.controller.monitor()
    assert actions(w).count("submit") == 1
    assert [path.read_bytes() for path in receipt_paths] == receipts
    assert not any(action in {"scancel", "cancel"} for action in actions(w))


def test_broken_diagnostic_write_is_consumed_once_and_never_retries_request(tmp_path):
    journal = DesktopJournal(tmp_path / "local")
    journal.emit("desktop_preview", {"count": 1, "password": "PRIVATE_SECRET"})
    attempts = []

    class BrokenPipe:
        def write(self, raw):
            attempts.append(json.loads(raw))
            raise BrokenPipeError("PRIVATE_SECRET")

    session = object.__new__(bridge.Session)
    session.receipt = {"host": "synthetic", "ink_path": "/bin/ink", "bundle": "a" * 32}
    session.diagnostics = journal
    session.process = SimpleNamespace(
        stdin=BrokenPipe(), kill=lambda: pytest.fail("deadline should be cancelled")
    )
    try:
        with pytest.raises(ValueError, match="interrupted") as error:
            session.exchange({"action": "status"})
        assert "PRIVATE_SECRET" not in str(error.value)
        assert [request["action"] for request in attempts] == ["status"]
        assert journal.take() == []  # An uncertain write is not replayed on close.
        assert "PRIVATE_SECRET" not in json.dumps(attempts)
    finally:
        journal.close()
    values = records(tmp_path / "local")
    assert any(value["event_type"] == "desktop_preview" for value in values)
    assert "PRIVATE_SECRET" not in json.dumps(values)
