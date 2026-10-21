from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from inkly.config import (
    ConfigError,
    ConversationConfig,
    CoreConfig,
    LLMConfig,
    LoggingConfig,
    OllamaServiceConfig,
    RetrievalConfig,
    SharedLoggingConfig,
    TomlParser,
)
from inkly.core.conversation import ConversationManager
from inkly.core.diagnostics import DiagnosticSession, EventSink
from inkly.core.runtime import InklyRuntime


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def make_config(tmp_path, shared=None, enabled=True):
    return SimpleNamespace(
        state=SimpleNamespace(
            conversation_dir=tmp_path / "private-history",
            log_dir=tmp_path / "private-logs",
        ),
        logging=LoggingConfig(
            enabled=enabled,
            max_log_files=1000,
            shared=shared or SharedLoggingConfig(),
        ),
        conversation=ConversationConfig(),
        core=CoreConfig(),
        retrieval=RetrievalConfig(enabled=False),
        llm=LLMConfig(),
        ollama=OllamaServiceConfig(),
    )


def shared_config(tmp_path):
    if os.name != "posix":
        pytest.skip("Shared storage requires POSIX group permissions")
    import grp

    directory = tmp_path / "shared"
    directory.mkdir(exist_ok=True)
    os.chown(directory, -1, os.getgid())
    directory.chmod(0o3770)
    return SharedLoggingConfig(
        enabled=True,
        directory=str(directory),
        group=grp.getgrgid(os.getgid()).gr_name,
        retention_days=7,
        approval_reference="synthetic-test-fixture",
    )


