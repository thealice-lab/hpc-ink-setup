#!/usr/bin/env python3
"""Standalone OpenSSH transport; no Inkly or scientific packages on Windows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid


def exchange(receipt, request, ssh):
    host = receipt["host"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
        raise ValueError("Use an OpenSSH host alias or user@host")
    ink = receipt["ink_path"]
    if not re.fullmatch(r"(?:/|~/)[A-Za-z0-9_./-]+", ink) or ".." in ink.split("/"):
        raise ValueError("Use an absolute or ~/ path to the remote Inkly launcher")
    command = (
        '"$HOME"/' + shlex.quote(ink[2:]) if ink.startswith("~/") else shlex.quote(ink)
    ) + " tensor"
    try:
        result = subprocess.run(
            [
                ssh,
                "-T",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                host,
                command,
            ],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            "SSH timed out. Keep the receipt; check status before retrying"
        ) from None
    try:
        response = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError(
            "No valid Inkly response. Check SSH login, host trust and installed version; "
            "keep the receipt and check status before any new submission"
        ) from None
    if not isinstance(response, dict):
        raise ValueError("Expected an Inkly response object")
    if not response.get("ok"):
        raise ValueError(response.get("error", "Remote operation failed"))
    if result.returncode != 0:
        raise ValueError(
            "SSH failed; keep the receipt and check status before retrying"
        )
    if response.get("bundle") != receipt["bundle"]:
        raise ValueError("Response bundle does not match the receipt")
    return response


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", default="ssh", help="Native OpenSSH executable")
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare", help="Transfer and preview; never submits")
    prepare.add_argument("input", type=Path)
    prepare.add_argument("--host", required=True, help="OpenSSH alias or user@host")
    prepare.add_argument("--ink-path", default="~/.inkly/bin/ink")
    prepare.add_argument("--receipt", type=Path, required=True)
    prepare.add_argument("--cpus", type=int, required=True)
    prepare.add_argument(
        "--memory-mib", type=int, required=True, help="Total Slurm memory"
    )
    prepare.add_argument("--hours", type=int, default=1)
    for action in ("submit", "status", "fetch"):
        command = commands.add_parser(action)
        command.add_argument("receipt", type=Path)
        if action == "submit":
            command.add_argument("--confirm", action="store_true", required=True)
        if action == "fetch":
            command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            with args.input.open("rb") as source:
                data = source.read(5 * 1024 * 1024 + 1)
            if not data or len(data) > 5 * 1024 * 1024:
                raise ValueError("Input must be nonempty and at most 5 MiB")
            receipt = {
                "host": args.host,
                "ink_path": args.ink_path,
                "bundle": uuid.uuid4().hex,
            }
            # Save the recovery ID before crossing the network boundary.
            with args.receipt.open("x", encoding="utf-8") as out:
                os.chmod(args.receipt, 0o600)
                json.dump(receipt, out)
                out.flush()
                os.fsync(out.fileno())
            request = {
                "name": args.input.name,
                "data": base64.b64encode(data).decode("ascii"),
                "cpus": args.cpus,
                "memory_mib": args.memory_mib,
                "hours": args.hours,
            }
        else:
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            request = {"confirm": args.action == "submit" and args.confirm}
        request.update(action=args.action, bundle=receipt["bundle"])
        response = exchange(receipt, request, args.ssh)
        if args.action == "prepare":
            print(response["script"])
            print("Prepared only. Runtime resource directives use the preview above.")
        elif args.action == "fetch":
            print(f"Verified output saved: {retrieve(response, args.out)}")
        else:
            print(json.dumps(response, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"TENSOR bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
