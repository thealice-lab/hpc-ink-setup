#!/usr/bin/env python3
"""Standalone OpenSSH transport; no Inkly or scientific packages on Windows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid


class MissingBundleError(ValueError):
    """The HPC no longer has the bundle referenced by a saved receipt."""


def _ssh_args(receipt, ssh, session=False):
    host = receipt["host"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
        raise ValueError("Use an OpenSSH host alias or user@host")
    ink = receipt["ink_path"]
    if not re.fullmatch(r"(?:/|~/)[A-Za-z0-9_./-]+", ink) or ".." in ink.split("/"):
        raise ValueError("Use an absolute or ~/ path to the remote Inkly launcher")
    command = (
        '"$HOME"/' + shlex.quote(ink[2:]) if ink.startswith("~/") else shlex.quote(ink)
    ) + (" tensor --session" if session else " tensor")
    options = []
    if receipt.get("auth") == "keyboard-interactive":
        options = [
            "-o",
            "PreferredAuthentications=keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
        ]
    return [
        ssh,
        "-T",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        *options,
        host,
        command,
    ]


def _response(raw, receipt):
    try:
        response = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError(
            "No valid Inkly response. Check SSH login, host trust and matching HPC version; "
            "keep the receipt and resume before any new submission"
        ) from None
    if not isinstance(response, dict):
        raise ValueError("Expected an Inkly response object")
    if not response.get("ok"):
        error = response.get("error", "Remote operation failed")
        if error == "Unknown bundle; prepare a new input first":
            raise MissingBundleError(error)
        raise ValueError(error)
    if response.get("bundle") != receipt["bundle"]:
        raise ValueError("Response bundle does not match the receipt")
    return response


def exchange(receipt, request, ssh):
    try:
        result = subprocess.run(
            _ssh_args(receipt, ssh),
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            "SSH timed out. Keep the receipt; check status before retrying"
        ) from None
    response = _response(result.stdout, receipt)
    if result.returncode != 0:
        raise ValueError(
            "SSH failed; keep the receipt and check status before retrying"
        )
    return response


class Session:
    """Use one authenticated SSH connection for preview, decision and monitoring."""

    def __init__(self, receipt, ssh):
        self.receipt = receipt
        self.process = subprocess.Popen(
            _ssh_args(receipt, ssh, session=True),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.responses = queue.Queue()
        self.error_tail = b""
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.error_reader.start()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read_errors(self):
        while True:
            chunk = self.process.stderr.read(1024)
            if not chunk:
                return
            self.error_tail = (self.error_tail + chunk)[-8192:]

    def _read(self):
        try:
            while True:
                raw = self.process.stdout.readline(96 * 1024 * 1024 + 1)
                if not raw:
                    break
                self.responses.put(raw)
                if len(raw) > 96 * 1024 * 1024:
                    break
        finally:
            self.responses.put(None)

    def exchange(self, request, *, receipt=None):
        receipt = receipt or self.receipt
        if any(
            receipt.get(key) != self.receipt.get(key)
            for key in ("host", "ink_path", "auth")
        ):
            raise ValueError("One SSH session cannot mix connection settings")
        request = {**request, "bundle": receipt["bundle"]}
        # Cover a blocked input transfer/authentication as well as response reads.
        deadline = threading.Timer(180, self.process.kill)
        deadline.daemon = True
        deadline.start()
        try:
            self.process.stdin.write(json.dumps(request).encode() + b"\n")
            self.process.stdin.flush()
            raw = self.responses.get(timeout=180)
        except (OSError, queue.Empty):
            raise ValueError(
                "SSH interrupted or timed out; keep the receipt and resume"
            ) from None
        finally:
            deadline.cancel()
        if raw is None:
            self.error_reader.join(timeout=1)
            error = self.error_tail.lower()
            if b"could not resolve hostname" in error:
                raise ValueError(
                    "Cannot resolve SSH host; check the network/VPN and resume"
                )
            if b"permission denied" in error:
                raise ValueError(
                    "SSH authentication denied; complete the local sign-in and resume"
                )
            if b"host key verification failed" in error:
                raise ValueError(
                    "SSH host key is not trusted; verify it through the established site procedure"
                )
            raise ValueError(
                "SSH closed; check login and matching HPC version, then resume"
            )
        if len(raw) > 96 * 1024 * 1024:
            raise ValueError("Response exceeds transfer limit")
        return _response(raw, receipt)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=1)
        self.error_reader.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()


def retrieve(response, output):
    name = response["name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.log", name):
        raise ValueError("Unsafe output filename")
    data = base64.b64decode(response["data"], validate=True)
    if hashlib.sha256(data).hexdigest() != response["sha256"]:
        raise ValueError("Output checksum mismatch; no file written")
    output.mkdir(parents=True, exist_ok=True)
    path = output / name
    descriptor, temporary = tempfile.mkstemp(dir=output, prefix=".tensor-")
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Atomic publication, with no overwrite on Windows or Linux.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def _prepare(args, report=print):
    with args.input.open("rb") as source:
        data = source.read(5 * 1024 * 1024 + 1)
    if not data or len(data) > 5 * 1024 * 1024:
        raise ValueError("Input must be nonempty and at most 5 MiB")
    path = args.receipt or args.input.with_suffix(args.input.suffix + ".inkly.json")
    receipt = {
        "host": args.host,
        "ink_path": args.ink_path,
        "bundle": uuid.uuid4().hex,
        "auth": args.auth,
        "output": str(
            (getattr(args, "out", None) or args.input.parent / "returned").resolve()
        ),
    }
    # Save the recovery ID before crossing the network boundary. Never overwrite it.
    with path.open("x", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        json.dump(receipt, out)
        out.flush()
        os.fsync(out.fileno())
    report(f"Recovery receipt: {path}")
    return receipt, {
        "action": "prepare",
        "name": args.input.name,
        "data": base64.b64encode(data).decode("ascii"),
        "cpus": args.cpus,
        "memory_mib": args.memory_mib,
        "hours": args.hours,
    }


def _preview(response):
    resources = response.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("Install the matching HPC version for the resource preview")
    print(f"Prepared {response['name']} ({response['stages']} Gaussian stages)")
    print(f"CPUs: {resources['cpus']} ({resources['cpus_source']})")
    print(f"Exported CPUs by stage: {resources['exported_cpus']}")
    print(f"Exported Gaussian memory by stage (MB): {resources['exported_memory_mb']}")
    print(f"Gaussian runtime memory: {resources['gaussian_memory_mb']} MB")
    print(
        f"Slurm total memory: {resources['slurm_memory_mib']} MiB ({resources['memory_source']})"
    )
    print(f"Wall time limit: {resources['hours']} hour(s)")
    print("Original input is preserved. TENSOR retains scientific setup and analysis.")


def _confirm(response):
    if not sys.stdin.isatty():
        raise ValueError(
            "Submission needs an interactive terminal; use run --preview for preparation only"
        )
    while True:
        answer = input("Submit this calculation? [y/N/s=show SBATCH] ").strip().lower()
        if answer == "s":
            print(response["script"])
        else:
            return answer == "y"


def _watch(session, receipt, output, interval):
    previous = None
    while True:
        response = session.exchange({"action": "status"})
        state = response["state"]
        if state != previous:
            print(f"Job {response.get('job_id', 'unconfirmed')}: {state}")
            previous = state
        if state == "COMPLETED" and response.get("exit_code") == "0:0":
            result = session.exchange({"action": "fetch"})
            path = retrieve(result, output)
            print(f"Verified output saved: {path}")
            print("In TENSOR: Analysis & 3D view -> Add logs -> select this file.")
            return 0
        if state not in {
            "PENDING",
            "RUNNING",
            "CONFIGURING",
            "COMPLETING",
            "SUSPENDED",
            "REQUEUED",
            "RESIZING",
        }:
            raise ValueError(
                f"Job is {state} (exit {response.get('exit_code')}); keep the receipt, inspect the job, and resume when appropriate. No new submission was attempted"
            )
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", default="ssh", help="Native OpenSSH executable")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("run", "prepare"):
        prepare = commands.add_parser(
            action,
            help="Guided handoff" if action == "run" else "Preview only; never submits",
        )
        prepare.add_argument("input", type=Path)
        prepare.add_argument("--host", required=True, help="OpenSSH alias or user@host")
        prepare.add_argument("--ink-path", default="~/.inkly/bin/ink")
        prepare.add_argument(
            "--auth",
            choices=("keyboard-interactive", "configured"),
            default="keyboard-interactive",
            help="Cuttlefish MFA by default; configured uses existing OpenSSH authentication settings",
        )
        prepare.add_argument("--receipt", type=Path)
        prepare.add_argument("--cpus", type=int, help="Override exported CPU settings")
        prepare.add_argument(
            "--memory-mib",
            type=int,
            help="Override TOTAL Slurm MiB, not Gaussian memory",
        )
        prepare.add_argument("--hours", type=int, default=1)
        if action == "run":
            prepare.add_argument("--out", type=Path)
            prepare.add_argument(
                "--preview",
                action="store_true",
                help="Prepare and inspect without submission",
            )
            prepare.add_argument("--poll-seconds", type=int, default=30)
    resume = commands.add_parser(
        "resume", help="Recover using the recorded bundle; never blindly resubmit"
    )
    resume.add_argument("receipt", type=Path)
    resume.add_argument("--out", type=Path)
    resume.add_argument("--poll-seconds", type=int, default=30)
    for action in ("submit", "status", "fetch"):
        command = commands.add_parser(action)
        command.add_argument("receipt", type=Path)
        if action == "submit":
            command.add_argument("--confirm", action="store_true", required=True)
        if action == "fetch":
            command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in {"run", "resume"} and args.poll_seconds < 1:
            raise ValueError("--poll-seconds must be positive")
        if args.action in {"prepare", "run"}:
            receipt, request = _prepare(args)
        else:
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            request = {
                "action": args.action,
                "confirm": args.action == "submit" and args.confirm,
            }
        if args.action in {"run", "resume"}:
            with Session(receipt, args.ssh) as session:
                response = session.exchange(
                    request if args.action == "run" else {"action": "status"}
                )
                if response.get("submission") == "PREPARED":
                    _preview(response)
                    if args.action == "run" and args.preview:
                        print(response["script"])
                        print(
                            "Prepared only. Use resume with the recovery receipt to continue."
                        )
                        return 0
                    if not _confirm(response):
                        print(
                            "Prepared only; nothing submitted. Use resume with the receipt to continue."
                        )
                        return 0
                    response = session.exchange({"action": "submit", "confirm": True})
                    print(
                        f"Submitted job {response['job_id']}. Ctrl+C stops waiting, not the job."
                    )
                return _watch(
                    session,
                    receipt,
                    args.out
                    or Path(
                        receipt.get(
                            "output",
                            str(args.receipt.parent / "returned")
                            if args.receipt
                            else "returned",
                        )
                    ),
                    args.poll_seconds,
                )
        request.update(action=args.action, bundle=receipt["bundle"])
        response = exchange(receipt, request, args.ssh)
        if args.action == "prepare":
            _preview(response)
            print(response["script"])
            print("Prepared only. Runtime resource directives use the preview above.")
        elif args.action == "fetch":
            print(f"Verified output saved: {retrieve(response, args.out)}")
        else:
            print(json.dumps(response, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print(
            "Stopped. Keep the recovery receipt and use resume; submitted jobs are not cancelled.",
            file=sys.stderr,
        )
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"TENSOR bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
