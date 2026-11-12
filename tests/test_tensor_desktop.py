"""Desktop confirmation, cancellation, recovery and native Tk tests. No network."""

import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location(
    "desktop_backend_test", scripts / "tensor_desktop_backend.py"
)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
SETTINGS = {
    "host": "researcher@cluster",
    "ink_path": "~/.inkly/bin/ink",
    "auth": "configured",
    "hours": 2,
}


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    paths = [tmp_path / f"sample_{number}.gjf" for number in (1, 2)]
    for path in paths:
        path.write_bytes(
            b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
        )
    records, calls, events, connections = {}, [], [], []

    class Session:
        def __init__(self, connection, ssh):
            connections.append(connection)
            self.process = SimpleNamespace(
                terminate=lambda: calls.append((None, "stop-ssh"))
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def exchange(self, request, *, receipt):
            action, bundle = request["action"], receipt["bundle"]
            calls.append((bundle, action))
            if action == "prepare":
                data = base64.b64decode(request["data"])
                records[bundle] = {
                    "name": request["name"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "script_sha256": "abc",
                    "stages": 1,
                    "submission": "PREPARED",
                    "state": "PREPARED",
                    "script": "# synthetic SBATCH",
                    "resources": {
                        "cpus": 2,
                        "slurm_memory_mib": 1024,
                        "gaussian_memory_mb": 768,
                        "hours": request["hours"],
                    },
                }
            record = records[bundle]
            if action == "submit":
                record.update(submission="SUBMITTED", state="RUNNING", job_id="123")
            if action == "status" and record["state"] == "RUNNING":
                record.update(state="COMPLETED", exit_code="0:0")
            if action == "fetch":
                data = b"synthetic normal output"
                return {
                    "name": Path(record["name"]).with_suffix(".log").name,
                    "data": base64.b64encode(data).decode(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            # Copy nested resources, as the real transport JSON does.
            return {**record, "resources": dict(record["resources"])}

    monkeypatch.setattr(backend.bridge, "Session", Session)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "ssh.exe")
    controller = backend.DesktopController(
        lambda *event: events.append(event), interval=0
    )
    yield SimpleNamespace(
        controller=controller,
        paths=paths,
        records=records,
        calls=calls,
        events=events,
        connections=connections,
    )
    controller.close()


def actions(workflow):
    return [action for _, action in workflow.calls]


def test_preview_cannot_submit_and_monitor_does_not_submit_prepared(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    assert actions(w) == ["prepare", "prepare"]
    assert w.controller.review()["count"] == 2
    w.controller.monitor()
    assert actions(w) == ["prepare", "prepare", "status", "status"]


@pytest.mark.parametrize(
    "confirmation,identifier", [(False, "current"), (True, "stale"), (False, "stale")]
)
def test_submit_requires_current_approved_preview(workflow, confirmation, identifier):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    with pytest.raises(ValueError, match="confirmation"):
        w.controller.submit(
            w.controller.review_id if identifier == "current" else "stale", confirmation
        )
    assert "submit" not in actions(w)


def test_approved_batch_uses_same_connection_and_returns_both_logs(workflow):
    w = workflow
    original = [path.read_bytes() for path in w.paths]
    w.controller.preview(w.paths, SETTINGS)
    review = w.controller.review()
    assert review["cpus"] == 4 and review["memory_mib"] == 2048
    w.controller.submit(review["id"], True)
    assert len(w.connections) == 1
    assert actions(w)[:6] == [
        "prepare",
        "prepare",
        "status",
        "status",
        "submit",
        "submit",
    ]
    assert len(list((w.paths[0].parent / "returned").glob("*.log"))) == 2
    assert [path.read_bytes() for path in w.paths] == original
    assert (
        len(
            [
                event
                for event in w.events
                if event[0] == "row" and event[1]["state"] == "RETURNED"
            ]
        )
        == 2
    )
    assert w.controller.review_id is None
    w.controller.preview(w.paths, SETTINGS)
    w.controller.monitor()
    assert actions(w).count("prepare") == 2 and actions(w).count("submit") == 2


@pytest.mark.parametrize(
    "change", ["input", "resources", "script", "output", "unknown"]
)
def test_rechecks_whole_selection_before_first_submit(workflow, change):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    record = list(w.records.values())[1]
    if change == "input":
        w.paths[1].write_bytes(b"edited after preview")
    elif change == "resources":
        record["resources"]["cpus"] = 32
    elif change == "script":
        record["script_sha256"] = "changed"
    elif change == "unknown":
        record.update(submission="SUBMISSION_UNKNOWN", state="UNKNOWN")
    else:
        output = w.paths[1].parent / "returned" / "sample_2.log"
        output.parent.mkdir()
        output.write_text("existing research result")
    with pytest.raises(ValueError):
        w.controller.submit(w.controller.review_id, True)
    assert "submit" not in actions(w)
    assert w.controller.review_id is None


def test_lost_submit_reply_consumes_approval_and_resume_never_duplicates(
    workflow, monkeypatch
):
    w = workflow
    original = backend.bridge.Session.exchange

    def lost(self, request, **kwargs):
        response = original(self, request, **kwargs)
        if request["action"] == "submit":
            raise ValueError("Lost acknowledgement")
        return response

    w.controller.preview(w.paths, SETTINGS)
    monkeypatch.setattr(backend.bridge.Session, "exchange", lost)
    w.controller.execute("submit", w.controller.review_id, True)
    assert actions(w).count("submit") == 1
    assert w.controller.session is None
    assert any(event[0] == "error" for event in w.events)
    monkeypatch.setattr(backend.bridge.Session, "exchange", original)
    w.controller.preview(w.paths, SETTINGS)
    assert w.controller.review()["count"] == 1
    w.controller.submit(w.controller.review_id, True)
    assert actions(w).count("submit") == 2


def test_stop_closes_only_ssh_and_never_cancels_or_submits_jobs(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    w.controller.stop()
    with pytest.raises(InterruptedError):
        w.controller.monitor()
    assert actions(w)[-1] == "stop-ssh"
    assert "submit" not in actions(w)


def test_missing_settings_do_not_connect(workflow):
    w = workflow
    w.controller.execute("preview", w.paths, {**SETTINGS, "host": ""})
    assert not w.connections
    assert any(event[0] == "error" for event in w.events)


def test_stop_before_worker_starts_prevents_connection(workflow):
    w = workflow
    w.controller.stop()
    w.controller.execute("preview", w.paths, SETTINGS)
    assert not w.connections and not w.calls


def test_bundled_dialog_fallback_restores_auth_environment(tmp_path, monkeypatch):
    dialog = tmp_path / "packaged-dialog.exe"
    dialog.write_bytes(b"synthetic placeholder; never executed")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS_REQUIRE", raising=False)
    monkeypatch.setenv("INKLY_SSH_TARGET", "old-target")
    with backend.picker.authentication(
        {**SETTINGS, "auth": "keyboard-interactive"}, dialog
    ):
        assert os.environ["SSH_ASKPASS"] == str(dialog)
        assert os.environ["SSH_ASKPASS_REQUIRE"] == "force"
        assert os.environ["INKLY_SSH_TARGET"] == SETTINGS["host"]
    assert "SSH_ASKPASS" not in os.environ
    assert "SSH_ASKPASS_REQUIRE" not in os.environ
    assert os.environ["INKLY_SSH_TARGET"] == "old-target"


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_window_add_remove_preview_and_confirmation(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    try:
        app.add_paths(w.paths)
        assert len(app.table.get_children()) == 2
        assert str(app.submit_button["state"]) == "disabled"
        root.update()
        w.controller.preview(w.paths, SETTINGS)
        for event in w.events:
            app.events.put(event)
        app.drain()
        assert str(app.submit_button["state"]) == "normal"
        scheduled = []
        monkeypatch.setattr(app, "start", lambda *args: scheduled.append(args))
        monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: False)
        app.submit_button.invoke()
        assert not scheduled
        monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
        app.submit_button.invoke()
        assert scheduled == [("submit", w.controller.review_id, True)]
        app.table.selection_set(str(w.paths[0]))
        app.remove_button.invoke()
        assert len(app.files) == 1 and app.review is None
        assert str(app.submit_button["state"]) == "disabled"
        assert app.table.item(str(w.paths[1]))["values"][6] == "Not previewed"
    finally:
        app.close()
