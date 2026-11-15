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
                return {"bundle": bundle, "capabilities": endpoint["capabilities"]}
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


def actions(workflow):
    return [action for _, action in workflow.calls]


def record_for(workflow, path):
    receipt = json.loads(path.with_suffix(".gjf.inkly.json").read_text())
    return workflow.records[receipt["bundle"]]


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


def test_cancelled_row_does_not_block_submitting_other_prepared_files(workflow):
    w = workflow
    w.controller.preview(w.paths, SETTINGS)
    recorded_job(w, w.paths[0], "CANCELLED by 1000", "1")
    w.controller.preview(w.paths, SETTINGS)
    assert w.controller.review()["count"] == 1
    w.controller.submit(w.controller.review_id, True)
    assert actions(w).count("submit") == 1
    assert record_for(w, w.paths[0])["state"] == "CANCELLED by 1000"


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
        assert len(app.files) == 1 and app.review is None
        assert str(app.submit_button["state"]) == "disabled"
        assert app.table.item(str(w.paths[1]))["values"][6] == "Not previewed"
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
        app.busy = False
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
