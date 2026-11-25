"""Desktop confirmation, cancellation, recovery and native Tk tests. No network."""

import base64
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import warnings

import pytest


@pytest.fixture(autouse=True)
def collect_garbage_on_main_thread():
    """Free each test's Tk windows on the main thread.

    Tcl aborts the process if a background thread (worker or diagnostics)
    frees a Tk object, and Python's cyclic collector runs on whichever thread
    triggers it. Closed test windows sit in reference cycles, so every test
    runs with automatic collection off and ends with a collection here.
    """
    enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    yield
    gc.collect()
    if enabled:
        gc.enable()


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


@pytest.fixture(autouse=True)
def isolated_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    paths = [tmp_path / f"sample_{number}.gjf" for number in (1, 2)]
    for path in paths:
        path.write_bytes(
            b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nSynthetic\n\n0 1\nH 0 0 0\n\n"
        )
    records, calls, events, connections = {}, [], [], []
    missing_bundles = set()
    # None imitates an older HPC endpoint without the "version" operation.
    endpoint = {"capabilities": ["prepare", "submit", "status", "fetch", "cancel"]}

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
            if action == "status" and bundle in missing_bundles:
                raise backend.bridge.MissingBundleError(
                    "Unknown bundle; prepare a new input first"
                )
            if action == "version":
                if endpoint["capabilities"] is None:
                    raise ValueError("Unsupported operation")
                return {
                    "bundle": bundle,
                    "version": endpoint.get("version", "0.4.2"),
                    "capabilities": endpoint["capabilities"],
                }
            if action == "jobs":
                return {
                    "bundle": bundle,
                    "jobs": [dict(job) for job in endpoint.get("jobs", [])],
                    "unavailable": [],
                    "truncated": False,
                    "observed_at": "2026-11-15T12:00:00+00:00",
                }
            if action == "input":
                return {**endpoint["inputs"][bundle], "bundle": bundle}
            if action == "cancel":
                assert request.get("confirm") is True
                if records[bundle]["state"] != "PENDING":
                    raise ValueError("Only PENDING jobs can be cancelled")
                records[bundle]["state"] = "CANCELLED by 1000"
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
        missing_bundles=missing_bundles,
        endpoint=endpoint,
    )
    controller.close()
    controller.journal.close()


def test_job_details_stay_out_of_journal_activity_and_receipts(workflow):
    c = workflow.controller
    c.preview(workflow.paths, SETTINGS)
    path = workflow.paths[0]
    sidecar = path.with_suffix(".gjf.inkly.json")
    before = sidecar.read_bytes()
    workflow.endpoint["capabilities"].append("job_details")
    original_exchange = c.session.exchange

    def exchange(request, **kwargs):
        if request["action"] == "job_details":
            return {
                "name": path.name,
                "state": "FAILED",
                "logs": [{"text": "PRIVATE_RESEARCH_EXCERPT"}],
            }
        return original_exchange(request, **kwargs)

    c.session.exchange = exchange
    audited = []
    c.audit = lambda *args: audited.append(args)
    c.job_details([str(path)], SETTINGS)
    assert sidecar.read_bytes() == before
    assert len(workflow.connections) == 1
    assert workflow.events[-1][0] == "job_details"
    assert "PRIVATE_RESEARCH_EXCERPT" in str(workflow.events[-1])
    assert "PRIVATE_RESEARCH_EXCERPT" not in str(audited)
    assert "PRIVATE_RESEARCH_EXCERPT" not in str(messages(workflow))
    assert "submit" not in actions(workflow) and "cancel" not in actions(workflow)


def test_job_details_old_endpoint_preserves_connection_and_preview(workflow):
    c = workflow.controller
    c.preview(workflow.paths, SETTINGS)
    review = c.review_id
    c.job_details([str(workflow.paths[0])], SETTINGS)
    assert "does not support" in workflow.events[-1][1]["unavailable"]
    assert c.review_id == review
    assert len(workflow.connections) == 1


def test_job_details_request_uses_watch_session_without_restarting_it(workflow):
    c = workflow.controller
    c.preview(workflow.paths, SETTINGS)
    c.request("job_details", [str(workflow.paths[0])], SETTINGS)
    assert c.handle_requests() is False
    assert workflow.events[-1][0] == "job_details"
    assert len(workflow.connections) == 1


def test_job_details_wording_marks_missing_estimates_and_private_excerpts():
    import tensor_desktop_status as wording

    pending = wording.job_details_text(
        {"name": "sample.gjf", "state": "PENDING", "reason": "Priority"}
    )
    assert "Estimated start (HPC local time): Unavailable" in pending
    assert "Elapsed run time: Unavailable" in pending
    assert "Pending reason: Priority" in pending
    assert "estimates can change" in pending
    failed = wording.job_details_text(
        {
            "name": "sample.gjf",
            "state": "TIMEOUT",
            "logs": [{"name": "sample.log", "available": False}],
        }
    )
    assert "Log unavailable" in failed and "not saved to Activity" in failed


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Tk")
def test_native_job_details_button_routes_read_only_watch_request(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    root = tk.Tk()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda *a, **k: workflow.controller,
        settings_path=tmp_path / "settings.json",
    )
    try:
        path = str(workflow.paths[0])
        app.add_paths([path])
        app.rows[path] = {"name": "sample.gjf", "state": "PENDING"}
        app.table.selection_set(path)
        app.busy = True
        app.action = "monitor"
        app.buttons()
        assert str(app.details_button["state"]) == "normal"
        app.job_details()
        action, args = workflow.controller.requests.get_nowait()
        assert action == "job_details" and args[0] == [path]
    finally:
        app.busy = False
        app.close()


def actions(workflow):
    return [action for _, action in workflow.calls]


def record_for(workflow, path):
    receipt = json.loads(path.with_suffix(".gjf.inkly.json").read_text())
    return workflow.records[receipt["bundle"]]


@pytest.mark.parametrize(
    "probe", ["current", "older-release", "no-cancel", "old", "missing", "invalid"]
)
def test_connection_needs_no_inputs_slurm_or_receipts(workflow, probe):
    c = workflow.controller
    before = {path: path.read_bytes() for path in workflow.paths}
    every = ["prepare", "submit", "status", "fetch", "cancel", "job_details", "jobs"]
    workflow.endpoint["capabilities"] = every
    if probe == "older-release":
        # A compatible HPC copy from an earlier release is not a warning.
        workflow.endpoint["version"] = "0.4.0"
    elif probe == "no-cancel":
        workflow.endpoint["capabilities"] = [op for op in every if op != "cancel"]
    elif probe == "old":
        workflow.endpoint["capabilities"] = None
    elif probe == "missing":
        workflow.endpoint["capabilities"] = ["status"]
    elif probe == "invalid":
        c.connect(SETTINGS)
        c.session.exchange = lambda *a, **k: {
            "version": "PRIVATE_BAD_HANDSHAKE\n",
            "capabilities": [],
        }
        with pytest.raises(ValueError, match="invalid version handshake"):
            c.test_connection(SETTINGS)
        assert "PRIVATE_BAD_HANDSHAKE" not in str(workflow.events)
        return
    c.test_connection(SETTINGS)
    c.test_connection(SETTINGS)
    assert len(workflow.connections) == 1
    assert actions(workflow) == ["version", "version"]
    assert not c.items and c.review_id is None
    assert not list(workflow.paths[0].parent.glob("*.inkly.json"))
    assert before == {path: path.read_bytes() for path in workflow.paths}
    install = [data for kind, data in workflow.events if kind == "hpc_install"][-1]
    if probe in {"old", "missing"}:
        # An unusable copy is not a notice: the window offers to update it.
        assert not [kind for kind, _ in workflow.events if kind == "notice"]
        assert install["state"] == "outdated" and install["offer"] is True
        message = [data for kind, data in workflow.events if kind == "message"][-1]
        expected = {
            "old": "too old to report its version",
            "missing": "lacks operations this Desktop",
        }[probe]
        assert expected in message and "No Slurm command was run" in message
        if probe == "missing":
            assert "prepare, submit, fetch" in message
        return
    notice = [data for kind, data in workflow.events if kind == "notice"][-1]
    expected = {
        "current": "supports what this Desktop",
        "older-release": "Inkly (0.4.0) supports what this Desktop",
        "no-cancel": "Not available until Inkly on the HPC is updated (Connection settings): Cancel job.",
    }[probe]
    assert expected in notice
    assert "differ" not in notice and "compatib" not in notice
    assert install["offer"] is False and install["version"] == workflow.endpoint.get(
        "version", "0.4.2"
    )
    if probe in {"current", "older-release"}:
        assert "Not available" not in notice
    # An earlier compatible release is not a warning; the update is mentioned.
    assert ("can be installed on the HPC" in notice) == (install["state"] == "outdated")


def recorded(workflow, path):
    return json.loads(path.with_suffix(".gjf.inkly.json").read_text())["bundle"]


def hpc_job(bundle, state, name="sample_1.gjf", ended_days=None, job_id="123"):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return {
        "bundle": bundle,
        "name": name,
        "job_id": job_id,
        "submission": "PREPARED" if state == "PREPARED" else "SUBMITTED",
        "state": state,
        # Older than the Job history default (30 days), away from its edge.
        "prepared_at": (now - timedelta(days=45)).isoformat(),
        "submitted_at": (now - timedelta(days=45)).isoformat(),
        "ended_at": None
        if ended_days is None
        else (now - timedelta(days=ended_days)).isoformat(),
    }


def input_copy(name, data):
    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode(),
    }


def found_event(workflow):
    return [data for kind, data in workflow.events if kind == "jobs_found"][-1]


def test_load_my_jobs_finds_recent_files_and_lists_the_others(workflow):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    c.preview(w.paths, SETTINGS)
    first, second = (recorded(w, path) for path in w.paths)
    w.endpoint["jobs"] = [
        hpc_job(first, "PENDING"),
        hpc_job(second, "COMPLETED", "sample_2.gjf", ended_days=1),
        hpc_job("c" * 32, "RUNNING", "elsewhere.gjf"),
        hpc_job("d" * 32, "COMPLETED", "old.gjf", ended_days=10),
        hpc_job("e" * 32, "PREPARED", "never.gjf", job_id=""),
    ]
    before = sorted(p.name for p in w.paths[0].parent.iterdir())
    c.load_jobs(SETTINGS)
    found = found_event(w)
    assert found["paths"] == [str(path.resolve()) for path in w.paths]
    # Only the recent job Desktop cannot find here needs a copy.
    assert [job["bundle"] for job in found["restore"]] == ["c" * 32]
    assert found["replaced"] == 0 and not found["from_history"]
    assert sorted(p.name for p in w.paths[0].parent.iterdir()) == before
    assert len(w.connections) == 1
    assert actions(w)[-2:] == ["version", "jobs"]
    c.execute("close")
    events = c.journal.take()
    load = [e["payload"] for e in events if e["event_type"] == "desktop_jobs"]
    assert {"action": "load", "count": 3}.items() <= load[-1].items()
    # Counts only: no file or job names in diagnostics.
    assert "elsewhere" not in json.dumps(events) and "sample_" not in json.dumps(events)


def test_a_run_again_replacement_is_not_loaded_again(workflow):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    c.preview(w.paths[:1], SETTINGS)
    old = recorded(w, w.paths[0])
    # As after Run again: the file's receipt now records a newer bundle.
    receipt = w.paths[0].with_suffix(".gjf.inkly.json")
    receipt.write_text(
        json.dumps({**json.loads(receipt.read_text()), "bundle": "f" * 32})
    )
    c.index.replaced(old)
    w.endpoint["jobs"] = [hpc_job(old, "FAILED", ended_days=1)]
    c.load_jobs(SETTINGS)
    found = found_event(w)
    assert found["paths"] == [] and found["restore"] == [] and found["replaced"] == 1
    # Chosen in Job history, the old job gets a copy of its own.
    c.load_jobs(SETTINGS, [old])
    found = found_event(w)
    assert [job["bundle"] for job in found["restore"]] == [old]
    assert found["from_history"]


def test_copies_of_jobs_from_elsewhere_work_like_added_files(workflow, tmp_path):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    data = b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nElsewhere\n\n0 1\nH 0 0 0\n\n"
    bundle = "c" * 32
    w.endpoint["inputs"] = {bundle: input_copy("elsewhere.gjf", data)}
    w.records[bundle] = {
        "name": "elsewhere.gjf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "script_sha256": "abc",
        "stages": 1,
        "submission": "SUBMITTED",
        "state": "PENDING",
        "job_id": "456",
        "resources": {
            "cpus": 2,
            "slurm_memory_mib": 1024,
            "gaussian_memory_mb": 768,
            "hours": 2,
        },
    }
    folder = tmp_path / "copies"
    job = hpc_job(bundle, "PENDING", "elsewhere.gjf")
    c.restore_jobs([job], folder, SETTINGS)
    restored = [data for kind, data in w.events if kind == "jobs_restored"][-1]
    path = folder / f"elsewhere-{bundle[:8]}" / "elsewhere.gjf"
    assert restored["paths"] == [str(path)]
    assert path.read_bytes() == data
    receipt = json.loads(path.with_suffix(".gjf.inkly.json").read_text())
    assert receipt == {
        "host": SETTINGS["host"],
        "ink_path": SETTINGS["ink_path"],
        "bundle": bundle,
        "auth": SETTINGS["auth"],
        "output": str((path.parent / "returned").resolve()),
    }
    assert c.index.locate(bundle) == ("found", path.resolve())
    # Loading it again reuses the copy instead of writing another.
    c.restore_jobs([job], folder, SETTINGS)
    assert [data for kind, data in w.events if kind == "jobs_restored"][-1] == restored
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "elsewhere.gjf",
        "elsewhere.gjf.inkly.json",
    ]
    # The copy previews like any added file: its recorded job, nothing new.
    c.preview([path], SETTINGS)
    rows = [data for kind, data in w.events if kind == "row"]
    assert rows[-1]["state"] == "PENDING" and rows[-1]["job_id"] == "456"
    assert "prepare" not in actions(w)