def events(directory):
    return [
        json.loads(line)
        for path in Path(directory).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_disabled_diagnostics_create_no_files(tmp_path):
    config = make_config(tmp_path, enabled=False)
    with DiagnosticSession(config) as session:
        session.emit("user_prompt", {"chars": 20})
    assert not config.state.log_dir.exists()


def test_schema_and_strict_payload_allowlist(tmp_path):
    config = make_config(tmp_path)
    secret = "PRIVATE_CREDENTIAL_SENTINEL"
    with DiagnosticSession(config) as session:
        session.emit(
            "error",
            {
                "component": "backend",
                "code": "backend_failed",
                "password": secret,
                "token": secret,
                "exception": secret,
                "nested": {"credential": secret},
            },
        )
        session.emit("user_prompt", {"chars": 12, "text": secret, "user_id": secret})
        session.emit("ai_response", {"chars": False, "content": secret})
        session.emit(secret, {"content": secret})
    records = events(config.state.log_dir)
    assert len(records) == 5
    assert secret not in json.dumps(records)
    for record in records:
        assert set(record) == {
            "schema_version",
            "event_type",
            "timestamp",
            "session",
            "payload",
        }
        assert record["schema_version"] == 1
        assert record["session"]["user_id"] is None
        assert record["session"]["host"] == "redacted"
        assert record["timestamp"].endswith("+00:00")
    assert records[1]["payload"] == {"component": "backend", "code": "backend_failed"}
    assert records[2]["payload"] == {"chars": 12}
    assert records[3]["payload"] == {}


def test_prompt_and_response_switches(tmp_path):
    config = make_config(tmp_path)
    config.logging.log_user_prompts = False
    config.logging.log_ai_responses = False
    with DiagnosticSession(config) as session:
        session.emit("user_prompt", {"chars": 3})
        session.emit("ai_response", {"chars": 9})
    assert [event["event_type"] for event in events(config.state.log_dir)] == [
        "session_start",
        "session_end",
    ]


def test_runtime_keeps_text_private_on_backend_error(tmp_path):
    config = make_config(tmp_path)
    runtime = InklyRuntime(config)
    runtime.plugins = SimpleNamespace(discover=lambda: {})
    secret = "PRIVATE_BACKEND_SECRET"

    def fail_backend(prompt):
        raise RuntimeError(secret)

    runtime.backend = SimpleNamespace(generate=fail_backend)
    with pytest.raises(RuntimeError, match=secret):
        runtime.handle_query("private-user", "password=" + secret)
    records = events(config.state.log_dir)
    assert secret not in json.dumps(records)
    assert "private-user" not in json.dumps(records)
    assert any(record["payload"].get("code") == "backend_failed" for record in records)
    assert records[-1]["payload"]["status"] == "error"
    assert secret in json.dumps(runtime.conversation._read_full_history("private-user"))


def test_private_permissions(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permissions")
    config = make_config(tmp_path)
    conversation = ConversationManager(config)
    conversation.append_turn("alice", "user", "private fixture")
    with DiagnosticSession(config):
        pass
    for directory in (
        conversation.base_dir,
        conversation.recovery_dir,
        config.state.log_dir,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for path in directory.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_shared_files_are_group_readable_not_group_writable(tmp_path):
    shared = shared_config(tmp_path)
    config = make_config(tmp_path, shared)
    with DiagnosticSession(config) as session:
        session.emit("user_prompt", {"chars": 10, "content": "PRIVATE_SENTINEL"})
    assert len(events(shared.directory)) == 3
    for path in Path(shared.directory).iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert path.stat().st_gid == os.getgid()
        assert "PRIVATE_SENTINEL" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", [0o755, 0o2770, 0o3777])
def test_unsafe_shared_permissions_fall_back_without_chmod(tmp_path, mode):
    shared = shared_config(tmp_path)
    directory = Path(shared.directory)
    directory.chmod(mode)
    config = make_config(tmp_path, shared)
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(config):
            pass
    assert stat.S_IMODE(directory.stat().st_mode) == mode
    assert not list(directory.iterdir())
    assert len(events(config.state.log_dir)) == 2


def test_shared_group_membership_is_required(tmp_path, monkeypatch):
    shared = shared_config(tmp_path)
    import grp

    monkeypatch.setattr(
        grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid() + 100000)
    )
    config = make_config(tmp_path, shared)
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(config):
            pass
    assert not list(Path(shared.directory).iterdir())
    assert len(events(config.state.log_dir)) == 2


def test_shared_symlink_is_rejected(tmp_path):
    shared = shared_config(tmp_path)
    target = Path(shared.directory)
    link = tmp_path / "shared-link"
    link.symlink_to(target, target_is_directory=True)
    shared.directory = str(link)
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(make_config(tmp_path, shared)):
            pass
    assert not list(target.iterdir())


def test_unavailable_shared_storage_does_not_lose_conversations(tmp_path):
    shared = shared_config(tmp_path)
    Path(shared.directory).rmdir()
    config = make_config(tmp_path, shared)
    manager = ConversationManager(config)
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(config):
            manager.append_exchange("alice", "private question", "private answer")
    assert [item["content"] for item in manager.load_recent("alice")] == [
        "private question",
        "private answer",
    ]
    assert not Path(shared.directory).exists()


def test_legacy_history_is_preserved_and_copied_without_duplicate_turns(
    tmp_path, private_home
):
    legacy = private_home / ".inkly" / "conversations"
    legacy.mkdir(parents=True)
    old_turn = {"role": "user", "content": "legacy fixture"}
    (legacy / "alice.json").write_text(
        json.dumps([old_turn, old_turn]), encoding="utf-8"
    )
    manager = ConversationManager(make_config(tmp_path))
    manager.append_turn("alice", "assistant", "new fixture")
    assert [item["content"] for item in manager._read_full_history("alice")] == [
        "legacy fixture",
        "legacy fixture",
        "new fixture",
    ]
    assert json.loads((legacy / "alice.json").read_text()) == json.loads(
        manager._conversation_file("alice").read_text()
    )


def test_history_outage_and_recovery_preserve_both_sides(tmp_path):
    config = make_config(tmp_path)
    manager = ConversationManager(config)
    manager.append_turn("alice", "user", "before outage")
    from inkly.core import conversation

    real_write = conversation.atomic_history

    def fail_configured_write(path, history):
        if path.parent == config.state.conversation_dir:
            raise OSError("synthetic unavailable storage")
        return real_write(path, history)

    with patch.object(conversation, "atomic_history", fail_configured_write):
        with pytest.warns(RuntimeWarning):
            manager.append_turn("alice", "assistant", "during outage")
    manager.append_turn("alice", "user", "after recovery")
    expected = ["before outage", "during outage", "after recovery"]
    assert [item["content"] for item in manager._read_full_history("alice")] == expected
    assert [
        item["content"]
        for item in json.loads(manager._conversation_file("alice").read_text())
    ] == expected


def test_corrupt_original_is_not_overwritten(tmp_path, private_home):
    manager = ConversationManager(make_config(tmp_path))
    manager.recovery_dir.mkdir(parents=True)
    original = manager.recovery_dir / "alice.json"
    original.write_text("{corrupt original", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        manager.append_turn("alice", "user", "recoverable fixture")
    assert original.read_text() == "{corrupt original"
    assert manager.load_recent("alice")[-1]["content"] == "recoverable fixture"


def test_history_disabled_creates_no_directories(tmp_path):
    config = make_config(tmp_path)
    config.conversation.enabled = False
    manager = ConversationManager(config)
    manager.append_exchange("alice", "question", "answer")
    assert manager.load_recent("alice") == []
    assert not manager.base_dir.exists()
    assert not manager.recovery_dir.exists()


@pytest.mark.parametrize(
    "user_id", ["../../victim", "/absolute/path", "alice/bob", "..", "\\victim"]
)
def test_history_user_ids_cannot_escape_the_directory(tmp_path, user_id):
    manager = ConversationManager(make_config(tmp_path))
    manager.append_turn(user_id, "user", "fixture")
    path = manager._conversation_file(user_id)
    assert path.parent == manager.base_dir
    assert manager.load_recent(user_id)[0]["content"] == "fixture"


def test_separate_users_keep_separate_histories(tmp_path):
    manager = ConversationManager(make_config(tmp_path))
    manager.append_exchange("alice", "alice question", "alice answer")
    manager.append_exchange("bob", "bob question", "bob answer")
    assert all("alice" in turn["content"] for turn in manager.load_recent("alice"))
    assert all("bob" in turn["content"] for turn in manager.load_recent("bob"))


def test_hardlinked_history_does_not_expose_or_modify_target(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX hardlink protection")
    config = make_config(tmp_path)
    config.state.conversation_dir.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text('[{"role":"user","content":"UNRELATED_PRIVATE_SENTINEL"}]')
    os.link(victim, config.state.conversation_dir / "alice.json")
    original = victim.read_bytes()
    with pytest.warns(RuntimeWarning):
        manager = ConversationManager(config)
        manager.append_turn("alice", "user", "safe fixture")
    assert victim.read_bytes() == original
    assert "UNRELATED_PRIVATE_SENTINEL" not in json.dumps(manager.load_recent("alice"))


def _history_worker(root, home, worker):
    with patch.object(Path, "home", return_value=Path(home)):
        manager = ConversationManager(make_config(Path(root)))
        for index in range(6):
            manager.append_exchange(
                "alice", f"question-{worker}-{index}", f"answer-{worker}-{index}"
            )


def _logging_worker(root, home, worker, shared=None):
    with patch.object(Path, "home", return_value=Path(home)):
        config = make_config(Path(root), shared)
        with DiagnosticSession(config) as session:
            for index in range(10):
                session.emit("user_prompt", {"chars": worker * 10 + index})


@pytest.mark.parametrize("worker_function", [_history_worker, _logging_worker])
def test_independent_processes_do_not_lose_or_corrupt_records(
    tmp_path, private_home, worker_function
):
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(
            target=worker_function, args=(str(tmp_path), str(private_home), index)
        )
        for index in range(4)
    ]
    for process in workers:
        process.start()
    for process in workers:
        process.join(60)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("Storage worker timed out")
        assert process.exitcode == 0
    config = make_config(tmp_path)
    if worker_function is _history_worker:
        history = ConversationManager(config)._read_full_history("alice")
        assert len(history) == 48
        assert len({item["turn_id"] for item in history}) == 48
        for index in range(0, len(history), 2):
            assert history[index]["role"] == "user"
            assert history[index + 1]["role"] == "assistant"
    else:
        records = events(config.state.log_dir)
        assert len(records) == 48
        assert len({record["session"]["session_id"] for record in records}) == 4


def test_one_session_serializes_concurrent_threads(tmp_path):
    config = make_config(tmp_path)
    with DiagnosticSession(config) as session:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda index: session.emit("user_prompt", {"chars": index}),
                    range(60),
                )
            )
    assert len(events(config.state.log_dir)) == 62


def test_rotation_and_retention_preserve_jsonl_boundaries(tmp_path):
    config = make_config(tmp_path)
    with DiagnosticSession(config) as session:
        sink = session.sinks[0]
        sink.max_bytes = 600
        sink.max_files = 3
        for index in range(20):
            session.emit("user_prompt", {"chars": index})
    paths = list(config.state.log_dir.glob("*.jsonl"))
    assert len(paths) == 3
    assert all(path.stat().st_size <= 600 for path in paths)
    assert events(config.state.log_dir)


def test_partial_write_failure_keeps_previous_complete_lines(tmp_path, monkeypatch):
    sink = EventSink(tmp_path / "logs", "a" * 32, 4096, 5)
    sink.append(b'{"complete":true}\n')
    real_write = os.write
    calls = 0

    def failing_write(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, data[:3])
        raise OSError("synthetic full filesystem")

    with monkeypatch.context() as temporary_patch:
        temporary_patch.setattr(os, "write", failing_write)
        with pytest.raises(OSError):
            sink.append(b'{"partial":false}\n')
    sink.close()
    assert events(tmp_path / "logs") == [{"complete": True}]


@pytest.mark.parametrize(
    "field,value",
    [
        ("directory", ""),
        ("directory", "relative"),
        ("group", ""),
        ("retention_days", -1),
        ("retention_days", True),
    ],
)
def test_sharing_requires_explicit_policy_configuration(tmp_path, field, value):
    shared = SharedLoggingConfig(
        True, str(tmp_path / "shared"), "fixture-group", 7, "fixture-policy"
    )
    setattr(shared, field, value)
    with pytest.raises(ConfigError):
        shared.validate()


def test_raw_diagnostics_and_legacy_global_sink_are_rejected():
    for overrides in (
        {"log_raw_prompts": True},
        {"log_raw_ai_responses": True},
        {"global_log": True},
    ):
        with pytest.raises(ConfigError):
            LoggingConfig(**overrides).validate()


def test_global_logging_opt_out_does_not_require_usable_shared_configuration():
    config = LoggingConfig(enabled=False, shared=SharedLoggingConfig(enabled=True))
    assert config.validate() is config


def test_toml_parser_resolves_configured_storage_and_shared_settings(tmp_path):
    home = tmp_path / "state"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[install]\nallow_modify_shell_rc = false\n"
        f'[state]\ninkly_home = "{home.as_posix()}"\nbin_dir = "{home.as_posix()}/bin"\n'
        f'log_dir = "{tmp_path.as_posix()}/custom-logs"\nconversation_dir = "{tmp_path.as_posix()}/custom-history"\n'
        "[logging.shared]\nenabled = true\n"
        f'directory = "{tmp_path.as_posix()}/shared"\ngroup = "fixture-group"\n'
        'retention_days = 7\napproval_reference = "fixture-policy"\n',
        encoding="utf-8",
    )
    config = TomlParser(config_path).load()
    assert config.state.conversation_dir == tmp_path / "custom-history"
    assert config.state.log_dir == tmp_path / "custom-logs"
    assert config.logging.shared.enabled
    assert config.logging.shared.retention_days == 7
    config_path.write_text(
        config_path.read_text().replace('/shared"', '/custom-history/shared"')
    )
    with pytest.raises(ConfigError, match="separate"):
        TomlParser(config_path).load()


