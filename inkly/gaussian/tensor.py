"""One private Gaussian bundle per TENSOR handoff; JSON over authenticated SSH."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from inkly.cluster_profile import resolve_cluster_profile
from inkly.core.storage import history_lock, private_directory, private_open
from inkly.gaussian.job_spec import GaussianJobSpec
from inkly.gaussian.sbatch import generate_sbatch


MAX_INPUT = 5 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024


def _read(path, limit=MAX_OUTPUT):
    with os.fdopen(private_open(path, os.O_RDONLY), "rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("File exceeds the transfer limit")
    return data


def _write(path, data):
    with os.fdopen(
        private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
    ) as out:
        out.write(data)


def _save(path, record):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(record, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def _validate_input(data):
    """Accept self-contained TENSOR Link1 inputs, without external file paths."""
    text = data.decode("utf-8")
    if "\x00" in text:
        raise ValueError("Gaussian input must be a text file without NUL bytes")
    sections = re.split(r"(?im)^\s*--link1--\s*$", text)
    for section in sections:
        header = True
        has_route = False
        for line in section.splitlines():
            if line.lstrip().startswith("@"):
                raise ValueError("External Gaussian include files are unsupported")
            if header and line.lstrip().startswith("#"):
                has_route, header = True, False
            if header and line.lstrip().startswith("%"):
                match = re.fullmatch(r"\s*%(\w+)\s*=\s*(\S+)\s*", line)
                if not match:
                    raise ValueError("Unsupported Gaussian Link0 directive")
                key, value = match.groups()
                if key.lower() in {"chk", "rwf"}:
                    if not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]*\." + key.lower(), value, re.I
                    ):
                        raise ValueError("Checkpoint/RWF must be local filenames")
                elif key.lower() not in {"mem", "nproc", "nprocshared"}:
                    raise ValueError("Unsupported Gaussian Link0 directive")
        if not has_route:
            raise ValueError("Every Gaussian Link1 section needs a route")
    return len(sections)


def _scheduler(args, directory):
    return subprocess.run(
        args, cwd=directory, capture_output=True, text=True, timeout=30, check=False
    )


def _state(directory, receipt):
    job = receipt.get("job_id")
    if not job:
        return {"state": receipt["submission"], "exit_code": None}
    result = _scheduler(
        ["sacct", "-j", job, "-X", "-n", "-P", "-o", "JobIDRaw,State,ExitCode"],
        directory,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.strip().split("|")
            if len(fields) >= 3 and fields[0] == job:
                return {"state": fields[1], "exit_code": fields[2]}
    result = _scheduler(
        ["squeue", "--noheader", "--jobs", job, "--format", "%A|%T"], directory
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.strip().split("|")
            if len(fields) == 2 and fields[0] == job:
                return {"state": fields[1], "exit_code": None}
    return {"state": "UNKNOWN", "exit_code": None}


def handle(request, root=None):
    """Process exactly one explicit operation; never retry a submission."""
    if os.name != "posix":
        raise ValueError("The Inkly TENSOR endpoint requires the Linux HPC")
    if not isinstance(request, dict):
        raise ValueError("Expected a JSON request object")
    bundle = request.get("bundle", "")
    if not isinstance(bundle, str) or not re.fullmatch(r"[0-9a-f]{32}", bundle):
        raise ValueError("Invalid bundle ID")
    action = request.get("action")
    if action not in {"prepare", "submit", "status", "fetch"}:
        raise ValueError("Unsupported operation")
    root = private_directory(root or Path.home() / ".inkly" / "tensor")
    directory = root / bundle
    if action == "prepare":
        name = request["name"]
        spec = GaussianJobSpec(name, request["cpus"], request["memory_mib"])
        data = base64.b64decode(request["data"], validate=True)
        if not data or len(data) > MAX_INPUT:
            raise ValueError("Input must be nonempty and at most 5 MiB")
        stages = _validate_input(data)
        digest = hashlib.sha256(data).hexdigest()
        script = generate_sbatch(
            spec,
            resolve_cluster_profile(),
            tensor_output=True,
            input_sha256=digest,
            hours=request["hours"],
        )
        # Exclusive creation makes interrupted preparation safe to identify.
        directory.mkdir(mode=0o700)
        private_directory(directory)
        _write(directory / name, data)
        _write(directory / "job.sbatch", script.encode())
        receipt = {
            "bundle": bundle,
            "name": name,
            "sha256": digest,
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "stages": stages,
            "submission": "PREPARED",
        }
        _save(directory / "receipt.json", receipt)
        return {**receipt, "script": script}

    if not directory.is_dir():
        raise ValueError("Unknown bundle; prepare a new input first")
    private_directory(directory)
    with history_lock(directory, "submission"):
        receipt = json.loads(_read(directory / "receipt.json"))
        if action == "submit":
            if request.get("confirm") is not True:
                raise ValueError("Submission requires explicit confirmation")
            if receipt["submission"] != "PREPARED":
                raise ValueError(
                    "Already attempted; check status, never blindly resubmit"
                )
            for name, key in [
                (receipt["name"], "sha256"),
                ("job.sbatch", "script_sha256"),
            ]:
                if hashlib.sha256(_read(directory / name)).hexdigest() != receipt[key]:
                    raise ValueError("Prepared input or script changed; prepare again")
            # Persist uncertainty BEFORE calling Slurm. A disconnect/timeout must
            # not allow a second sbatch, even if its response was never received.
            receipt["submission"] = "SUBMISSION_UNKNOWN"
            _save(directory / "receipt.json", receipt)
            result = _scheduler(["sbatch", "--parsable", "job.sbatch"], directory)
            match = re.fullmatch(r"([0-9]+)(?:;[A-Za-z0-9_.-]+)?\s*", result.stdout)
            if result.returncode != 0 or not match:
                raise ValueError(
                    "Submission unconfirmed; inspect squeue before any new attempt"
                )
            receipt.update(job_id=match[1], submission="SUBMITTED")
            _save(directory / "receipt.json", receipt)
            return receipt
        status = _state(directory, receipt)
        response = {**receipt, **status}
        if action == "status":
            return response
        if status != {"state": "COMPLETED", "exit_code": "0:0"}:
            raise ValueError(
                "Output is unavailable until Slurm reports COMPLETED / 0:0"
            )
        name = Path(receipt["name"]).stem + ".log"
        data = _read(directory / name)
        if (
            data.count(b"Normal termination of Gaussian") < receipt["stages"]
            or b"Error termination" in data
        ):
            raise ValueError("Gaussian output is incomplete or reports an error")
        return {
            "bundle": bundle,
            "name": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        }


def main():
    try:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Request exceeds transfer limit")
        request = json.loads(raw)
        response = handle(request)
        print(json.dumps({"ok": True, **response}))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        # Never echo a payload, research input, scheduler output or traceback.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"ok": False, "error": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
