import json
import multiprocessing
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

import install
from inkly.config import SharedLoggingConfig
from inkly.core.conversation import ConversationManager
from inkly.core.shared_conversations import WITHHELD, filtered_content
from inkly.core.shared_storage import prepare_shared_storage
from tests.test_observability import make_config, shared_config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def snapshots(shared):
    return {
        path.name: [json.loads(line) for line in path.read_text().splitlines()]
        for path in Path(shared.directory).glob("chat-*.jsonl")
    }


def test_automatic_full_snapshot_and_private_archive_permissions(tmp_path):
    shared = shared_config(tmp_path)
    manager = ConversationManager(make_config(tmp_path, shared))
    manager.append_exchange("alice", "cluster question", "cluster answer")
    manager.append_turn("bob", "user", "separate conversation")
    assert manager.archive_dir == Path(shared.directory) / f"history-{os.getuid()}"
    assert stat.S_IMODE(manager.archive_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((manager.archive_dir / "alice.json").stat().st_mode) == 0o600
    exported = snapshots(shared)
    assert len(exported) == 2
    assert sorted(len(records) for records in exported.values()) == [1, 2]
    for path in Path(shared.directory).glob("chat-*.jsonl"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert path.stat().st_gid == os.getgid()
    assert not list(Path(shared.directory).glob(".chat-*.tmp"))


def test_existing_history_is_preserved_and_shared_on_next_write(tmp_path):
    config = make_config(tmp_path)
    original = ConversationManager(config)
    original.append_turn("alice", "user", "existing question")
    config.logging.shared = shared_config(tmp_path)
    updated = ConversationManager(config)
    updated.append_turn("alice", "assistant", "new answer")
    records = next(iter(snapshots(config.logging.shared).values()))
    assert [record["payload"]["content"] for record in records] == [
        "existing question",
        "new answer",
    ]
    assert len(json.loads((original.recovery_dir / "alice.json").read_text())) == 2
    assert len(json.loads((updated.archive_dir / "alice.json").read_text())) == 2


def test_scratch_removal_and_return_rebuilds_complete_snapshot(tmp_path):
    shared = shared_config(tmp_path)
    config = make_config(tmp_path, shared)
    manager = ConversationManager(config)
    manager.append_turn("alice", "user", "before scratch removal")
    for path in Path(shared.directory).glob("chat-*.jsonl"):
        path.unlink()
    (manager.archive_dir / "alice.json").unlink()
    manager.archive_dir.rmdir()
    Path(shared.directory).rmdir()
    with pytest.warns(RuntimeWarning):
        manager.append_turn("alice", "assistant", "during outage")
    assert not Path(shared.directory).exists()
    assert len(manager._read_full_history("alice")) == 2
    prepare_shared_storage(shared)
    manager.append_turn("alice", "user", "after scratch returns")
    assert len(next(iter(snapshots(shared).values()))) == 3


@pytest.mark.parametrize("disabled", ["shared", "logging", "conversation"])
def test_opt_out_stops_new_scratch_writes(tmp_path, disabled):
    shared = shared_config(tmp_path)
    config = make_config(tmp_path, shared)
    getattr(config, disabled if disabled != "shared" else "logging").enabled = False
    if disabled == "shared":
        config.logging.enabled = True
        shared.enabled = False
    manager = ConversationManager(config)
    manager.append_turn("alice", "user", "opted out")
    assert not list(Path(shared.directory).iterdir())
    if disabled != "conversation":
        assert manager.load_recent("alice")[0]["content"] == "opted out"


def test_text_sharing_and_private_archiving_have_separate_switches(tmp_path):
    shared = shared_config(tmp_path)
    shared.share_conversations = False
    manager = ConversationManager(make_config(tmp_path, shared))
    manager.append_turn("alice", "user", "private archive only")
    assert not snapshots(shared)
    assert (manager.archive_dir / "alice.json").exists()
    shared.share_conversations = True
    shared.archive_conversations = False
    manager = ConversationManager(make_config(tmp_path, shared))
    manager.append_turn("alice", "assistant", "shared answer")
    assert manager.archive_dir is None
    assert len(next(iter(snapshots(shared).values()))) == 2


@pytest.mark.parametrize(
    "content",
    [
        "password=PRIVATE_SENTINEL",
        '"api_key": "PRIVATE_SENTINEL"',
        "Authorization: Bearer PRIVATE_SENTINEL",
        "TOKEN=PRIVATE_SENTINEL",
        "AWS_SECRET_ACCESS_KEY=PRIVATE_SENTINEL",
        "--password PRIVATE_SENTINEL",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE_SENTINEL",
        "ghp_abcdefghijklmnopqrstuv",
        "github_pat_abcdefghijklmnopqrstuv",
        "sk-abcdefghijklmnopqrstuv",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmn",
        "https://alice:PRIVATE_SENTINEL@example.test/path",
        "Backend error: PRIVATE_SENTINEL",
        "Plugin error: PRIVATE_SENTINEL",
    ],
)
def test_common_credentials_and_raw_errors_withhold_entire_turn(content):
    assert filtered_content(content) == (WITHHELD, True)


def test_cluster_details_personal_details_and_research_remain_in_shared_chat(tmp_path):
    shared = shared_config(tmp_path)
    config = make_config(tmp_path, shared)
    text = "My name is Alice. Contact alice@example.test about my unpublished cluster study."
    manager = ConversationManager(config)
    manager.append_turn("alice", "user", text)
    record = next(iter(snapshots(shared).values()))[0]
    assert record["payload"]["content"] == text
    assert record["payload"]["redacted"] is False


def test_withheld_original_and_arbitrary_metadata_stay_private(tmp_path):
    shared = shared_config(tmp_path)
    manager = ConversationManager(make_config(tmp_path, shared))
    manager._write_full_history(
        "alice",
        [
            {
                "role": "user",
                "content": "password=PRIVATE_CONTENT_SENTINEL",
                "timestamp": "PRIVATE_TIMESTAMP_SENTINEL",
                "turn_id": "PRIVATE_ID_SENTINEL",
                "nested": {"private": "PRIVATE_METADATA_SENTINEL"},
            }
        ],
    )
    exported = next(iter(snapshots(shared).values()))
    assert exported[0]["payload"]["redacted"] is True
    assert "PRIVATE_" not in json.dumps(exported)
    assert (
        "PRIVATE_CONTENT_SENTINEL" in (manager.recovery_dir / "alice.json").read_text()
    )


def test_snapshot_write_failure_preserves_last_complete_shared_file(
    tmp_path, monkeypatch
):
    shared = shared_config(tmp_path)
    manager = ConversationManager(make_config(tmp_path, shared))
    manager.append_turn("alice", "user", "first question")
    original = snapshots(shared)
    from inkly.core import shared_conversations

    def fail_publish(*args, **kwargs):
        raise OSError("synthetic shared full filesystem")

    monkeypatch.setattr(shared_conversations.os, "rename", fail_publish)
    with pytest.warns(RuntimeWarning):
        manager.append_turn("alice", "assistant", "saved privately during failure")
    assert snapshots(shared) == original
    assert len(manager._read_full_history("alice")) == 2
    assert not list(Path(shared.directory).glob(".chat-*.tmp"))


def test_existing_snapshot_symlink_is_never_followed_or_replaced(tmp_path):
    shared = shared_config(tmp_path)
    manager = ConversationManager(make_config(tmp_path, shared))
    manager.append_turn("alice", "user", "first question")
    path = next(Path(shared.directory).glob("chat-*.jsonl"))
    path.unlink()
    target = tmp_path / "protected"
    target.write_text("do not overwrite")
    path.symlink_to(target)
    with pytest.warns(RuntimeWarning):
        manager.append_turn("alice", "assistant", "second turn")
    assert path.is_symlink()
    assert target.read_text() == "do not overwrite"
    assert len(manager._read_full_history("alice")) == 2


def _history_writer(directory, shared):
    directory = Path(directory)
    with patch.object(Path, "home", return_value=directory / "home"):
        manager = ConversationManager(make_config(directory, shared))
        for index in range(8):
            manager.append_exchange(
                "alice", f"synthetic question {index}", "synthetic answer"
            )


def test_multiple_processes_publish_complete_snapshots_without_lost_turns(tmp_path):
    shared = shared_config(tmp_path)
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_history_writer, args=(str(tmp_path), shared))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    records = next(iter(snapshots(shared).values()))
    assert len(records) == 48
    assert len({record["session"]["session_id"] for record in records}) == 1
    assert [record["payload"]["turn_index"] for record in records] == list(range(48))
    assert (
        len(
            ConversationManager(make_config(tmp_path, shared))._read_full_history(
                "alice"
            )
        )
        == 48
    )


def test_install_disclaimer_has_destination_access_secret_warning_and_opt_out(capsys):
    settings = SharedLoggingConfig(enabled=True, group="fixture-lab")
    install.print_shared_logging_notice(settings)
    notice = capsys.readouterr().out
    for expected in (
        "/scratch/inklys-logs",
        "fixture-lab",
        "existing and future chats",
        "unpublished",
        "passwords",
        "private keys",
        "not a guarantee",
        "enabled = false",
        "~/.inkly/config.toml",
        "private history",
        "administrators",
    ):
        assert expected in notice