@pytest.mark.parametrize("problem", ["checksum", "unsafe-name", "existing-file"])
def test_bad_copies_are_reported_and_never_written(workflow, tmp_path, problem):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    bundle, name = "c" * 32, "elsewhere.gjf"
    copy = input_copy(name, b"%mem=1GB\n# hf\n\nx\n\n0 1\nH 0 0 0\n\n")
    if problem == "checksum":
        copy["sha256"] = "0" * 64
    elif problem == "unsafe-name":
        name = copy["name"] = "../outside.gjf"
    w.endpoint["inputs"] = {bundle: copy}
    folder = tmp_path / "copies"
    if problem == "existing-file":
        target = folder / f"elsewhere-{bundle[:8]}" / name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"researcher's own different file")
    c.restore_jobs([hpc_job(bundle, "PENDING", name)], folder, SETTINGS)
    restored = [data for kind, data in w.events if kind == "jobs_restored"][-1]
    assert restored["paths"] == []
    assert any("was not copied" in data for kind, data in w.events if kind == "message")
    assert not (tmp_path / "outside.gjf").exists()
    if problem == "existing-file":
        assert target.read_bytes() == b"researcher's own different file"
        assert not target.with_suffix(".gjf.inkly.json").exists()
    else:
        assert not list(folder.rglob("*.inkly.json"))


def test_load_finds_unknown_jobs_beside_receipts_in_folders_used_before(
    workflow, tmp_path
):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    # Submitted before Desktop kept its list of files: only the receipt
    # beside the input records the job.
    old = tmp_path / "project" / "old.gjf"
    old.parent.mkdir()
    old.write_text("input")
    old.with_suffix(".gjf.inkly.json").write_text(json.dumps({"bundle": "c" * 32}))
    w.endpoint["jobs"] = [hpc_job("c" * 32, "PENDING", "old.gjf")]
    c.load_jobs(SETTINGS)
    assert found_event(w)["paths"] == [] and len(found_event(w)["restore"]) == 1
    # Once a file from that folder has been added, the job is found there.
    c.index.remember_folders([old.parent])
    c.load_jobs(SETTINGS)
    assert found_event(w)["paths"] == [str(old)] and found_event(w)["restore"] == []
    assert c.index.locate("c" * 32) == ("found", old.resolve())


def test_search_jobs_finds_inputs_in_a_chosen_folder_without_the_hpc(
    workflow, tmp_path
):
    w, c = workflow, workflow.controller
    root = tmp_path / "research"
    old = root / "2026" / "batch" / "old.gjf"
    old.parent.mkdir(parents=True)
    old.write_text("input")
    old.with_suffix(".gjf.inkly.json").write_text(json.dumps({"bundle": "c" * 32}))
    missing = [
        hpc_job("c" * 32, "PENDING", "old.gjf"),
        hpc_job("d" * 32, "PENDING", "gone.gjf"),
    ]
    c.request("search_jobs", missing, str(root))
    c.connect(SETTINGS)
    before = len(w.connections)
    c.handle_requests()  # Inside a watch, no connection is needed or checked.
    searched = [data for kind, data in w.events if kind == "jobs_searched"][-1]
    assert searched["paths"] == [str(old)] and searched["complete"]
    assert [job["name"] for job in searched["missing"]] == ["gone.gjf"]
    assert len(w.connections) == before and "jobs" not in actions(w)
    # Next time the folder is checked automatically.
    assert old.parent.resolve() in c.index.folders()
    c.execute("close")
    events = c.journal.take()
    assert {"action": "search", "count": 1}.items() <= [
        e["payload"] for e in events if e["event_type"] == "desktop_jobs"
    ][-1].items()
    assert "old.gjf" not in json.dumps(events) and "research" not in json.dumps(events)


def test_job_history_lists_every_recorded_job(workflow):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs"]
    w.endpoint["jobs"] = [
        hpc_job("a" * 32, "COMPLETED", ended_days=40),
        hpc_job("b" * 32, "CANCELLED by 1000", ended_days=2),
    ]
    c.history(SETTINGS)
    listing = [data for kind, data in w.events if kind == "history"][-1]
    assert [job["bundle"] for job in listing["jobs"]] == ["a" * 32, "b" * 32]


@pytest.mark.parametrize("operation", ["load_jobs", "history"])
def test_job_lists_explain_an_older_hpc_copy(workflow, operation):
    w, c = workflow, workflow.controller
    getattr(c, operation)(SETTINGS)
    message = [data for kind, data in w.events if kind == "message"][-1]
    assert "needs an updated HPC copy of Inkly" in message
    offer = [data for kind, data in w.events if kind == "hpc_install"][-1]
    assert offer["state"] == "outdated" and offer["offer"] is True
    assert not [kind for kind, _ in w.events if kind in {"jobs_found", "history"}]
    assert "jobs" not in actions(w)


def test_copying_explains_an_older_hpc_copy(workflow, tmp_path):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs"]
    c.restore_jobs([hpc_job("c" * 32, "PENDING")], tmp_path / "copies", SETTINGS)
    assert "needs an updated HPC copy" in [d for k, d in w.events if k == "notice"][-1]
    assert [d for k, d in w.events if k == "jobs_restored"][-1] == {"paths": []}
    assert not (tmp_path / "copies").exists()


def test_job_lists_run_inside_the_watch_on_its_connection(workflow):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    c.connect(SETTINGS)
    c.request("load_jobs", SETTINGS)
    c.request("history", SETTINGS)
    c.request("load_jobs", {**SETTINGS, "host": "someone@elsewhere"})
    c.handle_requests()
    kinds = [kind for kind, _ in w.events]
    assert "jobs_found" in kinds and "history" in kinds
    assert "different HPC connection" in [d for k, d in w.events if k == "notice"][-1]
    assert len(w.connections) == 1


def test_connection_does_not_consume_an_existing_preview(workflow):
    c = workflow.controller
    c.preview(workflow.paths, SETTINGS)
    review = c.review_id
    c.test_connection(SETTINGS)
    assert c.review_id == review
    assert len(workflow.connections) == 1
    assert actions(workflow)[-1] == "version"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Tk")
def test_native_connection_button_needs_no_added_files(workflow, tmp_path):
    import tkinter as tk
    import tensor_desktop as gui

    root = tk.Tk()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda *a, **k: workflow.controller,
        settings_path=tmp_path / "settings.json",
    )
    try:
        app.settings = dict(SETTINGS)
        assert not app.files
        app.buttons()
        assert str(app.test_connection_button["state"]) == "normal"
        app.test_connection()
        assert app.action == "test_connection"
        assert str(app.test_connection_button["state"]) == "disabled"
    finally:
        app.busy = False
        app.close()


def recorded_job(workflow, path, state, job_id="123"):
    """Mark a previewed file as submitted, with the given Slurm state."""
    record_for(workflow, path).update(
        submission="SUBMITTED", state=state, job_id=job_id
    )


def messages(workflow):
    return [data for kind, data in workflow.events if kind == "message"]


def rows_for(workflow, path):
    return [
        data
        for kind, data in workflow.events
        if kind == "row" and data["path"] == str(path)
    ]


