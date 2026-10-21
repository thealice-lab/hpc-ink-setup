from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from inkly.core.permissions import open_shared_directory
from inkly.core.shared_conversations import publish_conversation_snapshot
from inkly.core.storage import (
    atomic_history,
    history_lock,
    private_directory,
    private_open,
)


class ConversationManager:
    """
    Persistent per-user conversation history.

    Storage and prompt-context generation are intentionally separate:
    - full history is stored on disk
    - processed history is generated for prompt injection
    """

    def __init__(self, config):
        # Store config reference so conversation limits and summarization
        # settings can be read from one place.
        self.config = config

        # Master on/off switch for conversation persistence.
        self.enabled = self.config.conversation.enabled

        self.recovery_dir = Path.home() / ".inkly" / "conversations"
        state = getattr(config, "state", None)
        configured = getattr(state, "conversation_dir", self.recovery_dir)
        self.base_dir = Path(os.path.abspath(Path(configured).expanduser()))
        self._storage_warning_shown = False
        logging = getattr(config, "logging", None)
        self.shared = (
            getattr(logging, "shared", None)
            if getattr(logging, "enabled", False)
            else None
        )
        self.archive_dir = None
        if (
            os.name == "posix"
            and self.shared is not None
            and self.shared.enabled
            and self.shared.archive_conversations
        ):
            self.archive_dir = Path(self.shared.directory) / f"history-{os.getuid()}"

    @staticmethod
    def _filename(user_id: str) -> str:
        if not isinstance(user_id, str):
            raise TypeError("Conversation user IDs must be strings")
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", user_id):
            return user_id + ".json"
        return "user-" + hashlib.sha256(user_id.encode("utf-8")).hexdigest() + ".json"

    def _directories(self) -> list[Path]:
        directories = list(dict.fromkeys((self.recovery_dir, self.base_dir)))
        if self.archive_dir is not None:
            try:
                descriptor = open_shared_directory(
                    Path(self.shared.directory), self.shared.group
                )
                os.close(descriptor)
                directories.append(self.archive_dir)
            except OSError:
                self._warn_storage()
        return directories

    def _warn_storage(self) -> None:
        if not self._storage_warning_shown:
            try:
                warnings.warn(
                    "Inkly history storage is unavailable or invalid; originals are "
                    "preserved and private recovery is used when possible.",
                    RuntimeWarning,
                    stacklevel=3,
                )
            except RuntimeWarning:
                pass
            self._storage_warning_shown = True

    def _lock_directory(self) -> Path:
        for directory in self._directories():
            try:
                return private_directory(directory)
            except OSError:
                self._warn_storage()
        raise OSError("No usable private conversation storage is available")

    def _load_sources(self, name: str) -> tuple[list[dict], list[Path]]:
        merged = []
        seen = set()
        writable = []
        for directory in self._directories():
            try:
                private_directory(directory)
                try:
                    descriptor = private_open(directory / name, os.O_RDONLY)
                except FileNotFoundError:
                    records = []
                else:
                    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                        records = json.load(source)
                if not isinstance(records, list) or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("role"), str)
                    or not isinstance(item.get("content"), str)
                    for item in records
                ):
                    raise ValueError("Invalid conversation history")
            except (OSError, ValueError):
                self._warn_storage()
                continue
            writable.append(directory)
            occurrences = {}
            for record in records:
                record = record.copy()
                original = {
                    key: value for key, value in record.items() if key != "turn_id"
                }
                digest = hashlib.sha256(
                    json.dumps(original, sort_keys=True, ensure_ascii=False).encode(
                        "utf-8"
                    )
                ).hexdigest()
                occurrence = occurrences.get(digest, 0)
                occurrences[digest] = occurrence + 1
                turn_id = record.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id:
                    turn_id = f"legacy-{digest}-{occurrence}"
                record["turn_id"] = turn_id
                if turn_id not in seen:
                    merged.append(record)
                    seen.add(turn_id)
        if not writable:
            raise OSError("No usable private conversation storage is available")
        merged.sort(key=lambda record: str(record.get("timestamp", "")))
        return merged, writable

    def _persist(self, name: str, records: list[dict], directories: list[Path]) -> None:
        saved = False
        for directory in directories:
            try:
                if directory == self.archive_dir:
                    descriptor = open_shared_directory(
                        Path(self.shared.directory), self.shared.group
                    )
                    os.close(descriptor)
                atomic_history(directory / name, records)
                saved = True
            except OSError:
                self._warn_storage()
        if not saved:
            raise OSError("Conversation could not be saved to private storage")
        if (
            self.shared is not None
            and self.shared.enabled
            and self.shared.share_conversations
        ):
            try:
                publish_conversation_snapshot(self.shared, name, records)
            except (OSError, ValueError):
                self._warn_storage()

    def _conversation_file(self, user_id: str) -> Path:
        """
        Return the JSON file path for one user's conversation history.
        """
        return self.base_dir / self._filename(user_id)

    def _read_full_history(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Load the full stored history for a user.

        Returns:
            A validated list of conversation turn dictionaries.
            Invalid or corrupt data is ignored safely.
        """
        if not self.enabled:
            return []

        name = self._filename(user_id)
        with history_lock(self._lock_directory(), name):
            history, _ = self._load_sources(name)
            return history

    def _write_full_history(self, user_id: str, history: List[Dict[str, Any]]) -> None:
        """
        Persist the full conversation history for a user.
        """
        if not self.enabled:
            return
        name = self._filename(user_id)
        with history_lock(self._lock_directory(), name):
            _, directories = self._load_sources(name)
            self._persist(name, history, directories)

    def _append_records(self, user_id: str, turns: list[tuple[str, str]]) -> None:
        if not self.enabled:
            return
        name = self._filename(user_id)
        with history_lock(self._lock_directory(), name):
            history, directories = self._load_sources(name)
            for role, content in turns:
                history.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "turn_id": uuid4().hex,
                        "role": role,
                        "content": content,
                    }
                )
            self._persist(name, history, directories)

    def append_turn(self, user_id: str, role: str, content: str) -> None:
        """
        Append one conversation turn to persistent storage.
        """
        self._append_records(user_id, [(role, content)])

    def append_exchange(self, user_id: str, question: str, answer: str) -> None:
        """
        Append a user/assistant exchange as two back-to-back turns.
        """
        self._append_records(user_id, [("user", question), ("assistant", answer)])

    def load_recent(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Return the most recent stored turns based on configured history size.
        """
        history = self._read_full_history(user_id)
        return history[-self.config.conversation.max_messages :]

    def build_context(
        self,
        user_id: str,
        *,
        current_query: Optional[str] = None,
        max_prompt_length: Optional[int] = None,
    ) -> List[str]:
        """
        Build processed conversation context for prompt injection.

        High-level flow:
        - load full history
        - optionally remove a duplicate current query
        - either keep recent turns as-is or summarize older turns
        - optionally trim the final lines to fit a prompt budget

        Returns:
            A list of prompt-ready text lines.
        """
        if not self.enabled:
            return []

        history = self._read_full_history(user_id)
        if not history:
            return []

        # Prevent the current query from appearing twice in the prompt.
        # This handles the case where the caller already appended the query
        # to conversation history before asking for built context.
        if current_query:
            filtered = history.copy()

            if (
                filtered
                and filtered[-1].get("role") == "user"
                and filtered[-1].get("content") == current_query
            ):
                filtered = filtered[:-1]

            history = filtered

        if not history:
            return []

        max_messages = self.config.conversation.max_messages
        summary_trigger = self.config.conversation.summary_trigger
        summarize = self.config.conversation.summarize

        # If history is already small enough, keep it verbatim.
        if len(history) <= max_messages:
            lines = self._format_recent(history)

        # If summarization is enabled and history is large enough, summarize
        # older turns and keep the most recent turns verbatim.
        elif summarize and len(history) >= summary_trigger:
            older = history[:-max_messages]
            recent = history[-max_messages:]

            lines = []

            summary = self._summarize_turns(older)
            if summary:
                lines.append("[SUMMARY OF OLDER CONTEXT]")
                lines.extend(summary)
                lines.append("")

            lines.append("[RECENT HISTORY]")
            lines.extend(self._format_recent(recent))

        # Otherwise, just keep the most recent configured number of turns.
        else:
            lines = self._format_recent(history[-max_messages:])

        # Optionally enforce a rough prompt-size budget.
        if max_prompt_length is not None:
            lines = self._fit_lines_to_budget(lines, max_prompt_length)

        return lines

    def _format_recent(self, turns: List[Dict[str, Any]]) -> List[str]:
        """
        Convert recent conversation turns into prompt-ready lines.

        Format:
            user: ...
            assistant: ...
        """
        return [f"{turn['role']}: {turn['content']}" for turn in turns]

    def _summarize_turns(self, turns: List[Dict[str, Any]]) -> List[str]:
        """
        Build a deterministic summary of older conversation turns.

        This is intentionally lightweight and non-LLM-based.
        The goal is to keep older context visible without storing
        too much raw text in the final prompt.
        """
        bullets: List[str] = []
        max_chars = self.config.conversation.max_summary_chars

        for turn in turns:
            role = turn["role"]
            content = " ".join(turn["content"].split())

            # Clamp each summarized line so one long message does not
            # dominate the summary block.
            if len(content) > 140:
                content = content[:137] + "..."

            bullets.append(f"- {role}: {content}")

        summary_text = "\n".join(bullets)

        # Clamp total summary size to configured character budget.
        if len(summary_text) > max_chars:
            summary_text = summary_text[: max_chars - 3] + "..."

        return summary_text.splitlines()

    def _fit_lines_to_budget(
        self, lines: List[str], max_prompt_length: int
    ) -> List[str]:
        """
        Trim lines until the combined text fits within the prompt budget.

        Oldest lines are dropped first because recent turns usually matter more.
        """
        kept = list(lines)

        while kept and len("\n".join(kept)) > max_prompt_length:
            kept.pop(0)

        return kept
