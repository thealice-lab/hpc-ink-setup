from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from inkly.core.permissions import open_shared_directory, validate_shared_acl


CREDENTIAL_MARKERS = re.compile(
    r"\b(?:password|passwd|passphrase|credentials?|private[ _-]?key|"
    r"api[ _-]?key|access[ _-]?token|refresh[ _-]?token|bearer|authorization)\b"
    r"|\b[\w-]*(?:token|secret|access_key|private_key)[\w-]*[\"']?\s*[:=]\s*\S+"
    r"|-----BEGIN[^\n]*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    r"|[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@",
    re.IGNORECASE,
)
WITHHELD = "[withheld: possible credentials; private history retains the original]"


def filtered_content(content: str) -> tuple[str, bool]:
    """Withhold whole turns with common credential markers, not just matched spans."""
    if CREDENTIAL_MARKERS.search(content) or re.search(
        r"\b(?:backend|plugin|retrieval) error\s*:", content, re.IGNORECASE
    ):
        return WITHHELD, True
    return content, False


def _event(turn, index, session_id):
    if (
        not isinstance(turn, dict)
        or not isinstance(turn.get("role"), str)
        or turn["role"] not in {"user", "assistant"}
        or not isinstance(turn.get("content"), str)
    ):
        raise ValueError("Invalid conversation turn")
    content, redacted = filtered_content(turn["content"])
    try:
        timestamp = datetime.fromisoformat(turn.get("timestamp", ""))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        timestamp = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "event_type": "user_prompt" if turn["role"] == "user" else "ai_response",
        "timestamp": timestamp.isoformat(),
        "session": {
            "session_id": session_id,
            "user_id": None,
            "host": "redacted",
            "pid": os.getpid(),
        },
        "payload": {
            "turn_index": index,
            "chars": len(content),
            "content": content,
            "redacted": redacted,
        },
    }


def publish_conversation_snapshot(shared, name: str, history: list[dict]) -> None:
    """Atomically publish an owner's complete filtered snapshot, never a partial file."""
    root = open_shared_directory(Path(shared.directory), shared.group)
    subject = hashlib.sha256(name.encode("utf-8")).hexdigest()
    filename = f"chat-{os.getuid()}-{subject}.jsonl"
    temporary = f".chat-{uuid4().hex}.tmp"
    descriptor = None
    try:
        try:
            existing = os.stat(filename, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_nlink != 1
                or existing.st_uid != os.getuid()
                or existing.st_gid != os.fstat(root).st_gid
                or stat.S_IMODE(existing.st_mode) != 0o640
            ):
                raise PermissionError("Unexpected shared conversation file")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root,
        )
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            info.st_uid != os.getuid()
            or info.st_gid != os.fstat(root).st_gid
            or info.st_nlink != 1
        ):
            raise PermissionError("Invalid shared conversation ownership")
        validate_shared_acl(descriptor, directory=False)
        session_id = uuid4().hex
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
            for index, turn in enumerate(history):
                event = _event(turn, index, session_id)
                output.write(json.dumps(event, separators=(",", ":")) + "\n")
            output.flush()
            os.fsync(descriptor)
        os.fchmod(descriptor, 0o640)
        validate_shared_acl(descriptor, directory=False)
        os.rename(temporary, filename, src_dir_fd=root, dst_dir_fd=root)
        os.fsync(root)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            try:
                os.unlink(temporary, dir_fd=root)
            except FileNotFoundError:
                pass
        finally:
            os.close(root)
