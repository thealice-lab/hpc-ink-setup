#!/usr/bin/env python3
"""Choose TENSOR exports and hand them to Inkly through native OpenSSH."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from types import SimpleNamespace

import tensor_bridge as bridge


ACTIVE = {
    "PENDING",
    "RUNNING",
    "CONFIGURING",
    "COMPLETING",
    "SUSPENDED",
    "REQUEUED",
    "RESIZING",
}


def config_path():
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    return root / "Inkly" / "tensor-picker.json"


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("Connection settings must be a JSON object")
    if settings.get("auth") not in {"keyboard-interactive", "configured"}:
        raise ValueError("Authentication must be keyboard-interactive or configured")
    if type(settings.get("hours")) is not int or not 1 <= settings["hours"] <= 336:
        raise ValueError("Time limit must be a whole number of hours from 1 to 336")
    bridge._ssh_args(settings, "ssh")
    return settings


def save_settings(path, settings):
    validate_settings(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tensor-picker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure(path):
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    print("Connection setup (saved on this computer; never enter credentials here).")
    for key, label, default in (
        ("host", "HPC username@host or SSH alias", ""),
        ("ink_path", "HPC Inkly launcher", "~/.inkly/bin/ink"),
        (
            "auth",
            "Authentication: keyboard-interactive or configured",
            "keyboard-interactive",
        ),
        ("hours", "Maximum hours per NEW calculation (1-336)", 1),
    ):
        value = input(f"{label} [{settings.get(key, default)}]: ").strip()
        settings[key] = value or settings.get(key, default)
    settings["hours"] = int(settings["hours"])
    # Save only connection preferences, never arbitrary fields/credentials.
    settings = {key: settings[key] for key in ("host", "ink_path", "auth", "hours")}
    save_settings(path, settings)
    print(f"Settings saved: {path}")
    return settings


def choose_files():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        raise ValueError(
            "File selection needs Python's Tcl/Tk component; alternatively use --files PATH ..."
        ) from None
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return filedialog.askopenfilenames(
            parent=root,
            title="Choose TENSOR Gaussian exports (Ctrl/Shift selects multiple)",
            filetypes=[("Gaussian inputs", "*.gjf *.com")],
        )
    except tk.TclError:
        raise ValueError(
            "The file dialog could not open; use --files PATH ... from a terminal"
        ) from None
    finally:
        if root is not None:
            root.destroy()


def selections(paths):
    selected = list(dict.fromkeys(Path(path).resolve() for path in paths))
    for path in selected:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:gjf|com)", path.name):
            raise ValueError(f"Unsupported Gaussian filename: {path.name}")
        if not path.is_file() or not 0 < path.stat().st_size <= 5 * 1024 * 1024:
            raise ValueError(f"Input must exist, be nonempty and at most 5 MiB: {path}")
    return selected


def plan(paths, settings, mode):
    """Preflight the whole selection before creating receipts or connecting."""
    items, targets, outputs, bundles = [], set(), set(), set()
    for path in paths:
        receipt_path = path.with_suffix(path.suffix + ".inkly.json")
        receipt = None
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            bridge._ssh_args(receipt, "ssh")
            if not re.fullmatch(r"[0-9a-f]{32}", receipt["bundle"]):
                raise ValueError(f"Invalid recovery receipt: {receipt_path}")
            if receipt["bundle"] in bundles:
                raise ValueError("Two selected files refer to the same recovery bundle")
            bundles.add(receipt["bundle"])
        elif mode == "resume":
            raise ValueError(f"No saved handoff for {path.name}; preview it first")
        connection = receipt or settings
        targets.add(tuple(connection.get(key) for key in ("host", "ink_path", "auth")))
        output = Path((receipt or {}).get("output", path.parent / "returned")).resolve()
        destination = output / path.with_suffix(".log").name
        if receipt is None and destination.exists():
            raise ValueError(
                f"Output already exists; choose a fresh research folder: {destination}"
            )
        if destination in outputs:
            raise ValueError(f"Selected files would share an output: {destination}")
        outputs.add(destination)
        items.append(
            {
                "path": path,
                "receipt_path": receipt_path,
                "receipt": receipt,
                "output": output,
            }
        )
    if len(targets) > 1:
        raise ValueError(
            "Select files for one HPC connection at a time; existing receipts keep their original connection"
        )
    return items


@contextmanager
def authentication(settings, askpass=None):
    """Reuse the established helper; do not add or replace authentication code."""
    old = {
        key: os.environ.get(key)
        for key in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "INKLY_SSH_TARGET")
    }
    try:
        if settings.get("auth") == "keyboard-interactive":
            if not os.environ.get("SSH_ASKPASS"):
                helper = (
                    Path(os.environ.get("LOCALAPPDATA", ""))
                    / "CuttlefishSsh"
                    / "connect_cuttlefish.ps1"
                )
                if askpass and Path(askpass).is_file() and not helper.is_file():
                    os.environ["SSH_ASKPASS"] = str(askpass)
                elif os.name != "nt" or not helper.is_file():
                    raise ValueError(
                        "Configure your existing site-approved SSH_ASKPASS helper first, or use configured authentication"
                    )
                else:
                    result = subprocess.run(
                        [
                            "powershell.exe",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            str(helper),
                            "-BuildOnly",
                        ],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    executable = result.stdout.strip()
                    if (
                        result.returncode
                        or not executable
                        or not Path(executable).is_file()
                    ):
                        raise ValueError(
                            "The existing local SSH helper could not be prepared"
                        )
                    os.environ["SSH_ASKPASS"] = executable
            os.environ["SSH_ASKPASS_REQUIRE"] = "force"
            os.environ["INKLY_SSH_TARGET"] = settings.get("host", "your HPC account")
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _new_handoff(session, item, settings, report=print, receipt_path=None):
    connection = item["receipt"] or settings
    args = SimpleNamespace(
        input=item["path"],
        receipt=receipt_path or item["receipt_path"],
        out=item["output"],
        host=connection["host"],
        ink_path=connection["ink_path"],
        auth=connection["auth"],
        cpus=None,
        memory_mib=None,
        hours=settings["hours"],
    )
    receipt, request = bridge._prepare(args, report=report)
    if receipt_path is None:
        item["receipt"] = receipt
    return session.exchange(request, receipt=receipt), receipt


def _archive_receipt(item):
    """Copy the exact old receipt aside before atomically replacing it."""
    path = item["receipt_path"]
    try:
        original = path.read_bytes()
        saved = json.loads(original)
    except (OSError, ValueError, UnicodeError):
        raise ValueError(
            f"Cannot safely archive the saved recovery receipt: {path}"
        ) from None
    if saved != item["receipt"]:
        raise ValueError(
            f"The saved recovery receipt changed on disk; it was not replaced: {path}"
        )

    for _ in range(5):
        archive = path.with_name(f"{path.name}.orphaned-{uuid.uuid4().hex}.bak")
        try:
            with archive.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(archive, 0o600)
            return archive, original
        except FileExistsError:
            continue
        except OSError:
            archive.unlink(missing_ok=True)
            raise
    raise OSError("Could not create a unique archive for the old recovery receipt")


def _recover_missing_bundle(session, item, settings, report):
    old_receipt = item["receipt"]
    archive, original = _archive_receipt(item)
    temporary = item["receipt_path"].with_name(
        f".{item['receipt_path'].name}.recovery-{uuid.uuid4().hex}.tmp"
    )
    try:
        # Persist a fresh bundle ID to a temporary receipt first. The original
        # receipt remains in place until the replacement is ready to publish.
        _, request = bridge._prepare(
            SimpleNamespace(
                input=item["path"],
                receipt=temporary,
                out=item["output"],
                host=old_receipt["host"],
                ink_path=old_receipt["ink_path"],
                auth=old_receipt["auth"],
                cpus=None,
                memory_mib=None,
                hours=settings["hours"],
            ),
            report=lambda _message: None,
        )
        if item["receipt_path"].read_bytes() != original:
            raise ValueError(
                "The saved recovery receipt changed during recovery; no replacement was made"
            )
        os.replace(temporary, item["receipt_path"])
    except Exception:
        temporary.unlink(missing_ok=True)
        # If publication did not happen, keep the canonical old receipt and
        # discard only the redundant archive copy.
        try:
            if item["receipt_path"].read_bytes() == original:
                archive.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    new_receipt = json.loads(item["receipt_path"].read_text(encoding="utf-8"))
    item["receipt"] = new_receipt
    item["recovered"] = True
    item["recovered_from"] = archive
    report(f"Archived the missing-bundle receipt: {archive.name}")
    report(f"Created a new recovery receipt: {item['receipt_path'].name}")
    response = session.exchange(request, receipt=new_receipt)
    report(
        f"Re-prepared {item['path'].name} for preview only. No Slurm job was submitted."
    )
    return response


def prepare(session, item, settings, report=print, recover_bundles=None):
    if item["receipt"] is None:
        response, _ = _new_handoff(session, item, settings, report=report)
    else:
        report(
            f"Using saved handoff: {item['path'].name} (its original connection applies)"
        )
        try:
            response = session.exchange({"action": "status"}, receipt=item["receipt"])
        except bridge.MissingBundleError:
            approved = set(recover_bundles or ())
            if item["receipt"]["bundle"] not in approved:
                raise
            response = _recover_missing_bundle(session, item, settings, report)

    if response["name"] != item["path"].name:
        raise ValueError("The receipt belongs to a different input filename")
    # A reused receipt must still describe the selected export, not an older edit.
    if hashlib.sha256(item["path"].read_bytes()).hexdigest() != response["sha256"]:
        raise ValueError(
            f"{item['path'].name} changed since preparation; keep its receipt and review the original handoff"
        )
    item["response"] = response


def summary(items):
    print("\nFile | CPUs | Gaussian MB | Slurm MiB | Hours | Stages | Status")
    for item in items:
        response = item["response"]
        resource = response["resources"]
        print(
            f"{response['name']} | {resource['cpus']} | {resource['gaussian_memory_mb']} | {resource['slurm_memory_mib']} | {resource['hours']} | {response['stages']} | {response.get('state', response['submission'])}"
        )
    print("CPU/memory come from TENSOR exports unless a saved handoff used overrides.")
    print(
        "Slurm memory includes cluster headroom. Originals are unchanged; scientific setup/analysis stays in TENSOR."
    )


def show_scripts(items):
    for item in items:
        response = item["response"]
        print(f"\n--- {item['path'].name} ---")
        print(response.get("script", "Already submitted; use its recorded job status."))


def confirm(items):
    ready = [item for item in items if item["response"]["submission"] == "PREPARED"]
    if not ready:
        return False
    print("\nNew jobs awaiting approval:")
    for item in ready:
        resource = item["response"]["resources"]
        print(
            f"  {item['path'].name}: {resource['cpus']} CPUs, {resource['slurm_memory_mib']} MiB, {resource['hours']} hour(s)"
        )
    cpus = sum(item["response"]["resources"]["cpus"] for item in ready)
    memory = sum(item["response"]["resources"]["slurm_memory_mib"] for item in ready)
    print(
        f"{len(ready)} independent jobs; if all run together: {cpus} CPUs, {memory} MiB total."
    )
    print("Slurm controls when they start. Each job keeps the time limit shown above.")
    if not sys.stdin.isatty():
        raise ValueError("Submission requires an interactive terminal")
    while True:
        answer = input(
            f"Type 'submit {len(ready)}' to submit these jobs, 's' for scripts, or Enter to leave prepared: "
        ).strip()
        if answer.lower() == "s":
            show_scripts(ready)
        else:
            return answer == f"submit {len(ready)}"


def collect(session, item, report=print):
    result = session.exchange({"action": "fetch"}, receipt=item["receipt"])
    expected = item["path"].with_suffix(".log").name
    if result["name"] != expected:
        raise ValueError("Returned output does not match the selected input")
    path = item["output"] / expected
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != result["sha256"]:
            raise ValueError(
                f"Existing output differs; preserved without overwrite: {path}"
            )
        report(f"Already returned; checksum verified: {path}")
    else:
        report(f"Verified output saved: {bridge.retrieve(result, item['output'])}")
    return path


def watch(session, items, interval):
    pending = list(items)
    states = {}
    while pending:
        for item in list(pending):
            response = session.exchange({"action": "status"}, receipt=item["receipt"])
            state = response["state"]
            key = item["receipt"]["bundle"]
            if states.get(key) != state:
                print(
                    f"{item['path'].name}: job {response.get('job_id', 'unconfirmed')} {state}"
                )
                states[key] = state
            if state == "COMPLETED" and response.get("exit_code") == "0:0":
                collect(session, item)
                pending.remove(item)
            elif state not in ACTIVE:
                raise ValueError(
                    f"{item['path'].name}: {state} (exit {response.get('exit_code')}); stopped monitoring, other jobs are not cancelled. Keep receipts; no retry was submitted"
                )
        if pending:
            time.sleep(interval)
    print("In TENSOR: Analysis & 3D view -> Add logs -> choose the returned files.")


def handoff(items, settings, mode, ssh, interval=30):
    connection = items[0]["receipt"] or settings
    print(f"HPC: {connection['host']} | Inkly: {connection['ink_path']}")
    with authentication(connection), bridge.Session(connection, ssh) as session:
        for item in items:
            prepare(session, item, settings)
        summary(items)
        if mode == "preview":
            if (
                sys.stdin.isatty()
                and input("Type 's' to show generated scripts, or Enter to finish: ")
                .strip()
                .lower()
                == "s"
            ):
                show_scripts(items)
            print(
                "Preview finished. Nothing submitted. Run the launcher again and choose Run or Resume."
            )
            return
        # Detect uncertain/failed jobs before submitting any new ones in this selection.
        for item in items:
            response = item["response"]
            destination = item["output"] / item["path"].with_suffix(".log").name
            if response["submission"] == "PREPARED" and destination.exists():
                raise ValueError(
                    f"Output already exists; review this saved handoff before submitting: {destination}"
                )
            if response["submission"] != "PREPARED" and not (
                response.get("state") in ACTIVE
                or (
                    response.get("state") == "COMPLETED"
                    and response.get("exit_code") == "0:0"
                )
            ):
                raise ValueError(
                    f"{item['path'].name} has an unresolved job state; inspect its saved handoff before proceeding"
                )
        if any(item["response"]["submission"] == "PREPARED" for item in items):
            if not confirm(items):
                print(
                    "Nothing submitted. Existing jobs continue; keep all receipts to resume."
                )
                return
            for item in items:
                if item["response"]["submission"] == "PREPARED":
                    response = session.exchange(
                        {"action": "submit", "confirm": True}, receipt=item["receipt"]
                    )
                    print(f"Submitted {item['path'].name}: job {response['job_id']}")
        print("Ctrl+C stops monitoring; it does not cancel submitted jobs.")
        watch(session, items, interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--configure", action="store_true", help="Edit saved connection preferences"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check local prerequisites; no picker or SSH connection",
    )
    parser.add_argument("--mode", choices=("preview", "run", "resume"))
    parser.add_argument(
        "--files", nargs="+", help="Input paths instead of a file dialog"
    )
    parser.add_argument("--hours", type=int, help="Maximum hours for new handoffs only")
    args = parser.parse_args(argv)
    try:
        path = config_path()
        if args.configure:
            configure(path)
            return 0
        if args.check:
            import tkinter

            print(f"Python {sys.version.split()[0]}; Tcl/Tk {tkinter.TkVersion}")
            print(f"OpenSSH: {shutil.which('ssh') or 'MISSING'}")
            print(
                f"Settings: {path} ({'saved' if path.exists() else 'configure on first launch'})"
            )
            if path.exists():
                validate_settings(json.loads(path.read_text(encoding="utf-8")))
            return 0 if shutil.which("ssh") else 1
        mode = args.mode
        if mode is None:
            print(
                "1 Preview only (default)\n2 Run selected calculations\n3 Resume saved handoffs"
            )
            choice = input("Choose [1]: ").strip() or "1"
            if choice not in {"1", "2", "3"}:
                raise ValueError("Choose 1, 2 or 3")
            mode = {"1": "preview", "2": "run", "3": "resume"}[choice]
        paths = selections(args.files if args.files is not None else choose_files())
        if not paths:
            print("No files selected. Nothing changed or connected.")
            return 0
        settings = (
            validate_settings(json.loads(path.read_text(encoding="utf-8")))
            if path.exists()
            else configure(path)
        )
        if args.hours is not None:
            settings = validate_settings({**settings, "hours": args.hours})
        items = plan(paths, settings, mode)
        print(
            f"\nSelected {len(items)} input(s). New handoffs use at most {settings['hours']} hour(s) each."
        )
        print("Existing handoffs keep their original resources and time limits.")
        ssh = shutil.which("ssh")
        if ssh is None:
            raise ValueError("Install the Windows OpenSSH Client before connecting")
        handoff(items, settings, mode, ssh)
        return 0
    except (KeyboardInterrupt, EOFError):
        print(
            "Stopped. Keep the receipts and select the same files with Resume; existing HPC jobs continue.",
            file=sys.stderr,
        )
        return 130
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ImportError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            f"TENSOR picker: {exc}\nKeep any receipts; use Resume before preparing another copy.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
