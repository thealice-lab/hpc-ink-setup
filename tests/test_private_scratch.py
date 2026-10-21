import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from inkly.config import ConfigError, LoggingConfig
from inkly.core.conversation import ConversationManager
from inkly.core.diagnostics import DiagnosticSession
from tests.test_observability import events, make_config
from tools.configure_private_scratch import configure_private_scratch


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_private_scratch_duplicates_home_diagnostics_and_keeps_text_out(tmp_path):
    config = make_config(tmp_path)
    config.logging.private_scratch_dir = str(tmp_path / "scratch/logs")
    with DiagnosticSession(config) as session:
        session.emit("user_prompt", {"chars": 7, "content": "PRIVATE_SENTINEL"})
    assert events(config.state.log_dir) == events(config.logging.private_scratch_dir)
    assert "PRIVATE_SENTINEL" not in json.dumps(
        events(config.logging.private_scratch_dir)
    )
    if os.name == "posix":
        directory = Path(config.logging.private_scratch_dir)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir()
        )


def test_unavailable_private_scratch_does_not_stop_home_logs_or_history(tmp_path):
    config = make_config(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("original")
    config.logging.private_scratch_dir = str(blocker / "logs")
    with pytest.warns(RuntimeWarning):
        with DiagnosticSession(config):
            ConversationManager(config).append_turn("alice", "user", "private fixture")
    assert len(events(config.state.log_dir)) == 2
    assert (
        ConversationManager(config).load_recent("alice")[0]["content"]
        == "private fixture"
    )
    assert blocker.read_text() == "original"


def test_private_scratch_same_as_home_log_path_is_not_duplicated(tmp_path):
    config = make_config(tmp_path)
    config.logging.private_scratch_dir = str(config.state.log_dir)
    with DiagnosticSession(config) as session:
        assert len(session.sinks) == 1
    assert len(events(config.state.log_dir)) == 2


@pytest.mark.parametrize("value", [True, 1, "relative/logs"])
def test_private_scratch_rejects_invalid_paths(value):
    with pytest.raises(ConfigError):
        LoggingConfig(private_scratch_dir=value).validate()


def make_installed_config(home):
    directory = home / ".inkly"
    directory.mkdir()
    config_path = directory / "config.toml"
    config_path.write_text(
        "[install]\nallow_modify_shell_rc = false\n"
        f'[state]\ninkly_home = "{directory.as_posix()}"\n'
        f'bin_dir = "{directory.as_posix()}/bin"\n'
        '[logging]\nenabled = true\n[unrelated]\nsetting = "keep this setting"\n'
    )
    return config_path


def test_owner_setup_preserves_config_and_private_history(private_home, tmp_path):
    path = make_installed_config(private_home)
    original = path.read_bytes()
    directory = tmp_path / "scratch/inklys-logs"
    directory.parent.mkdir()
    config, backup = configure_private_scratch(path, directory)
    assert backup.read_bytes() == original
    assert config.raw_config["unrelated"]["setting"] == "keep this setting"
    assert not config.logging.shared.enabled
    assert config.logging.private_scratch_dir == str(directory / "logs")
    assert config.state.log_dir == private_home / ".inkly/logs"
    assert config.state.conversation_dir == directory / "conversations"
    manager = ConversationManager(config)
    manager.append_exchange("alice", "private question", "private answer")
    assert (manager.recovery_dir / "alice.json").read_bytes() == (
        directory / "conversations/alice.json"
    ).read_bytes()
    assert "owner-only" in (directory / "README.md").read_text()
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_owner_setup_refuses_existing_shared_root_without_chmod(private_home, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permissions")
    path = make_installed_config(private_home)
    original = path.read_bytes()
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o770)
    with pytest.raises(PermissionError):
        configure_private_scratch(path, directory)
    assert path.read_bytes() == original
    assert stat.S_IMODE(directory.stat().st_mode) == 0o770
    assert not list(directory.iterdir())


def test_owner_setup_refuses_custom_history_without_losing_originals(
    private_home, tmp_path
):
    path = make_installed_config(private_home)
    text = path.read_text().replace(
        "[logging]", f'conversation_dir = "{tmp_path.as_posix()}/custom"\n[logging]'
    )
    path.write_text(text)
    with pytest.raises(ValueError, match="Migrate customized history"):
        configure_private_scratch(path, tmp_path / "scratch")
    assert path.read_text() == text


def test_owner_setup_refuses_symlink_root(private_home, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX symlink")
    path = make_installed_config(private_home)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "scratch-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError):
        configure_private_scratch(path, link)
    assert not list(target.iterdir())


def test_owner_setup_backs_up_runtime_and_launcher_before_installing(
    private_home, tmp_path, monkeypatch
):
    import install

    path = make_installed_config(private_home)
    runtime = private_home / ".inkly/lib/inkly"
    runtime.mkdir(parents=True)
    (runtime / "old.py").write_text("old runtime")
    launcher = private_home / ".inkly/bin/ink"
    launcher.parent.mkdir()
    launcher.write_text("old launcher")
    calls = []
    monkeypatch.setattr(install, "STATE_CFG", None)
    monkeypatch.setattr(install, "setup_runtime_dirs", lambda: calls.append("runtime"))
    monkeypatch.setattr(install, "install_ink", lambda: calls.append("launcher"))
    _, backup = configure_private_scratch(
        path, tmp_path / "scratch", install_runtime=True
    )
    assert calls == ["runtime", "launcher"]
    assert next(backup.parent.glob("launcher-*")).read_text() == "old launcher"
    with tarfile.open(next(backup.parent.glob("runtime-*.tar.gz")), "r:gz") as archive:
        assert archive.extractfile("inkly/old.py").read() == b"old runtime"


def test_owner_setup_does_not_install_through_runtime_symlink(private_home, tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX symlink")
    path = make_installed_config(private_home)
    original = path.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.py"
    marker.write_text("do not overwrite")
    runtime = private_home / ".inkly/lib/inkly"
    runtime.parent.mkdir()
    runtime.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlinks require manual review"):
        configure_private_scratch(path, tmp_path / "scratch", install_runtime=True)
    assert path.read_bytes() == original
    assert marker.read_text() == "do not overwrite"