def test_logging_records_safe_workflow_and_one_submission_per_file(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    w.controller.submit(w.controller.review_id, True)
    w.controller.execute("close")
    events = w.controller.journal.take()
    types = {e["event_type"] for e in events}
    assert types >= {
        "desktop_session_start",
        "desktop_session_end",
        "desktop_connection",
        "desktop_preview",
        "desktop_prepared",
        "desktop_approval",
        "desktop_submission",
        "desktop_job_state",
        "desktop_retrieval",
    }
    assert actions(w).count("submit") == 1
    connection_states = [
        e["payload"].get("status")
        for e in events
        if e["event_type"] == "desktop_connection"
    ]
    assert connection_states[-1] == "closed"
    text = json.dumps(events)
    assert str(w.paths[0]) not in text
    assert "sample_1" not in text
    assert "synthetic normal output" not in text
    submissions = [
        e["payload"] for e in events if e["event_type"] == "desktop_submission"
    ]
    assert [e["status"] for e in submissions] == ["attempt", "confirmed"]
    assert submissions[1]["job_id"] == 123


def test_logging_exception_cannot_change_confirmation_or_retry_submission(
    workflow, monkeypatch
):
    w = workflow

    def fail(*args):
        raise OSError("PRIVATE_SENTINEL")

    with monkeypatch.context() as patch:
        patch.setattr(w.controller.journal, "emit", fail)
        w.controller.preview(w.paths[:1], SETTINGS)
        with pytest.raises(ValueError, match="confirmation"):
            w.controller.submit(w.controller.review_id, False)
        assert "submit" not in actions(w)
        review = w.controller.review_id
        w.controller.submit(review, True)
        with pytest.raises(ValueError, match="confirmation"):
            w.controller.submit(review, True)
        assert actions(w).count("submit") == 1


def test_logging_records_failed_preview_without_error_text(workflow):
    w = workflow
    w.controller.execute("preview", [], SETTINGS)
    values = w.controller.journal.take()
    errors = [e["payload"] for e in values if e["event_type"] == "desktop_error"]
    assert len(errors) == 1
    assert errors[0]["action"] == "preview"
    assert errors[0]["code"] == "validation"
    assert "Add at least" not in json.dumps(values)


def test_missing_bundle_requests_explicit_recovery_without_changing_receipt(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    receipt_path = w.paths[0].with_suffix(".gjf.inkly.json")
    original_receipt = receipt_path.read_bytes()
    original_input = w.paths[0].read_bytes()
    old_bundle = json.loads(original_receipt)["bundle"]

    w.records.pop(old_bundle)
    w.missing_bundles.add(old_bundle)
    w.calls.clear()
    w.events.clear()

    w.controller.preview(w.paths[:1], SETTINGS)

    assert actions(w) == ["status"]
    assert receipt_path.read_bytes() == original_receipt
    assert w.paths[0].read_bytes() == original_input
    assert "submit" not in actions(w)
    recovery = [data for kind, data in w.events if kind == "recovery_required"]
    assert len(recovery) == 1
    assert recovery[0]["files"] == [w.paths[0].name]
    assert recovery[0]["bundles"] == [old_bundle]


def test_approved_recovery_archives_old_receipt_and_reprepares_original_in_place(
    workflow,
):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    receipt_path = w.paths[0].with_suffix(".gjf.inkly.json")
    original_receipt = receipt_path.read_bytes()
    original_input = w.paths[0].read_bytes()
    old_bundle = json.loads(original_receipt)["bundle"]

    w.records.pop(old_bundle)
    w.missing_bundles.add(old_bundle)
    w.calls.clear()
    w.events.clear()

    # This bundle ID represents the user's explicit approval in the desktop dialog.
    w.controller.preview(w.paths[:1], SETTINGS, [old_bundle])

    new_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archives = list(w.paths[0].parent.glob("*.inkly.json.orphaned-*.bak"))
    assert new_receipt["bundle"] != old_bundle
    assert len(archives) == 1
    assert archives[0].read_bytes() == original_receipt
    assert w.paths[0].read_bytes() == original_input
    assert actions(w) == ["status", "prepare"]
    assert "submit" not in actions(w)
    assert w.controller.review()["count"] == 1
    assert w.controller.review()["recovered"] == [w.paths[0].name]
    assert any(
        kind == "message" and "No Slurm job was submitted" in data
        for kind, data in w.events
    )


def test_failed_remote_recovery_keeps_new_receipt_and_archived_old_receipt(
    workflow,
):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    receipt_path = w.paths[0].with_suffix(".gjf.inkly.json")
    original_receipt = receipt_path.read_bytes()
    old_bundle = json.loads(original_receipt)["bundle"]
    w.records.pop(old_bundle)
    w.missing_bundles.add(old_bundle)

    original_exchange = w.controller.session.exchange

    def fail_new_prepare(request, *, receipt=None):
        if request["action"] == "prepare":
            raise ValueError("Synthetic connection interruption")
        return original_exchange(request, receipt=receipt)

    w.controller.session.exchange = fail_new_prepare
    w.controller.execute("preview", w.paths[:1], SETTINGS, [old_bundle])

    new_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    archives = list(w.paths[0].parent.glob("*.inkly.json.orphaned-*.bak"))
    assert new_receipt["bundle"] != old_bundle
    assert len(archives) == 1
    assert archives[0].read_bytes() == original_receipt
    assert any(
        kind == "error" and "Synthetic connection interruption" in data
        for kind, data in w.events
    )
    assert "submit" not in actions(w)


def test_recovery_of_one_file_in_a_batch_leaves_other_receipts_intact(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    receipt_paths = [path.with_suffix(".gjf.inkly.json") for path in w.paths]
    old_receipts = [path.read_bytes() for path in receipt_paths]
    old_bundles = [json.loads(raw)["bundle"] for raw in old_receipts]
    original_inputs = [path.read_bytes() for path in w.paths]

    w.records.pop(old_bundles[0])
    w.missing_bundles.add(old_bundles[0])
    w.calls.clear()
    w.events.clear()

    w.controller.preview(w.paths, SETTINGS)
    recovery = [data for kind, data in w.events if kind == "recovery_required"]
    assert recovery[0]["files"] == [w.paths[0].name]
    assert receipt_paths[0].read_bytes() == old_receipts[0]
    assert receipt_paths[1].read_bytes() == old_receipts[1]

    w.controller.preview(w.paths, SETTINGS, [old_bundles[0]])
    new_receipts = [json.loads(path.read_text()) for path in receipt_paths]
    assert new_receipts[0]["bundle"] != old_bundles[0]
    assert new_receipts[1]["bundle"] == old_bundles[1]
    assert [path.read_bytes() for path in w.paths] == original_inputs
    assert w.controller.review()["count"] == 2
    assert w.controller.review()["recovered"] == [w.paths[0].name]
    assert "submit" not in actions(w)


def test_unrelated_ssh_error_does_not_trigger_missing_bundle_recovery(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    w.events.clear()
    original = w.controller.session.exchange

    def interrupted(request, *, receipt=None):
        if request["action"] == "status":
            raise ValueError("SSH interrupted or timed out")
        return original(request, receipt=receipt)

    w.controller.session.exchange = interrupted
    w.controller.execute("preview", w.paths[:1], SETTINGS)

    assert not any(kind == "recovery_required" for kind, _ in w.events)
    assert any(kind == "error" and "SSH interrupted" in data for kind, data in w.events)


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


def test_cancel_only_pending_jobs_after_checking_the_endpoint(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "123")
    recorded_job(w, w.paths[1], "PENDING", "124")
    w.calls.clear()
    w.events.clear()
    w.controller.cancel_jobs([w.paths[0]], SETTINGS)
    assert actions(w) == ["status", "version", "cancel"]
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"
    assert record_for(w, w.paths[1])["state"] == "PENDING"
    assert rows_for(w, w.paths[0])[-1]["state"] == "CANCELLED by 1000"
    assert "Cancelled sample_1.gjf: job 123 is CANCELLED." in messages(w)
    assert any("never resubmits" in text for text in messages(w))
    w.controller.execute("close")
    events = w.controller.journal.take()
    cancels = [e["payload"] for e in events if e["event_type"] == "desktop_cancel"]
    assert [e["status"] for e in cancels] == ["approved", "requested"]
    assert cancels[1]["job_id"] == 123
    assert "sample_1" not in json.dumps(events)


def notices(workflow):
    return [data for kind, data in workflow.events if kind == "notice"]


@pytest.mark.parametrize(
    "state, explanation",
    [
        ("CONFIGURING", "has already started (CONFIGURING)"),
        ("COMPLETED", "job 124 already completed, so there was nothing to cancel"),
        ("CANCELLED by 7", "job 124 was already cancelled"),
        ("FAILED", "job 124 already ended (FAILED)"),
        ("SUBMISSION_UNKNOWN", "cannot confirm a Slurm job"),
    ],
)
def test_cancel_explains_jobs_that_are_not_waiting_and_cancels_the_rest(
    workflow, state, explanation
):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "123")
    recorded_job(w, w.paths[1], state, "124")
    w.calls.clear()
    w.events.clear()
    w.controller.cancel_jobs(w.paths, SETTINGS)  # No exception.
    assert [bundle for bundle, action in w.calls if action == "cancel"] == [
        json.loads(w.paths[0].with_suffix(".gjf.inkly.json").read_text())["bundle"]
    ]
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"
    assert "submit" not in actions(w)
    assert len(notices(w)) == 1 and explanation in notices(w)[0]
    assert "sample_2.gjf" in notices(w)[0] and "sample_1.gjf" not in notices(w)[0]
    assert w.controller.session is not None  # The connection stays open.


def test_cancelling_a_finished_job_shows_a_notice_and_keeps_the_connection(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "COMPLETED", "2406955")
    w.events.clear()
    connections = len(w.connections)
    w.controller.execute("cancel_jobs", [w.paths[0]], SETTINGS)
    assert not [data for kind, data in w.events if kind == "error"]
    assert notices(w) == [
        "sample_1.gjf: job 2406955 already completed, so there was nothing to "
        "cancel. Check & return results brings back its log."
    ]
    assert rows_for(w, w.paths[0])[-1]["state"] == "COMPLETED"
    assert ("done", "cancel_jobs") in w.events
    assert "cancel" not in actions(w)
    assert w.controller.session is not None and len(w.connections) == connections


def test_job_that_starts_before_the_cancel_arrives_is_explained(workflow, monkeypatch):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "123")

    def starts_now(operation, receipt):
        record_for(w, w.paths[0])["state"] = "CONFIGURING"
        return True

    monkeypatch.setattr(w.controller, "endpoint_supports", starts_now)
    w.events.clear()
    w.controller.cancel_jobs([w.paths[0]], SETTINGS)
    assert "has already started (CONFIGURING)" in notices(w)[0]
    assert rows_for(w, w.paths[0])[-1]["state"] == "CONFIGURING"


def test_cancel_needs_a_saved_handoff(workflow):
    w = workflow
    with pytest.raises(ValueError, match="No saved handoff"):
        w.controller.cancel_jobs([w.paths[0]], SETTINGS)
    assert not w.calls and not w.connections


def test_older_hpc_endpoint_explains_and_cancels_nothing(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "2406999")
    w.endpoint["capabilities"] = None
    w.calls.clear()
    w.events.clear()
    w.controller.cancel_jobs([w.paths[0]], SETTINGS)
    assert actions(w) == ["status", "version"]
    assert "too old to cancel" in notices(w)[0]
    assert "scancel 2406999" in notices(w)[0]
    assert record_for(w, w.paths[0])["state"] == "PENDING"


def test_monitor_treats_cancelled_as_final_and_keeps_watching_others(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "CANCELLED by 1000", "1")
    recorded_job(w, w.paths[1], "RUNNING", "2")
    w.controller.monitor()
    assert rows_for(w, w.paths[1])[-1]["state"] == "RETURNED"
    assert (w.paths[1].parent / "returned" / "sample_2.log").exists()
    assert not (w.paths[0].parent / "returned" / "sample_1.log").exists()
    assert any("was cancelled" in text and "Run again" in text for text in messages(w))
    assert "submit" not in actions(w)


def test_cancel_while_watching_uses_the_open_connection(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    recorded_job(w, w.paths[1], "RUNNING", "2")
    connections = len(w.connections)
    w.events.clear()
    w.controller.request_cancel([w.paths[0]], SETTINGS)
    w.controller.monitor()
    assert len(w.connections) == connections  # No reconnect, no new sign-in.
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"
    assert rows_for(w, w.paths[1])[-1]["state"] == "RETURNED"
    assert "Cancelled sample_1.gjf: job 1 is CANCELLED." in messages(w)
    # The watch does not repeat the cancellation it just made.
    assert not any("was cancelled. Inkly never resubmits" in m for m in messages(w))
    assert "submit" not in actions(w)


def test_cancel_request_wakes_a_waiting_watch(workflow):
    import threading
    import time

    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    recorded_job(w, w.paths[1], "PENDING", "2")
    w.controller.interval = 30
    watch = threading.Thread(target=w.controller.monitor, daemon=True)
    watch.start()
    deadline = time.monotonic() + 5
    while actions(w).count("status") < 2 and time.monotonic() < deadline:
        time.sleep(0.01)  # Let the first check finish and the 30 s wait begin.
    w.controller.request_cancel(w.paths, SETTINGS)
    watch.join(timeout=5)
    assert not watch.is_alive(), "the watch should wake and finish, not wait 30 s"
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"
    assert record_for(w, w.paths[1])["state"] == "CANCELLED by 1000"


def test_stop_watching_drops_cancel_requests_not_yet_handled(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    w.controller.request_cancel([w.paths[0]], SETTINGS)
    w.controller.stop()
    assert w.controller.requests.empty()
    assert w.controller.wake.is_set()
    assert record_for(w, w.paths[0])["state"] == "PENDING"


def test_failed_action_drops_cancel_requests_not_yet_handled(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    w.controller.request_cancel([w.paths[0]], SETTINGS)
    w.controller.execute("submit", "wrong-review", True)  # Fails before watching.
    assert any(kind == "error" for kind, _ in w.events)
    assert w.controller.requests.empty()
    assert record_for(w, w.paths[0])["state"] == "PENDING"


def test_watch_cancel_for_another_connection_keeps_watching(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    recorded_job(w, w.paths[1], "RUNNING", "2")
    w.controller.target = ("someone@elsewhere", "~/other/ink", "configured")
    w.events.clear()
    w.controller.request_cancel([w.paths[0]], SETTINGS)
    w.controller.handle_requests()
    assert "different HPC connection" in notices(w)[0]
    assert "cancel" not in actions(w)
    assert record_for(w, w.paths[0])["state"] == "PENDING"


def events_of(workflow, kind):
    return [data for event, data in workflow.events if event == kind]


def test_preview_and_submit_a_new_file_while_watching_on_one_connection(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    connections = len(w.connections)
    w.events.clear()
    # The researcher adds sample_2 to the list and previews it during the watch.
    w.controller.request("preview", w.paths, SETTINGS)
    assert w.controller.handle_requests() is True
    review = w.controller.review()
    assert review["count"] == 1 and review["jobs"][0]["name"] == "sample_2.gjf"
    w.controller.request("submit", w.controller.review_id, True)
    assert w.controller.handle_requests() is True  # Returns; no nested watch.
    assert actions(w).count("submit") == 1
    assert record_for(w, w.paths[1])["submission"] == "SUBMITTED"
    assert record_for(w, w.paths[0])["state"] == "PENDING"  # Untouched.
    assert len(w.connections) == connections  # No reconnect, no new sign-in.


def test_request_problems_are_reported_without_ending_the_watch(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    w.events.clear()
    w.controller.request("submit", "not-the-current-review", True)
    assert w.controller.handle_requests() is False
    assert (
        "needs confirmation of the current preview" in events_of(w, "request_error")[0]
    )
    assert "submit" not in actions(w)
    assert w.controller.session is not None
    with pytest.raises(ValueError, match="cannot run while watching"):
        w.controller.request("close")


def test_watch_takes_preview_submit_and_cancel_requests_in_turn(workflow):
    import threading
    import time

    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    w.controller.interval = 30
    connections = len(w.connections)
    watch = threading.Thread(target=w.controller.monitor, daemon=True)
    watch.start()

    def wait_for(condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert condition()

    wait_for(lambda: actions(w).count("status") >= 1)
    first_review = w.controller.review_id
    w.controller.request("preview", w.paths, SETTINGS)
    wait_for(lambda: w.controller.review_id not in (None, first_review))
    w.controller.request("submit", w.controller.review_id, True)
    wait_for(lambda: "submit" in actions(w))
    wait_for(lambda: record_for(w, w.paths[1])["submission"] == "SUBMITTED")
    record_for(w, w.paths[1])["state"] = "PENDING"
    w.controller.request_cancel(w.paths, SETTINGS)
    watch.join(timeout=5)
    assert not watch.is_alive()
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"
    assert record_for(w, w.paths[1])["state"] == "CANCELLED by 1000"
    assert len(w.connections) == connections


def test_stop_watching_keeps_the_connection_so_watching_can_resume(workflow):
    import threading
    import time

    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "PENDING", "1")
    connections = len(w.connections)
    w.controller.interval = 30
    watch = threading.Thread(target=w.controller.monitor, daemon=True)
    watch.start()
    deadline = time.monotonic() + 5
    while actions(w).count("status") < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    w.controller.request_cancel([w.paths[0]], SETTINGS)  # Dropped by the stop.
    w.controller.pause()
    watch.join(timeout=5)
    assert not watch.is_alive()
    assert w.controller.session is not None  # Still connected.
    assert w.controller.requests.empty() and not w.controller.paused.is_set()
    assert "cancel" not in actions(w)
    assert any("Stopped watching" in text for text in messages(w))
    # Watching again needs no new connection or sign-in.
    w.controller.interval = 0
    record_for(w, w.paths[0])["state"] = "RUNNING"
    w.controller.monitor()
    assert rows_for(w, w.paths[0])[-1]["state"] == "RETURNED"
    assert len(w.connections) == connections


def test_stop_during_submit_lets_the_submission_finish_then_stops(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    w.controller.pause()  # Clicked while jobs were still being submitted.
    w.controller.submit(w.controller.review_id, True)
    assert record_for(w, w.paths[0])["submission"] == "SUBMITTED"
    assert any("Stopped watching" in text for text in messages(w))
    assert w.controller.session is not None


def test_request_arriving_as_the_watch_ends_is_dropped_with_a_notice(
    workflow, monkeypatch
):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "RUNNING", "1")
    real_monitor = w.controller.monitor

    def monitor_then_late_click():
        real_monitor()
        w.controller.request("preview", w.paths, SETTINGS)  # Clicked as it ended.

    monkeypatch.setattr(w.controller, "monitor", monitor_then_late_click)
    w.events.clear()
    w.controller.execute("monitor")
    assert w.controller.requests.empty()
    assert "finished before your last request ran" in notices(w)[0]
    assert ("done", "monitor") in w.events


def test_check_after_the_connection_closed_signs_in_again(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "RUNNING", "1")
    w.controller.close()
    connections = len(w.connections)
    w.controller.monitor()
    assert len(w.connections) == connections + 1
    assert rows_for(w, w.paths[0])[-1]["state"] == "RETURNED"


def test_cancelled_row_does_not_block_submitting_other_prepared_files(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "CANCELLED by 1000", "1")
    w.controller.preview(w.paths, SETTINGS)
    assert w.controller.review()["count"] == 1
    w.controller.submit(w.controller.review_id, True)
    assert actions(w).count("submit") == 1
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"


@pytest.mark.parametrize(
    "state, exit_code",
    [
        ("FAILED", "1:0"),
        ("TIMEOUT", "0:0"),
        ("OUT_OF_MEMORY", "0:125"),
        ("COMPLETED", "2:0"),
        ("NODE_FAIL", "0:0"),
    ],
)
def test_ended_rows_do_not_block_submitting_other_files(workflow, state, exit_code):
    # Load my jobs brings recent failures into the list; they have stopped, so
    # other prepared files can still be submitted beside them.
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], state, "1")
    record_for(w, w.paths[0])["exit_code"] = exit_code
    w.controller.preview(w.paths, SETTINGS)
    assert w.controller.review()["count"] == 1
    w.controller.submit(w.controller.review_id, True)
    assert actions(w).count("submit") == 1
    assert record_for(w, w.paths[0])["state"] == state


def test_watch_reports_an_ended_job_and_keeps_watching_the_others(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "TIMEOUT", "7")
    recorded_job(w, w.paths[1], "RUNNING", "8")  # The fake HPC completes it.
    w.controller.monitor()
    assert rows_for(w, w.paths[0])[-1]["state"] == "TIMEOUT"
    assert rows_for(w, w.paths[1])[-1]["state"] == "RETURNED"
    ended = [m for m in messages(w) if "ended as TIMEOUT" in m]
    assert len(ended) == 1 and "Job details" in ended[0]
    assert "Check complete" in messages(w)[-1]
    assert actions(w).count("fetch") == 1 and "submit" not in actions(w)


def test_watch_still_stops_for_a_job_that_may_or_may_not_have_run(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "UNKNOWN", "7")
    with pytest.raises(ValueError, match="inspect this job"):
        w.controller.monitor()


@pytest.mark.parametrize("state", ["UNKNOWN", "SUBMISSION_UNKNOWN", "BOGUS"])
def test_uncertain_rows_still_block_new_submissions(workflow, state):
    # A job that may or may not have run is inspected before anything else.
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], state, "1")
    w.controller.preview(w.paths, SETTINGS)
    with pytest.raises(ValueError, match="unresolved job state"):
        w.controller.submit(w.controller.review_id, True)
    assert "submit" not in actions(w)


def test_run_again_archives_receipt_and_prepares_without_submitting(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    receipt_path = w.paths[0].with_suffix(".gjf.inkly.json")
    original = receipt_path.read_bytes()
    recorded_job(w, w.paths[0], "CANCELLED by 1000", "1")
    w.calls.clear()
    w.events.clear()
    w.controller.rerun([w.paths[0]], w.paths, {**SETTINGS, "hours": 3})
    archives = list(w.paths[0].parent.glob("sample_1.gjf.inkly.json.rerun-*.bak"))
    assert len(archives) == 1 and archives[0].read_bytes() == original
    new_receipt = json.loads(receipt_path.read_text())
    assert new_receipt["bundle"] != json.loads(original)["bundle"]
    assert {key: new_receipt[key] for key in ("host", "ink_path", "auth")} == {
        key: json.loads(original)[key] for key in ("host", "ink_path", "auth")
    }
    assert w.records[new_receipt["bundle"]]["resources"]["hours"] == 3
    assert "submit" not in actions(w)
    # One status check before any change, then the new bundle, then the refresh.
    assert actions(w)[:2] == ["status", "prepare"]
    assert actions(w).count("prepare") == 1
    review = w.controller.review()
    assert review["count"] == 2  # The new bundle plus the other unsubmitted file.
    assert rows_for(w, w.paths[0])[-1]["state"] == "PREPARED"
    assert any("Nothing was submitted" in text for text in messages(w))
    w.controller.execute("close")
    reruns = [
        e["payload"]
        for e in w.controller.journal.take()
        if e["event_type"] == "desktop_rerun"
    ]
    assert [e["status"] for e in reruns] == ["approved", "prepared"]


@pytest.mark.parametrize(
    "state, advice",
    [
        ("PENDING", "Cancel it first"),
        ("SUSPENDED", "Cancel it first"),
        ("COMPLETED", "result is kept"),
        ("SUBMISSION_UNKNOWN", "may still be running"),
        ("UNKNOWN", "may still be running"),
    ],
)
def test_run_again_refuses_active_finished_or_unconfirmed_jobs(workflow, state, advice):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    receipt_path = w.paths[0].with_suffix(".gjf.inkly.json")
    original = receipt_path.read_bytes()
    recorded_job(w, w.paths[0], "CANCELLED by 1000", "1")
    recorded_job(w, w.paths[1], state, "2")
    w.calls.clear()
    with pytest.raises(ValueError, match=advice):
        w.controller.rerun(w.paths, w.paths, SETTINGS)
    assert receipt_path.read_bytes() == original
    assert not list(w.paths[0].parent.glob("*.rerun-*.bak"))
    assert "prepare" not in actions(w) and "submit" not in actions(w)


def test_run_again_never_overwrites_a_returned_log(workflow):
    w = workflow
    w.controller.preview(w.paths[:1], SETTINGS)
    recorded_job(w, w.paths[0], "FAILED", "1")
    output = w.paths[0].parent / "returned" / "sample_1.log"
    output.parent.mkdir()
    output.write_text("earlier result")
    with pytest.raises(ValueError, match="will not be overwritten"):
        w.controller.rerun(w.paths[:1], w.paths[:1], SETTINGS)
    assert output.read_text() == "earlier result"
    assert "prepare" not in actions(w)[1:]


def test_edited_input_is_shown_never_submitted_and_can_run_again(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    old_bundle = json.loads(w.paths[0].with_suffix(".gjf.inkly.json").read_text())[
        "bundle"
    ]
    w.paths[0].write_bytes(w.paths[0].read_bytes() + b"! edited\n")
    w.events.clear()
    w.controller.preview(w.paths, SETTINGS)  # No longer stops the whole list.
    row = rows_for(w, w.paths[0])[-1]
    assert row["input_changed"] and row["state"] == "PREPARED"
    assert any("was edited after it was prepared" in text for text in messages(w))
    assert w.controller.review()["count"] == 1
    w.controller.submit(w.controller.review_id, True)
    submitted = [bundle for bundle, action in w.calls if action == "submit"]
    assert submitted and old_bundle not in submitted
    assert w.records[old_bundle]["submission"] == "PREPARED"

    w.controller.rerun([w.paths[0]], w.paths, SETTINGS)
    receipt = json.loads(w.paths[0].with_suffix(".gjf.inkly.json").read_text())
    assert receipt["bundle"] != old_bundle
    assert (
        w.records[receipt["bundle"]]["sha256"]
        == hashlib.sha256(w.paths[0].read_bytes()).hexdigest()
    )
    assert not rows_for(w, w.paths[0])[-1]["input_changed"]
    assert w.controller.review()["count"] == 1


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
@pytest.mark.parametrize("accept", [False, True])
def test_native_window_requires_confirmation_before_recovery(
    workflow, tmp_path, monkeypatch, accept
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
        app.add_paths(w.paths[:1])
        w.controller.preview(w.paths[:1], SETTINGS)
        old_receipt = w.paths[0].with_suffix(".gjf.inkly.json")
        old_bundle = json.loads(old_receipt.read_text())["bundle"]
        w.records.pop(old_bundle)
        w.missing_bundles.add(old_bundle)
        w.events.clear()
        w.controller.preview(w.paths[:1], SETTINGS)
        for event in w.events:
            app.events.put(event)

        scheduled = []
        monkeypatch.setattr(app, "start", lambda *args: scheduled.append(args))
        monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: accept)
        app.drain()

        if accept:
            assert scheduled == [("preview", w.paths[:1], SETTINGS, [old_bundle])]
        else:
            assert not scheduled
            assert old_receipt.exists()
            assert "Recovery declined" in app.activity.get("1.0", "end")
    finally:
        app.close()


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
        # The list changed, so approval is gone; the remaining row keeps its details.
        assert len(app.files) == 1 and app.review is None
        assert str(app.submit_button["state"]) == "disabled"
        kept = app.table.item(str(w.paths[1]))["values"]
        assert kept[6] == "PREPARED" and kept[1] != "—"
        assert str(w.paths[0]) not in app.rows

        # Adding a file keeps the rows already shown and asks for a preview.
        extra = w.paths[0].parent / "sample_3.gjf"
        extra.write_bytes(w.paths[0].read_bytes())
        app.add_paths([extra])
        assert app.table.item(str(w.paths[1]))["values"] == kept
        assert app.table.item(str(extra))["values"][6] == "Not previewed"
        assert "1 file(s) not previewed yet" in app.overview["text"]
        assert str(app.submit_button["state"]) == "disabled"
    finally:
        app.close()


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_cancel_and_run_again_follow_selected_rows(
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
    first, second = (str(path) for path in w.paths)

    def show(path, **changes):
        app.events.put(("row", {**app.rows[path], **changes}))
        app.drain()

    try:
        app.settings = dict(SETTINGS)
        app.add_paths(w.paths)
        w.controller.preview(w.paths, SETTINGS)
        for event in w.events:
            app.events.put(event)
        app.drain()
        assert str(app.cancel_button["state"]) == "disabled"
        assert str(app.rerun_button["state"]) == "disabled"

        show(first, state="PENDING", job_id="123")
        app.table.selection_set(first)
        app.buttons()
        assert str(app.cancel_button["state"]) == "normal"
        assert str(app.rerun_button["state"]) == "disabled"

        scheduled, questions = [], []
        monkeypatch.setattr(app, "start", lambda *args: scheduled.append(args))

        def answer(value):
            def ask(title, text, **kwargs):
                questions.append((title, text, kwargs.get("default")))
                return value

            return ask

        monkeypatch.setattr(gui.messagebox, "askyesno", answer(False))
        app.cancel_button.invoke()
        assert not scheduled
        assert "job 123" in questions[-1][1] and questions[-1][2] == "no"
        assert "already started keeps running" in questions[-1][1]
        monkeypatch.setattr(gui.messagebox, "askyesno", answer(True))
        app.cancel_button.invoke()
        assert scheduled[-1] == ("cancel_jobs", [first], SETTINGS)

        show(first, state="CANCELLED by 1000")
        app.buttons()
        assert str(app.cancel_button["state"]) == "disabled"
        assert str(app.rerun_button["state"]) == "normal"
        app.rerun_button.invoke()
        assert scheduled[-1] == ("rerun", [first], [first, second], SETTINGS)
        assert "Nothing is submitted" in questions[-1][1]
        assert "CANCELLED" in questions[-1][1]

        # An unchanged PREPARED row is simply submitted; an edited one runs again.
        show(second, state="PREPARED")
        app.table.selection_set(second)
        app.buttons()
        assert str(app.rerun_button["state"]) == "disabled"
        show(second, input_changed=True)
        assert app.table.item(second)["values"][6] == "PREPARED (input edited)"
        app.buttons()
        assert str(app.rerun_button["state"]) == "normal"

        # Mixed selections and busy windows offer neither action.
        show(first, state="PENDING", job_id="123")
        app.table.selection_set([first, second])
        app.buttons()
        assert str(app.cancel_button["state"]) == "disabled"
        assert str(app.rerun_button["state"]) == "disabled"
        app.table.selection_set(first)
        app.busy = True
        app.buttons()
        assert str(app.cancel_button["state"]) == "disabled"

        # While watching submitted jobs, Cancel job stays available and goes to
        # the running watch on its open connection instead of starting anew.
        requests = []
        monkeypatch.setattr(
            w.controller, "request_cancel", lambda *args: requests.append(args)
        )
        app.action = "submit"
        app.buttons()
        assert str(app.cancel_button["state"]) == "normal"
        assert str(app.rerun_button["state"]) == "disabled"
        started = len(scheduled)
        app.cancel_button.invoke()
        assert requests == [([first], SETTINGS)]
        assert len(scheduled) == started
        app.busy, app.action = False, None

        # Expected outcomes are shown as information, not errors.
        shown = []
        monkeypatch.setattr(
            gui.messagebox, "showinfo", lambda *args, **kwargs: shown.append(args)
        )
        app.events.put(("notice", "job 2406955 already completed"))
        app.drain()
        assert shown == [("Inkly Desktop", "job 2406955 already completed")]
    finally:
        app.close()


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_window_collects_garbage_only_on_the_tk_thread(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    calls = []
    monkeypatch.setattr(
        gui,
        "gc",
        SimpleNamespace(
            disable=lambda: calls.append("disable"),
            enable=lambda: calls.append("enable"),
            collect=lambda *args: calls.append("collect"),
        ),
    )
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: workflow.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    try:
        # Automatic collection is off before any worker thread can trigger it.
        assert calls == ["disable"]
        app.drain()
        assert calls == ["disable"]  # Not due yet.
        app.next_collection = 0
        app.drain()
        assert calls == ["disable", "collect"]
        assert app.next_collection > 0
    finally:
        app.close()
    # Closing collects on the Tk thread and never re-enables automatic collection.
    assert calls == ["disable", "collect", "collect"]


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_new_work_goes_to_the_watch_and_stop_keeps_the_connection(
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
    requests, calls, scheduled = [], [], []
    monkeypatch.setattr(w.controller, "request", lambda *args: requests.append(args))
    monkeypatch.setattr(w.controller, "pause", lambda: calls.append("pause"))
    monkeypatch.setattr(w.controller, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(app, "start", lambda *args: scheduled.append(args))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    try:
        app.settings = dict(SETTINGS)
        app.add_paths(w.paths)
        w.controller.preview(w.paths, SETTINGS)
        for event in w.events:
            app.events.put(event)
        app.drain()
        app.busy, app.action = True, "monitor"  # Watching submitted jobs.
        app.buttons()
        state = {
            name: str(getattr(app, f"{name}_button")["state"])
            for name in ("add", "folder", "preview", "submit", "remove", "clear")
        }
        assert state == {
            "add": "normal",
            "folder": "normal",
            "preview": "normal",
            "submit": "normal",
            "remove": "disabled",
            "clear": "disabled",
        }
        assert str(app.settings_button["state"]) == "disabled"
        assert str(app.monitor_button["state"]) == "disabled"

        review_id = app.review["id"]
        app.submit_button.invoke()
        assert requests[-1] == ("submit", review_id, True)
        assert app.review is None and str(app.submit_button["state"]) == "disabled"
        app.preview_button.invoke()
        assert requests[-1] == ("preview", app.files, SETTINGS)
        assert not scheduled  # Nothing started a new action beside the watch.

        # Stop watching pauses and keeps the connection...
        app.stop_button.invoke()
        assert calls == ["pause"] and app.stop_deadline
        # ...and disconnects only if the same watch is still running later.
        app.force_stop(app.run_id + 1)
        assert calls == ["pause"]
        app.force_stop(app.run_id)
        assert calls == ["pause", "stop"]

        # When idle, Check & return results works even without a connection.
        app.busy, app.action = False, None
        w.controller.session = None
        app.buttons()
        assert str(app.monitor_button["state"]) == "normal"
    finally:
        w.controller.session = None
        app.close()


native_display = pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
SAMPLE_RESOURCES = {
    "cpus": 16,
    "slurm_memory_mib": 42326,
    "gaussian_memory_mb": 31744,
    "hours": 1,
}


def summary_row(path, state):
    return {"path": path, "state": state, "resources": dict(SAMPLE_RESOURCES)}


def queue_before_running(workflow, monkeypatch):
    """Report one PENDING status after sbatch, as Slurm did for a real job."""
    w = workflow

    class QueuedSession(backend.bridge.Session):
        def exchange(self, request, *, receipt):
            response = super().exchange(request, receipt=receipt)
            record = w.records[receipt["bundle"]]
            if request["action"] == "submit":
                record["state"] = "PENDING"
            elif response.get("state") == "PENDING":
                record["state"] = "RUNNING"
            return response

    monkeypatch.setattr(backend.bridge, "Session", QueuedSession)


def replay_overviews(events, review=None, rows=None):
    """Mirror the window's row/review bookkeeping without a display."""
    import tensor_desktop_status as status

    rows = {} if rows is None else rows
    texts = []
    for kind, data in events:
        if kind == "row":
            rows[data["path"]] = data
        elif kind == "review":
            review = data
        else:
            continue
        texts.append(status.overview_text(review, rows.values()))
    return texts, review, rows


def test_support_contact_is_the_approved_public_address():
    import tensor_desktop_status as status

    assert status.SUPPORT_EMAIL == "rrv9177@uncw.edu"
    assert status.SUPPORT_TEXT == "Questions or issues? Contact: rrv9177@uncw.edu"
    assert status.SUPPORT_URL == "mailto:rrv9177@uncw.edu"


@pytest.mark.parametrize("recovered", [[], ["sample_1.gjf"]])
def test_pre_submit_preview_wording_is_preserved(recovered):
    import tensor_desktop_status as status

    review = {"count": 2, "cpus": 4, "memory_mib": 2048, "recovered": recovered}
    expected = (
        "2 prepared job(s). If all start together: 4 CPUs and 2,048 MiB "
        "Slurm memory. Slurm decides start times."
    )
    if recovered:
        expected += (
            "\nRecovered handoff(s): sample_1.gjf. Check for any earlier running "
            "calculation before submitting a replacement."
        )
    rows = [summary_row("a", "PREPARED"), summary_row("b", "PREPARED")]
    assert status.overview_text(review, rows) == expected


def test_overview_follows_real_rows_from_prepared_to_pending_to_returned(
    workflow, monkeypatch
):
    w = workflow
    queue_before_running(w, monkeypatch)
    w.controller.preview(w.paths[:1], SETTINGS)
    texts, review, rows = replay_overviews(w.events)
    assert texts[-1].startswith("1 prepared job(s). If all start together: 2 CPUs")

    w.events.clear()
    w.controller.submit(w.controller.review_id, True)
    texts, review, rows = replay_overviews(w.events, review, rows)
    assert review is None
    # Once the approval is consumed, no summary may still call this work prepared.
    assert not any("prepared job(s)" in text for text in texts)
    states = [data["state"] for kind, data in w.events if kind == "row"]
    assert states == ["PREPARED", "SUBMITTED", "PENDING", "COMPLETED", "RETURNED"]
    pending = texts[states.index("PENDING") + 1]
    assert pending.startswith("Current status of 1 file(s): 1 PENDING.")
    assert "2 CPUs, 1,024 MiB Slurm memory (not measured usage)" in pending
    assert texts[-1] == (
        "Current status of 1 file(s): 1 RETURNED.\n"
        "RETURNED logs are verified; use Open results folder."
    )
    assert actions(w).count("submit") == 1


def test_overview_reports_mixed_states_without_implying_only_prepared():
    import tensor_desktop_status as status

    rows = [
        summary_row("a", "PREPARED"),
        summary_row("b", "RUNNING"),
        summary_row("c", "PENDING"),
        summary_row("d", "RETURNED"),
        summary_row("e", "CANCELLED by 4242"),
        summary_row("f", "SUBMISSION_UNKNOWN"),
        summary_row("g", "COMPLETED"),
    ]
    text = status.overview_text(None, rows)
    assert text.splitlines()[0] == (
        "Current status of 7 file(s): 1 PREPARED, 1 SUBMISSION_UNKNOWN, "
        "1 PENDING, 1 RUNNING, 1 COMPLETED, 1 RETURNED, 1 CANCELLED."
    )
    assert "prepared job(s)" not in text
    assert "PREPARED files are not submitted." in text
    assert "check your Slurm jobs before submitting again" in text
    # Only jobs still held by Slurm count toward requested limits.
    assert "2 submitted job(s): 32 CPUs, 84,652 MiB" in text
    assert "not measured usage" in text
    assert "CANCELLED jobs will not run" in text and "Run again" in text
    assert "Ended jobs need inspection" not in text
    assert "COMPLETED logs return after verification." in text

    review = {"count": 1, "cpus": 16, "memory_mib": 42326, "recovered": []}
    with_review = status.overview_text(review, rows)
    assert with_review.startswith("1 prepared job(s). If all start together")
    assert "Also in this list: 1 SUBMISSION_UNKNOWN, 1 PENDING, 1 RUNNING" in (
        with_review
    )
    assert "recorded jobs are never resubmitted" in with_review

    finished = status.overview_text(
        None, [summary_row("a", "RETURNED"), summary_row("b", "FAILED")]
    )
    assert "Requested limits" not in finished and "PREPARED" not in finished
    assert "Ended jobs need inspection; Inkly never resubmits them." in finished


def test_interrupted_or_inflight_submission_never_claims_nothing_was_submitted():
    import tensor_desktop_status as status

    rows = [summary_row("a", "PREPARED"), summary_row("b", "SUBMITTED")]
    interrupted = status.overview_text(None, rows, unconfirmed={"a"})
    assert "may already be submitted" in interrupted
    assert "are not submitted" not in interrupted
    inflight = status.overview_text(None, rows[:1], submitting=True)
    assert "wait for job IDs" in inflight and "are not submitted" not in inflight
    assert "are not submitted" in status.overview_text(None, rows[:1])


@native_display
def test_native_window_shows_contact_and_opens_mailto(workflow, tmp_path, monkeypatch):
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
        assert app.contact.cget("text") == (
            "Questions or issues? Contact: rrv9177@uncw.edu"
        )
        assert app.contact.winfo_manager() == "pack"
        assert app.contact.master is app.banner
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            assert app.contact.bind(sequence)
        opened, shown = [], []
        monkeypatch.setattr(
            gui.messagebox, "showinfo", lambda *args, **kwargs: shown.append(args)
        )
        monkeypatch.setattr(gui.webbrowser, "open", lambda url: opened.append(url) or 1)
        assert app.contact_support() == "break"
        assert opened == ["mailto:rrv9177@uncw.edu"] and not shown

        monkeypatch.setattr(gui.webbrowser, "open", lambda url: False)
        app.contact_support()
        assert "rrv9177@uncw.edu" in shown[-1][1]
        assert root.clipboard_get() == "rrv9177@uncw.edu"
    finally:
        app.close()


@native_display
def test_native_overview_tracks_submission_until_logs_return(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    queue_before_running(w, monkeypatch)
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    try:
        app.add_paths(w.paths[:1])
        w.controller.preview(w.paths[:1], SETTINGS)
        for event in w.events:
            app.events.put(event)
        app.drain()
        assert app.overview.cget("text").startswith("1 prepared job(s).")

        w.events.clear()
        w.controller.submit(w.controller.review_id, True)
        app.busy, app.action = True, "submit"
        texts = []
        for event in [*w.events, ("done", "submit")]:
            app.events.put(event)
            app.drain()
            texts.append(app.overview.cget("text"))
        assert any(
            text.startswith("Current status of 1 file(s): 1 PENDING.") for text in texts
        )
        assert not any("prepared job(s)" in text for text in texts)
        assert texts[-1].startswith("Current status of 1 file(s): 1 RETURNED.")
        assert app.table.item(str(w.paths[0]))["values"][6] == "RETURNED"
        assert str(app.submit_button["state"]) == "disabled"
    finally:
        app.close()


@native_display
def test_native_interrupted_submit_keeps_prepared_rows_unconfirmed(
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
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: None)
    try:
        app.add_paths(w.paths[:1])
        w.controller.preview(w.paths[:1], SETTINGS)
        rows = [event for event in w.events if event[0] == "row"]
        for event in w.events:
            app.events.put(event)
        app.drain()
        app.busy, app.action = True, "submit"
        for event in [
            ("review", None),
            ("error", "SSH interrupted or timed out; keep the receipt and resume"),
            ("done", "submit"),
        ]:
            app.events.put(event)
        app.drain()
        text = app.overview.cget("text")
        assert "may already be submitted" in text
        assert "are not submitted" not in text
        # The next real status read replaces the doubt with the recorded state.
        unknown = {**rows[-1][1], "state": "SUBMISSION_UNKNOWN"}
        app.events.put(("row", unknown))
        app.drain()
        text = app.overview.cget("text")
        assert text.startswith("Current status of 1 file(s): 1 SUBMISSION_UNKNOWN.")
        assert "check your Slurm jobs before submitting again" in text
        assert not app.unconfirmed
    finally:
        app.close()


def settle(root):
    for _ in range(10):
        root.update_idletasks()
        root.update()


@native_display
@pytest.mark.parametrize("scale", [100, 125, 150])
@pytest.mark.parametrize("size", ["1120x760", "940x640"])
def test_native_contact_never_obstructs_controls_at_common_scaling(
    workflow, tmp_path, scale, size
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    root = tk.Tk()
    # Windows 100/125/150% is 96/120/144 DPI; Tk scaling is pixels per point.
    # Set it absolutely: CI's own display scaling is not 100%.
    root.tk.call("tk", "scaling", 96 * scale / 100 / 72)
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    try:
        root.geometry(size + "+0+0")
        settle(root)
        shown = app.layout_report()
        # Measure the same window without the contact line to isolate its effect.
        app.contact.pack_forget()
        settle(root)
        baseline = app.layout_report()
        evidence = {"with_contact": shown, "without_contact": baseline}
        if baseline["clipped"] or baseline["table_height"] <= 60:
            # Recorded for review: a limit of the existing layout, not of the contact.
            warnings.warn(f"Desktop layout at {size}, {scale}%: {evidence}")
        assert shown["contact"] == "Questions or issues? Contact: rrv9177@uncw.edu"
        assert shown["contact_visible"], evidence
        # The contact line may never be the reason a control is cut off.
        assert set(shown["clipped"]) <= set(baseline["clipped"]), evidence
        # Nor may it collapse a table that is otherwise visible.
        if baseline["table_height"] > 60:
            assert shown["table_height"] > 30, evidence
        if scale == 100:
            # At 100% everything must fit, at the default and minimum sizes.
            assert shown["controls_visible"], evidence
            assert shown["table_height"] > 30, evidence
    finally:
        app.close()


def no_message_boxes(monkeypatch, gui):
    """A message box nobody can click would hang the test: fail instead."""

    def unexpected(*args, **kwargs):
        raise AssertionError(f"Unexpected message box: {args}")

    for name in ("showinfo", "showerror", "showwarning", "askyesno", "askokcancel"):
        monkeypatch.setattr(gui.messagebox, name, unexpected, raising=False)


def pump(app, seconds=10):
    """Drain window events until Desktop is idle with nothing left to start."""
    import time as clock

    deadline = clock.monotonic() + seconds
    quiet = 0
    while clock.monotonic() < deadline:
        app.drain()
        if not app.busy and app.after_done is None and app.events.empty():
            quiet += 1
            if quiet > 5:
                return
        else:
            quiet = 0
        clock.sleep(0.02)
    raise AssertionError("Desktop did not become idle")


def elsewhere_job(workflow, bundle="c" * 32, name="elsewhere.gjf", state="CONFIGURING"):
    data = b"%nprocshared=2\n%mem=768MB\n# hf/sto-3g\n\nElsewhere\n\n0 1\nH 0 0 0\n\n"
    workflow.endpoint.setdefault("inputs", {})[bundle] = input_copy(name, data)
    workflow.records[bundle] = {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "script_sha256": "abc",
        "stages": 1,
        "submission": "SUBMITTED",
        "state": state,
        "job_id": "456",
        "resources": {
            "cpus": 2,
            "slurm_memory_mib": 1024,
            "gaussian_memory_mb": 768,
            "hours": 2,
        },
    }
    return hpc_job(bundle, state, name, job_id="456")


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_load_my_jobs_fills_the_list_without_adding_files(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    w.endpoint["capabilities"] += ["jobs", "input"]
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    w.controller.emit = lambda kind, data: app.events.put((kind, data))
    asked, shown = [], []
    copies = tmp_path / "copies"
    monkeypatch.setattr(
        gui.filedialog, "askdirectory", lambda **k: asked.append(k) or str(copies)
    )
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda *args, **k: shown.append(args)
    )
    prompts = []
    monkeypatch.setattr(app, "ask_missing", lambda *a: prompts.append(a) or "copy")
    try:
        app.settings = dict(SETTINGS)
        # Earlier, this computer prepared sample_1; its job is now pending.
        w.controller.preview(w.paths[:1], SETTINGS)
        mine = recorded(w, w.paths[0])
        w.records[mine].update(submission="SUBMITTED", state="PENDING", job_id="123")
        w.endpoint["jobs"] = [hpc_job(mine, "PENDING"), elsewhere_job(w)]
        assert not app.files
        assert str(app.load_button["state"]) == "normal"
        app.load_button.invoke()
        pump(app)
        copy = copies / f"elsewhere-{'c' * 8}" / "elsewhere.gjf"
        assert app.files == [w.paths[0].resolve(), copy]
        assert {app.rows[str(path)]["state"] for path in app.files} == {
            "PENDING",
            "CONFIGURING",
        }
        # One prompt for the job not found here; the copies folder is remembered.
        assert [(len(a[0]), a[1]) for a in prompts] == [(1, False)]
        assert len(asked) == 1 and not shown
        assert app.jobs_index.folder() == copies.resolve()
        assert "prepare" not in actions(w)[1:] and "submit" not in actions(w)

        # Loading again finds both here: no question, no duplicate rows.
        app.load_button.invoke()
        pump(app)
        assert len(asked) == 1 and len(prompts) == 1 and len(app.files) == 2
        assert len(w.connections) == 1

        # While watching, the request goes to the watch; otherwise it waits.
        requests = []
        monkeypatch.setattr(w.controller, "request", lambda *a: requests.append(a))
        app.busy, app.action = True, "monitor"
        app.buttons()
        assert str(app.load_button["state"]) == "normal"
        app.load_button.invoke()
        app.history_button.invoke()
        assert requests == [("load_jobs", SETTINGS), ("history", SETTINGS)]
        app.action = "preview"
        app.buttons()
        assert str(app.load_button["state"]) == "disabled"
        assert str(app.history_button["state"]) == "disabled"
    finally:
        app.busy, app.action = False, None
        app.close()


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_job_history_filters_and_adds_selected_jobs(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    w.endpoint["capabilities"] += ["jobs", "input"]
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    w.controller.emit = lambda kind, data: app.events.put((kind, data))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(app, "ask_missing", lambda *a: "copy")
    app.jobs_index.set_folder(tmp_path / "copies")
    (tmp_path / "copies").mkdir()
    try:
        app.settings = dict(SETTINGS)
        cancelled = elsewhere_job(w, "b" * 32, "ethanol.gjf", "CANCELLED by 1000")
        cancelled["ended_at"] = cancelled["submitted_at"] = hpc_job(
            "x" * 32, "X", ended_days=2
        )["ended_at"]
        w.endpoint["jobs"] = [
            cancelled,
            hpc_job("a" * 32, "COMPLETED", "water.gjf", ended_days=1),
            hpc_job("d" * 32, "PENDING", "acetone.gjf"),
            hpc_job("e" * 32, "COMPLETED", "old.gjf", ended_days=60),
        ]
        w.endpoint["jobs"][1]["submitted_at"] = w.endpoint["jobs"][1]["ended_at"]
        # Well away from the 30-day boundary, so the clock tick cannot matter.
        w.endpoint["jobs"][3]["submitted_at"] = w.endpoint["jobs"][3]["ended_at"]
        app.history_button.invoke()
        pump(app)
        table = app.history_table
        # Default view: finished and cancelled jobs from the last 30 days.
        assert table.get_children() == ("b" * 32, "a" * 32)
        assert "Showing 2 of 4" in app.history_summary.cget("text")
        assert str(tmp_path / "copies") in app.copies_label.cget("text")
        app.history_state.set("All")
        app.history_period.set("All time")
        app.refresh_history()
        assert len(table.get_children()) == 4
        app.history_text.set("ethanol")
        app.refresh_history()
        assert table.get_children() == ("b" * 32,)
        assert str(app.history_add["state"]) == "disabled"
        table.selection_set("b" * 32)
        app.history_buttons()
        app.history_add.invoke()
        pump(app)
        copy = tmp_path / "copies" / f"ethanol-{'b' * 8}" / "ethanol.gjf"
        assert app.files == [copy]
        assert status_of(app, copy).startswith("CANCELLED")
    finally:
        app.busy, app.action = False, None
        app.close()


def status_of(app, path):
    return app.rows[str(path)]["state"]


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="Native Tk display required",
)
def test_native_find_files_adds_the_originals_instead_of_copies(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    w.endpoint["capabilities"] += ["jobs", "input"]
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    w.controller.emit = lambda kind, data: app.events.put((kind, data))
    # A job submitted before Desktop kept its list of files, in a project tree.
    job = elsewhere_job(w, "c" * 32, "old.gjf")
    original = tmp_path / "research" / "2026" / "old.gjf"
    original.parent.mkdir(parents=True)
    original.write_bytes(base64.b64decode(w.endpoint["inputs"]["c" * 32]["data"]))
    original.with_suffix(".gjf.inkly.json").write_text(
        json.dumps(
            {
                "host": SETTINGS["host"],
                "ink_path": SETTINGS["ink_path"],
                "bundle": "c" * 32,
                "auth": SETTINGS["auth"],
                "output": str(original.parent / "returned"),
            }
        )
    )
    w.endpoint["jobs"] = [job]
    choices = ["find"]
    prompts = []
    monkeypatch.setattr(
        app, "ask_missing", lambda *a: prompts.append(a) or choices.pop(0)
    )
    monkeypatch.setattr(
        gui.filedialog, "askdirectory", lambda **k: str(tmp_path / "research")
    )
    no_message_boxes(monkeypatch, gui)
    try:
        app.settings = dict(SETTINGS)
        app.load_button.invoke()
        pump(app)
        assert len(app.files) == 1 and app.files[0].resolve() == original.resolve()
        assert app.rows[str(app.files[0])]["state"] == "CONFIGURING"
        assert not list(tmp_path.glob("**/old-cccccccc"))  # No copy was made.
        assert len(prompts) == 1
        # Found automatically next time: no prompt and no duplicate row.
        app.load_button.invoke()
        pump(app)
        assert len(app.files) == 1 and len(prompts) == 1
    finally:
        app.busy, app.action = False, None  # Never ask before closing a test.
        app.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Real Tk dialog")
@pytest.mark.parametrize("button", ["find", "copy", "skip"])
def test_native_missing_jobs_prompt_returns_the_choice(
    workflow, tmp_path, monkeypatch, button
):
    import tkinter as tk
    import tensor_desktop as gui

    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: workflow.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    no_message_boxes(monkeypatch, gui)
    # If the button press never happens, close the prompt so a failure
    # cannot leave the test waiting.
    root.after(5000, lambda: app.missing_dialog.destroy())
    try:
        root.after(200, lambda: app.missing_buttons[button].invoke())
        choice = app.ask_missing(
            [{"name": f"job{i}.gjf", "bundle": "a" * 32} for i in range(8)],
            searched=True,
            complete=False,
        )
        assert choice == button
    finally:
        app.busy, app.action = False, None
        app.close()


def legacy_copy_scenario(workflow, tmp_path):
    """Synthetic 19-job history with copies, reruns and a cancelled-only file."""
    from datetime import datetime, timedelta, timezone
    import tensor_desktop_jobs as job_files

    w = workflow
    w.endpoint["capabilities"] += ["jobs", "input"]
    copies, project = tmp_path / "copies", tmp_path / "project"
    project.mkdir()
    families = [
        ("cancelled_1.gjf", ["CANCELLED"] * 6),
        ("compound_1.gjf", ["FAILED", "FAILED", "COMPLETED"]),
        ("compound_2.gjf", ["FAILED", "CONFIGURING"]),
        ("single_1.gjf", ["COMPLETED"]),
        ("single_2.gjf", ["COMPLETED"]),
    ] + [(f"series_{number}.gjf", ["COMPLETED"]) for number in range(6)]
    listing, originals = [], []
    serial = 0
    for name, states in families:
        runs = []
        for state in states:
            serial += 1
            bundle = f"{serial:08x}" + "0" * 24
            item = elsewhere_job(w, bundle, name, state)
            stamp = (
                datetime.now(timezone.utc) - timedelta(minutes=40 - serial)
            ).isoformat()
            item.update(
                prepared_at=stamp,
                submitted_at=stamp,
                ended_at=stamp if state != "CONFIGURING" else None,
            )
            listing.append(item)
            runs.append(item)
            job_files.restore(
                copies,
                item,
                {**w.endpoint["inputs"][bundle], "bundle": bundle},
                SETTINGS,
                w.controller.index,
            )
        if name == "cancelled_1.gjf" or name == "single_2.gjf":
            continue  # One file is cancelled-only; one really has only a copy.
        newest = runs[-1]
        path = project / name
        path.write_bytes(
            base64.b64decode(w.endpoint["inputs"][newest["bundle"]]["data"])
        )
        saved = {
            **SETTINGS,
            "bundle": newest["bundle"],
            "output": str(project / "returned"),
        }
        job_files.receipt_for(path).write_text(json.dumps(saved))
        for older in runs[:-1]:
            path.with_name(
                path.name + ".inkly.json.rerun-" + older["bundle"] + ".bak"
            ).write_text(json.dumps({**saved, "bundle": older["bundle"]}))
        originals.append(path)
    w.controller.index.set_folder(copies)
    legacy = json.loads(w.controller.index.path.read_text())
    for entry in legacy["handoffs"].values():
        entry.pop("original_checked", None)  # 0.5.0 did not record this choice.
    w.controller.index.path.write_text(json.dumps(legacy))
    w.endpoint["jobs"] = listing
    return project, copies, originals


def test_legacy_copy_load_needs_one_original_search_then_loads_quietly(
    workflow, tmp_path
):
    w, c = workflow, workflow.controller
    project, copies, originals = legacy_copy_scenario(w, tmp_path)
    assert len(w.endpoint["jobs"]) == 19
    c.load_jobs(SETTINGS)
    first = found_event(w)
    assert not first["paths"]
    assert len(first["restore"]) == 13 and first["cancelled"] == 6
    assert all(job.get("local_copy") for job in first["restore"])
    context = {"settings": SETTINGS, "jobs": first["jobs"], "from_history": False}
    c.search_jobs(first["restore"], str(project), context)
    result = [data for kind, data in w.events if kind == "jobs_searched"][-1]
    assert len(result["paths"]) == 10 and not result["missing"]
    assert len({Path(path).name for path in result["paths"]}) == 10
    assert result["replaced"] == 3
    assert result["found_count"] == 9 and result["copies"] == 1
    assert sum(move["original"] for move in result["moves"]) == 9
    assert {str(path) for path in originals} <= set(result["paths"])
    c.load_jobs(SETTINGS)
    next_load = found_event(w)
    assert len(next_load["paths"]) == 10 and not next_load["restore"]
    assert set(c.index.folders()) == {copies.resolve(), project.resolve()}
    assert "submit" not in actions(w) and "cancel" not in actions(w)


def test_cancelled_jobs_stay_selectable_from_history_only(workflow):
    w, c = workflow, workflow.controller
    w.endpoint["capabilities"] += ["jobs", "input"]
    item = elsewhere_job(w, "c" * 32, "cancelled.gjf", "CANCELLED")
    item["ended_at"] = hpc_job("c" * 32, "CANCELLED", ended_days=1)["ended_at"]
    w.endpoint["jobs"] = [item]
    c.load_jobs(SETTINGS)
    assert found_event(w)["cancelled"] == 1 and not found_event(w)["restore"]
    c.load_jobs(SETTINGS, [item["bundle"]])
    assert found_event(w)["restore"] == [item]


@native_display
def test_native_legacy_copy_load_searches_once_and_keeps_each_file_once(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    w = workflow
    project, copies, originals = legacy_copy_scenario(w, tmp_path)
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "settings.json",
    )
    w.controller.emit = lambda kind, data: app.events.put((kind, data))
    prompts = []
    no_message_boxes(monkeypatch, gui)
    monkeypatch.setattr(
        app, "ask_missing", lambda *args: prompts.append(args) or "find"
    )
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(project))
    try:
        app.settings = dict(SETTINGS)
        app.load_my_jobs()
        pump(app)
        assert len(prompts) == 1
        assert len(app.files) == len(set(path.name for path in app.files)) == 10
        assert set(originals) <= set(app.files)
        app.load_my_jobs()
        pump(app)
        assert len(app.files) == 10 and len(prompts) == 1
        assert len(app.table.get_children()) == 10
        assert "submit" not in actions(w) and "cancel" not in actions(w)
    finally:
        app.busy, app.action = False, None
        app.close()


@native_display
def test_native_history_can_repoint_a_loaded_copy_without_new_rows(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui
    import tensor_desktop_jobs as job_files

    w = workflow
    w.endpoint["capabilities"] += ["jobs", "input"]
    item = elsewhere_job(w, "c" * 32, "compound.gjf")
    project = tmp_path / "originals"
    project.mkdir()
    copy = job_files.restore(
        tmp_path / "copies",
        item,
        {**w.endpoint["inputs"][item["bundle"]], "bundle": item["bundle"]},
        SETTINGS,
        w.controller.index,
    )
    # Accepting the copy suppresses default prompts, but never disables Locate.
    w.controller.index.remember(copy, item["bundle"], original_checked=True)
    original = project / item["name"]
    original.write_bytes(copy.read_bytes())
    job_files.receipt_for(original).write_text(
        json.dumps(
            {**SETTINGS, "bundle": item["bundle"], "output": str(project / "returned")}
        )
    )
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: w.controller,
        settings_path=tmp_path / "settings.json",
    )
    w.controller.emit = lambda kind, data: app.events.put((kind, data))
    no_message_boxes(monkeypatch, gui)
    summaries = []
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda *args, **kw: summaries.append(args)
    )
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(project))
    try:
        app.settings = dict(SETTINGS)
        app.add_paths([copy])
        w.endpoint["jobs"] = [item]
        app.show_history(
            {"jobs": [item], "observed_at": item["submitted_at"], "settings": SETTINGS}
        )
        app.history_state.set("All")
        app.history_period.set("All time")
        app.refresh_history()
        app.history_table.selection_set(item["bundle"])
        assert app.history_table.item(item["bundle"])["values"][-1] == "Copy"
        app.find_history_files()
        pump(app)
        assert app.files == [original] and len(app.table.get_children()) == 1
        assert app.history_table.item(item["bundle"])["values"][-1] == "Original"
        assert "Switched 1" in summaries[-1][1] and str(project) in summaries[-1][1]
        assert (
            copy.is_file() and "submit" not in actions(w) and "cancel" not in actions(w)
        )
    finally:
        app.busy, app.action = False, None
        app.close()


@native_display
def test_native_partial_search_prompt_includes_the_result_and_repeat_count(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: workflow.controller,
        settings_path=tmp_path / "settings.json",
    )
    no_message_boxes(monkeypatch, gui)
    app.load_context["search_summary"] = (
        "Found 3 job(s) in chosen project; 2 still not found."
    )
    seen = []

    def inspect():
        label = app.missing_dialog.grid_slaves(row=0)[0]
        seen.append(label.cget("text"))
        app.missing_buttons["skip"].invoke()

    guard = root.after(
        5000,
        lambda: (
            app.missing_dialog.destroy() if app.missing_dialog.winfo_exists() else None
        ),
    )
    root.after(100, inspect)
    try:
        item = hpc_job("a" * 32, "FAILED", "compound.gjf")
        assert (
            app.ask_missing([item, {**item, "bundle": "b" * 32}], True, True) == "skip"
        )
        assert "Found 3 job(s) in chosen project; 2 still not found." in seen[0]
        assert "compound.gjf \u00d72" in seen[0] and "FAILED; submitted" in seen[0]
    finally:
        root.after_cancel(guard)
        app.busy, app.action = False, None
        app.close()


RELEASE = {"version": "0.6.0", "commit": "c" * 40}


@pytest.fixture
def hpc_setup(monkeypatch):
    """A controller whose SSH session runs the setup, then answers version."""
    state = SimpleNamespace(sessions=[], events=[], failure=None, version="0.6.0")
    state.done = {"done": True, "ok": True, "changed": True, "commit": "c" * 40}

    class SetupSession:
        def __init__(self, connection, ssh, remote=None):
            self.connection, self.remote, self.closed = connection, remote, False
            self.diagnostics = None
            self.process = SimpleNamespace(terminate=lambda: None)
            state.sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

        def run_setup(self, progress):
            progress({"stage": "preflight", "line": "bash 5.1.8"})
            progress({"stage": "install", "line": "Installing Inkly"})
            if state.failure:
                raise ValueError(state.failure)
            return state.done

        def exchange(self, request, *, receipt=None):
            assert request == {"action": "version"}
            return {
                "bundle": receipt["bundle"],
                "version": state.version,
                "capabilities": ["prepare", "submit", "version"],
            }

    monkeypatch.setattr(backend.bridge, "Session", SetupSession)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "ssh.exe")
    monkeypatch.setattr(backend.setup, "target", lambda: dict(RELEASE))
    state.controller = backend.DesktopController(
        lambda *event: state.events.append(event), interval=0
    )
    yield state
    state.controller.close()
    state.controller.journal.close()


def setup_events(state, kind):
    return [data for event, data in state.events if event == kind]


def test_setup_hpc_installs_this_release_over_one_sign_in(hpc_setup):
    c = hpc_setup.controller
    c.execute("setup_hpc", SETTINGS)
    [session] = hpc_setup.sessions
    assert session.connection["ink_path"] == "~/.inkly/bin/ink"
    payload = session.remote.split(" P=", 1)[1].split(";", 1)[0]
    assert base64.b64decode(payload) == backend.setup.script()
    assert f"'C={'c' * 40}; V=0.6.0; " in session.remote
    assert " M=auto; S=1; " in session.remote
    assert setup_events(hpc_setup, "setup_progress") == [
        {"stage": "preflight", "line": "bash 5.1.8"},
        {"stage": "install", "line": "Installing Inkly"},
    ]
    [result] = setup_events(hpc_setup, "setup_result")
    assert result["changed"] is True and result["version"] == "0.6.0"
    # One opening line in Activity: what, which commit, where, and the sign-in.
    [opening] = [m for m in setup_events(hpc_setup, "message") if "Inkly 0.6.0" in m]
    assert opening.startswith(
        f"Setting up Inkly 0.6.0 (commit {'c' * 12}) on {SETTINGS['host']}."
    )
    [notice] = setup_events(hpc_setup, "notice")
    assert "Inkly 0.6.0 is installed" in notice and "No Slurm command" in notice
    # Desktop already uses ~/.inkly/bin/ink, so the signed-in session is kept.
    assert c.session is session and not session.closed
    assert not setup_events(hpc_setup, "error")
    c.execute("close")
    common = {"client_session", "sequence", "occurred_at"}
    records = [
        {k: v for k, v in e["payload"].items() if k not in common}
        for e in c.journal.take()
        if e["event_type"] == "desktop_setup"
    ]
    assert records[0] == {"stage": "start", "mode": "auto"}
    assert records[1]["stage"] == "done" and records[1]["outcome"] == "ok"
    assert set(records[1]) == {"stage", "outcome", "seconds"}


def test_setup_hpc_for_another_launcher_explains_the_switch(hpc_setup):
    c = hpc_setup.controller
    hpc_setup.done = {**hpc_setup.done, "changed": False}
    settings = {**SETTINGS, "ink_path": "~/inkly-desktop-endpoint/ink"}
    c.execute("setup_hpc", settings, "repair", False)
    [session] = hpc_setup.sessions
    assert " M=repair; S=0; " in session.remote
    assert any(
        m.startswith("Repairing Inkly 0.6.0")
        for m in setup_events(hpc_setup, "message")
    )
    assert session.closed and c.session is None
    [notice] = setup_events(hpc_setup, "notice")
    assert "already up to date" in notice
    [result] = setup_events(hpc_setup, "setup_result")
    # The window offers the switch; saved receipts keep their own launcher.
    assert result["previous"] == "~/inkly-desktop-endpoint/ink"
    assert result["launcher"] == "~/.inkly/bin/ink"


def test_setup_hpc_failure_shows_only_the_setups_message(hpc_setup):
    c = hpc_setup.controller
    hpc_setup.failure = "~/.inkly/src/hpc-ink-setup has local changes."
    c.execute("setup_hpc", SETTINGS)
    assert setup_events(hpc_setup, "error") == [
        "~/.inkly/src/hpc-ink-setup has local changes."
    ]
    assert hpc_setup.sessions[0].closed and c.session is None
    assert not setup_events(hpc_setup, "setup_result")
    c.execute("close")
    records = c.journal.take()
    failed = [e["payload"] for e in records if e["event_type"] == "desktop_setup"][-1]
    assert failed["outcome"] == "failed" and failed["stage"] == "install"
    assert "src" not in json.dumps(records)
    assert any(
        e["payload"].get("action") == "setup_hpc"
        for e in records
        if e["event_type"] == "desktop_error"
    )


def test_setup_hpc_checks_the_version_after_the_handover(hpc_setup):
    hpc_setup.version = "0.5.1"
    hpc_setup.controller.execute("setup_hpc", SETTINGS)
    [error] = setup_events(hpc_setup, "error")
    assert "reported Inkly 0.5.1, not 0.6.0" in error


def test_setup_target_of_a_build_is_the_recorded_commit(tmp_path, monkeypatch):
    from inkly import __version__

    setup = backend.setup
    monkeypatch.setattr(setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(setup.sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(ValueError, match="does not record which release"):
        setup.target()
    info = tmp_path / "desktop_build.json"
    info.write_text(json.dumps({"version": __version__, "commit": "d" * 40}))
    assert setup.target() == {"version": __version__, "commit": "d" * 40}
    info.write_text(json.dumps({"version": "0.0.1", "commit": "d" * 40}))
    with pytest.raises(ValueError, match="differs from this Desktop"):
        setup.target()
    info.write_text(json.dumps({"version": __version__, "commit": "d" * 7}))
    with pytest.raises(ValueError, match="which commit"):
        setup.target()


def test_setup_target_of_a_source_run_is_the_committed_head():
    import subprocess

    from inkly import __version__

    repo = Path(backend.setup.__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        committed = subprocess.run(
            ["git", "-C", str(repo), "show", "HEAD:inkly/__init__.py"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a Git checkout")
    if f'"{__version__}"' not in committed:
        pytest.skip("the version change is not committed yet")
    assert backend.setup.target() == {"version": __version__, "commit": head}


def test_packaged_setup_script_keeps_lf_endings_and_smoke_reports_it(
    tmp_path, monkeypatch
):
    from inkly import __version__

    setup = backend.setup
    monkeypatch.setattr(setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(setup.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert setup.ready() is False
    (tmp_path / "desktop_hpc_setup.sh").write_bytes(b"set -u\r\necho ok\r\n")
    assert setup.script() == b"set -u\necho ok\n"
    assert setup.ready() is False  # No recorded commit yet.
    (tmp_path / "desktop_build.json").write_text(
        json.dumps({"version": __version__, "commit": "e" * 40})
    )
    assert setup.ready() is True


def test_desktop_build_bundles_the_setup_script_and_its_commit():
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts/windows/build_desktop.ps1").read_text()
    assert '--add-data "$setupScript;."' in build
    assert '--add-data "$buildInfo;."' in build
    assert "desktop_build.json" in build and "rev-parse HEAD" in build
    for workflow in ("lint.yml", "release-desktop.yml"):
        text = (root / ".github/workflows" / workflow).read_text()
        assert "assert result['setup'], result" in text
    assert b"\r" not in (root / "scripts/desktop_hpc_setup.sh").read_bytes()


def test_connection_offers_setup_when_inkly_is_missing_on_the_hpc(workflow):
    c = workflow.controller
    c.connect(SETTINGS)

    def missing(*args, **kwargs):
        raise backend.bridge.MissingLauncherError("Inkly is not installed")

    c.session.exchange = missing
    c.test_connection(SETTINGS)
    [install] = [data for kind, data in workflow.events if kind == "hpc_install"]
    assert install["state"] == "missing" and install["offer"] is True
    assert install["launcher"] == SETTINGS["ink_path"]
    assert c.session is None  # The remote side closed; the next action signs in again.
    assert not [kind for kind, _ in workflow.events if kind in {"notice", "error"}]
    message = [data for kind, data in workflow.events if kind == "message"][-1]
    assert "not installed at ~/.inkly/bin/ink" in message


@pytest.mark.parametrize(
    "error, action, expected",
    [
        ("MissingLauncherError", "preview", "hpc_install"),
        ("HostKeyError", "preview", "host_untrusted"),
        ("HostKeyError", "setup_hpc", "host_untrusted"),
        ("MissingLauncherError", "setup_hpc", None),
    ],
)
def test_failed_actions_offer_the_fix_before_the_error(
    workflow, monkeypatch, error, action, expected
):
    c = workflow.controller
    failure = getattr(backend.bridge, error)("stopped")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(c, action, fail)
    c.execute(action, workflow.paths if action == "preview" else SETTINGS, SETTINGS)
    kinds = [kind for kind, _ in workflow.events]
    offers = [k for k in kinds if k in {"hpc_install", "host_untrusted"}]
    assert offers == ([expected] if expected else [])
    if expected:
        assert kinds.index(expected) < kinds.index("error")
    if expected == "hpc_install":
        [install] = [d for k, d in workflow.events if k == "hpc_install"]
        assert install["state"] == "missing" and install["offer"] is True


@pytest.mark.parametrize(
    "version, than, older",
    [
        ("0.5.0", "0.6.0", True),
        ("0.10.0", "0.9.1", False),
        ("0.6.0", "0.6.0", False),
        ("0.6.0+local", "0.6.1", True),
        ("garbage", "0.6.0", False),
    ],
)
def test_release_versions_compare_by_number(version, than, older):
    assert backend.DesktopController.older(version, than) is older


GUI_RELEASE = {"version": "0.6.0", "commit": "f" * 40}


def setup_window(workflow, tmp_path, monkeypatch):
    """A Desktop window whose message boxes fail fast and whose dialogs never wait."""
    import tkinter as tk
    import tensor_desktop as gui

    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: workflow.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    no_message_boxes(monkeypatch, gui)
    # A grab on a window that is not shown fails in real Tk.
    monkeypatch.setattr(gui.tk.Toplevel, "grab_set", lambda self: None)
    monkeypatch.setattr(gui.hpc_setup, "target", lambda: dict(GUI_RELEASE))
    app.settings = dict(SETTINGS)
    return gui, app


def activity(app):
    return app.activity.get("1.0", "end")


@native_display
def test_native_connection_settings_offer_set_up_update_and_repair(
    workflow, tmp_path, monkeypatch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(app, "setup_hpc", lambda mode: started.append(mode))
    try:
        cases = [
            (None, "Set up Inkly on the HPC…", True, "Use Test connection"),
            (
                {"state": "missing", "launcher": "~/.inkly/bin/ink"},
                "Set up Inkly on the HPC…",
                True,
                "Not installed at ~/.inkly/bin/ink",
            ),
            (
                {"state": "outdated", "version": "0.5.0", "desktop": "0.6.0"},
                "Update Inkly on the HPC…",
                True,
                "The HPC copy is 0.5.0; this Desktop is 0.6.0",
            ),
            (
                {"state": "current", "version": "0.6.0"},
                "Set up Inkly on the HPC…",
                False,
                "Inkly 0.6.0 on the HPC is ready",
            ),
        ]
        for state, label, enabled, status in cases:
            app.hpc_state = state
            assert status in app.hpc_status_text()
            app.configure()
            assert app.setup_button.cget("text") == label
            assert (str(app.setup_button["state"]) == "normal") is enabled
            assert str(app.repair_button["state"]) == "normal"
            # Each button saves the settings shown, closes the window, then acts.
            app.repair_button.invoke()
        assert started == ["repair"] * 4
        app.hpc_state = None
        app.configure()
        app.setup_button.invoke()
        assert started[-1] == "auto"
        saved = json.loads((tmp_path / "gui-settings.json").read_text())
        assert {key: saved[key] for key in SETTINGS} == SETTINGS
    finally:
        app.busy = False
        app.close()


@native_display
def test_native_setup_confirmation_lists_every_change_and_the_sharing_choice(
    workflow, tmp_path, monkeypatch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    try:
        text = gui.setup_summary("researcher@cluster", "auto", GUI_RELEASE)
        for part in (
            "Inkly 0.6.0 (commit ffffffffffff)",
            "github.com/thealice-lab/hpc-ink-setup",
            "~/.inkly/src/hpc-ink-setup",
            "setup.sh",
            "PyPI",
            "~/.inkly/src/gaussian-docs-scraper",
            "PATH in ~/.bashrc",
            "No Slurm commands",
            "nothing is deleted",
            "never downgrades",
            "under a minute",
        ):
            assert part in text
        assert "Repair runs setup.sh again" in gui.setup_summary(
            "researcher@cluster", "repair", GUI_RELEASE
        )
        assert "never enter passwords" in gui.SHARED_LOGGING_NOTICE
        for state, mode, verb in [
            (None, "auto", "Set up"),
            ({"state": "outdated"}, "auto", "Update"),
            ({"state": "current"}, "repair", "Repair"),
        ]:
            app.hpc_state = state
            dialog, buttons, _, _ = app.setup_dialog(mode, GUI_RELEASE)
            assert dialog.title() == f"{verb} Inkly on the HPC"
            assert buttons["start"].cget("text") == verb
            buttons["cancel"].invoke()
        app.hpc_state = None
        for button, opt_out, expected in [
            ("start", False, True),
            ("start", True, False),
            ("cancel", False, None),
        ]:
            dialog, buttons, shared, result = app.setup_dialog("auto", GUI_RELEASE)
            assert shared.get() is True  # The configuration's default.
            if opt_out:
                shared.set(False)
            buttons[button].invoke()
            assert result["shared"] is expected
            assert not dialog.winfo_exists()
    finally:
        app.close()


@native_display
def test_native_setup_starts_only_after_confirmation(workflow, tmp_path, monkeypatch):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    started, choices, errors = [], [False, None], []
    monkeypatch.setattr(
        app, "start", lambda action, *args: started.append((action, args))
    )
    monkeypatch.setattr(app, "confirm_setup", lambda mode, release: choices.pop(0))
    try:
        app.setup_hpc("auto")
        assert started == [("setup_hpc", (SETTINGS, "auto", False))]
        app.setup_hpc("repair")
        assert len(started) == 1
        assert "HPC setup cancelled; nothing was changed" in activity(app)

        def unknown():
            raise ValueError("Cannot tell which commit to install on the HPC.")

        monkeypatch.setattr(gui.hpc_setup, "target", unknown)
        monkeypatch.setattr(
            gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(args)
        )
        app.setup_hpc("auto")
        assert errors[-1][0] == "Cannot set up the HPC" and len(started) == 1
    finally:
        app.close()


@native_display
@pytest.mark.parametrize("failed", [False, True])
def test_native_missing_hpc_copy_is_offered_for_setup_without_an_error_box(
    workflow, tmp_path, monkeypatch, failed
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    asked, setups = [], []
    monkeypatch.setattr(
        gui.messagebox,
        "askyesno",
        lambda title, text, **kwargs: asked.append((title, text)) or True,
    )
    monkeypatch.setattr(app, "setup_hpc", lambda mode: setups.append(mode))
    try:
        app.busy, app.action = True, "preview" if failed else "test_connection"
        app.events.put(
            (
                "hpc_install",
                {
                    "state": "missing",
                    "launcher": "~/.inkly/bin/ink",
                    "version": None,
                    "desktop": "0.6.0",
                    "offer": True,
                },
            )
        )
        if failed:
            app.events.put(("error", "Inkly is not installed at ~/.inkly/bin/ink"))
        app.events.put(("done", app.action))
        app.drain()
        [(title, text)] = asked
        assert title == "Set up Inkly on the HPC" and "Set it up now?" in text
        assert "no Slurm commands, nothing deleted" in text
        assert setups == ["auto"]
        assert "Not installed" in app.hpc_status_text()
    finally:
        app.busy = False
        app.close()


@native_display
def test_native_untrusted_host_offers_the_first_sign_in(
    workflow, tmp_path, monkeypatch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    asked, opened = [], []
    monkeypatch.setattr(
        gui.messagebox,
        "askyesno",
        lambda title, text, **kwargs: asked.append((title, text)) or True,
    )
    monkeypatch.setattr(app, "first_sign_in", lambda: opened.append(True))
    try:
        app.busy, app.action = True, "test_connection"
        app.events.put(("host_untrusted", None))
        app.events.put(("error", "SSH host key is not trusted"))
        app.events.put(("done", "test_connection"))
        app.drain()
        [(title, text)] = asked
        assert title == "Trust the HPC on this computer?"
        assert "matches the fingerprint your HPC site publishes" in text
        assert opened == [True]
    finally:
        app.busy = False
        app.close()


@native_display
@pytest.mark.parametrize("switch", [True, False])
def test_native_setup_progress_then_the_launcher_switch(
    workflow, tmp_path, monkeypatch, switch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    app.settings = {**SETTINGS, "ink_path": "~/inkly-desktop-endpoint/ink"}
    asked, notices = [], []
    monkeypatch.setattr(
        gui.messagebox,
        "askyesno",
        lambda title, text, **kwargs: asked.append((title, text)) or switch,
    )
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda *args, **kwargs: notices.append(args)
    )
    try:
        app.busy, app.action = True, "setup_hpc"
        for line in (
            "Installing Inkly Python requirements",
            "WARNING: You are using pip version 21.3.1; however, version 26.0.1 is available.",
            "Requirement already satisfied: tomli>=2.0 in /home/x/.inkly/venv (2.4.1)",
        ):
            app.events.put(("setup_progress", {"stage": "install", "line": line}))
        app.events.put(
            (
                "setup_result",
                {
                    "version": "0.6.0",
                    "commit": "f" * 40,
                    "changed": True,
                    "capabilities": ["prepare"],
                    "launcher": "~/.inkly/bin/ink",
                    "previous": "~/inkly-desktop-endpoint/ink",
                },
            )
        )
        app.events.put(("notice", "Inkly 0.6.0 is installed on the HPC."))
        app.events.put(("done", "setup_hpc"))
        app.drain()
        assert "HPC setup: Installing Inkly Python requirements" in activity(app)
        assert "pip version" not in activity(app)
        assert "Requirement already satisfied" not in activity(app)
        assert notices and asked[0][0] == "Use the new HPC copy?"
        assert "Saved jobs keep the launcher recorded" in asked[0][1]
        path = tmp_path / "gui-settings.json"
        if switch:
            assert app.settings["ink_path"] == "~/.inkly/bin/ink"
            assert json.loads(path.read_text())["ink_path"] == "~/.inkly/bin/ink"
            assert "ready for this Desktop" in app.hpc_status_text()
        else:
            assert app.settings["ink_path"] == "~/inkly-desktop-endpoint/ink"
            assert not path.exists()
    finally:
        app.busy = False
        app.close()


@native_display
def test_native_failed_setup_says_running_it_again_is_safe(
    workflow, tmp_path, monkeypatch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    errors = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(args)
    )
    try:
        app.busy, app.action = True, "setup_hpc"
        app.events.put(("error", "~/.inkly/src/hpc-ink-setup has local changes."))
        app.events.put(("done", "setup_hpc"))
        app.drain()
        [(title, text)] = [error[:2] for error in errors]
        assert title == "HPC setup stopped"
        assert "has local changes" in text and "Running setup again is safe" in text
    finally:
        app.busy = False
        app.close()


@native_display
def test_native_first_sign_in_opens_plain_openssh_in_a_console(
    workflow, tmp_path, monkeypatch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    app.settings = {**SETTINGS, "auth": "keyboard-interactive"}
    opened, shown = [], []
    ssh = r"C:\Windows\System32\OpenSSH\ssh.exe"
    monkeypatch.setattr(
        gui.subprocess, "Popen", lambda args, **kw: opened.append((args, kw))
    )
    monkeypatch.setattr(gui.shutil, "which", lambda name: ssh)
    monkeypatch.setattr(
        gui.messagebox, "showinfo", lambda *args, **kwargs: shown.append(args)
    )
    monkeypatch.setenv("SSH_ASKPASS", "dialog.exe")
    try:
        name = gui.os.name
        monkeypatch.setattr(gui.os, "name", "nt")
        try:
            app.first_sign_in()
        finally:
            monkeypatch.setattr(gui.os, "name", name)
        [(args, kwargs)] = opened
        assert args[:3] == ["cmd.exe", "/c", ssh]
        assert "StrictHostKeyChecking=ask" in args
        assert "PreferredAuthentications=keyboard-interactive" in args
        assert args[-4:] == ["researcher@cluster", "exit", "&", "pause"]
        assert "SSH_ASKPASS" not in kwargs["env"]
        assert "first sign-in window opened" in activity(app)
        if name != "nt":
            app.first_sign_in()
            assert "ssh researcher@cluster exit" in shown[-1][1]
    finally:
        app.close()


@native_display
def test_native_framed_sections_and_checkboxes_match_the_window(
    workflow, tmp_path, monkeypatch
):
    import tkinter as tk
    import tensor_desktop as gui

    styled = {}

    class Recording(gui.ttk.Style):
        def configure(self, name, *args, **kwargs):
            styled.setdefault(name, {}).update(kwargs)
            return super().configure(name, *args, **kwargs)

    monkeypatch.setattr(gui.ttk, "Style", Recording)
    root = tk.Tk()
    root.withdraw()
    app = gui.DesktopApp(
        root,
        controller_factory=lambda emit, askpass: workflow.controller,
        settings_path=tmp_path / "gui-settings.json",
    )
    try:
        window = styled["TFrame"]["background"]
        for name in ("TLabelframe", "TLabelframe.Label", "TCheckbutton"):
            assert styled[name]["background"] == window, name
    finally:
        app.close()


@native_display
def test_native_watched_jobs_that_finish_notify_once_per_check(
    workflow, tmp_path, monkeypatch
):
    gui, app = setup_window(workflow, tmp_path, monkeypatch)
    w = workflow
    first, second = (str(path) for path in w.paths)
    shown, in_front = [], {"value": False}
    monkeypatch.setattr(
        app.notifier,
        "show",
        lambda title, text, warning=False: shown.append((title, text, warning)),
    )
    monkeypatch.setattr(app.notifier, "foreground", lambda: in_front["value"])

    def rows(*changes):
        for path, state in changes:
            job = "1" if path == first else "2"
            app.events.put(("row", {**app.rows[path], "state": state, "job_id": job}))
        app.drain()

    try:
        app.add_paths(w.paths)
        w.controller.preview(w.paths, SETTINGS)
        for event in w.events:
            app.events.put(event)
        app.drain()
        assert app.notify_jobs.get() is True  # On unless turned off.
        # Not watching: a finished state shown by a preview or a load is quiet.
        rows((first, "RETURNED"))
        assert shown == []

        app.busy, app.action = True, "monitor"
        rows((first, "RUNNING"), (second, "RUNNING"))
        assert shown == []
        rows((first, "RETURNED"), (second, "OUT_OF_MEMORY"))
        assert len(shown) == 1  # One notification for the whole check.
        title, text, warning = shown[0]
        assert title == "Inkly: 1 returned and 1 ended without a result"
        assert "Returned: sample_1.gjf" in text
        assert "No result: sample_2.gjf (OUT_OF_MEMORY)" in text
        assert "All watched jobs are done." in text and warning
        rows((first, "RETURNED"))  # The same state again is not news.
        assert len(shown) == 1

        # With Desktop in front, the table already shows it.
        rows((first, "RUNNING"))
        in_front["value"] = True
        rows((first, "RETURNED"))
        assert len(shown) == 1

        # Turned off, nothing is shown, and the choice is kept for next time.
        in_front["value"] = False
        app.notify_jobs.set(False)
        app.save_notify_choice()
        rows((first, "RUNNING"))
        rows((first, "RETURNED"))
        assert len(shown) == 1
        assert gui.notify.load_preference(tmp_path) is False
    finally:
        app.busy = False
        app.close()