def test_private_setgid_history_is_hardened_without_losing_content(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX setgid inheritance")
    config = make_config(tmp_path)
    config.state.conversation_dir.mkdir(mode=0o700)
    config.state.conversation_dir.chmod(0o2700)
    manager = ConversationManager(config)
    manager.append_turn("alice", "user", "inherited-group fixture")
    assert stat.S_IMODE(manager.base_dir.stat().st_mode) == 0o700
    assert manager.load_recent("alice")[0]["content"] == "inherited-group fixture"


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_shared_writable_directory_is_not_converted_to_private(tmp_path, mode):
    if os.name != "posix":
        pytest.skip("POSIX directory permissions")
    config = make_config(tmp_path)
    config.state.conversation_dir.mkdir()
    config.state.conversation_dir.chmod(mode)
    with pytest.warns(RuntimeWarning):
        manager = ConversationManager(config)
        manager.append_turn("alice", "user", "recovery fixture")
    assert stat.S_IMODE(manager.base_dir.stat().st_mode) == mode
    assert not list(manager.base_dir.iterdir())
    assert manager.load_recent("alice")[0]["content"] == "recovery fixture"


def test_warning_as_error_does_not_disable_fallback(tmp_path):
    shared = shared_config(tmp_path)
    Path(shared.directory).rmdir()
    config = make_config(tmp_path, shared)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with DiagnosticSession(config):
            ConversationManager(config).append_turn("alice", "user", "fixture")
    assert len(events(config.state.log_dir)) == 2


def test_actual_group_membership_is_checked_even_for_directory_owner(
    tmp_path, monkeypatch
):
    shared = shared_config(tmp_path)
    real_group = os.getgid()
    monkeypatch.setattr(os, "getgroups", lambda: [])
    monkeypatch.setattr(os, "getgid", lambda: real_group + 100000)
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(make_config(tmp_path, shared)):
            pass
    assert not list(Path(shared.directory).iterdir())


def test_extended_shared_acl_is_rejected_without_modifying_it(tmp_path):
    shared = shared_config(tmp_path)
    acl_tool = shutil.which("setfacl")
    if acl_tool is None:
        pytest.skip("setfacl is unavailable")
    result = subprocess.run(
        [acl_tool, "-m", "u:65534:r-x", shared.directory], capture_output=True
    )
    if result.returncode:
        pytest.skip("The test filesystem does not support POSIX ACLs")
    original = os.getxattr(shared.directory, "system.posix_acl_access")
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(make_config(tmp_path, shared)):
            pass
    assert os.getxattr(shared.directory, "system.posix_acl_access") == original
    assert not list(Path(shared.directory).iterdir())


def test_multiple_shared_writers_keep_independent_valid_streams(tmp_path, private_home):
    shared = shared_config(tmp_path)
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(
            target=_logging_worker,
            args=(str(tmp_path), str(private_home), index, shared),
        )
        for index in range(3)
    ]
    for process in workers:
        process.start()
    for process in workers:
        process.join(60)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("Shared writer timed out")
        assert process.exitcode == 0
    records = events(shared.directory)
    assert len(records) == 36
    assert len({record["session"]["session_id"] for record in records}) == 3
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o640
        for path in Path(shared.directory).iterdir()
    )


def test_atomic_replace_failure_preserves_preexisting_history(tmp_path, monkeypatch):
    from inkly.core import storage

    manager = ConversationManager(make_config(tmp_path))
    manager.append_turn("alice", "user", "original fixture")
    paths = [directory / "alice.json" for directory in manager._directories()]
    originals = [path.read_bytes() for path in paths]

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with (
        pytest.warns(RuntimeWarning),
        pytest.raises(OSError, match="could not be saved"),
    ):
        manager.append_turn("alice", "assistant", "unsaved fixture")
    assert [path.read_bytes() for path in paths] == originals
    assert not list(manager.base_dir.glob(".history-*"))


def test_unreviewed_nfs4_acl_is_rejected(tmp_path, monkeypatch):
    shared = shared_config(tmp_path)
    monkeypatch.setattr(os, "listxattr", lambda descriptor: ["system.nfs4_acl"])
    monkeypatch.setattr(os, "getxattr", lambda descriptor, name: b"unreviewed-acl")
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(make_config(tmp_path, shared)):
            pass
    assert not list(Path(shared.directory).iterdir())
