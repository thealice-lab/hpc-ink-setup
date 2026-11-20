import os
from pathlib import Path
import subprocess
import sys

import pytest

from inkly.config import TomlParser
from scripts.validate_fresh_install import isolated_config, isolated_environment


ROOT = Path(__file__).resolve().parents[1]


def test_isolated_environment_excludes_personal_overrides(tmp_path, monkeypatch):
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "SCRAPER_DIR",
        "SSH_AUTH_SOCK",
        "INKLY_CLUSTER_PROFILE",
        "INKLY_GAUSSIAN_SHARED_ROOT",
        "PIP_INDEX_URL",
    ):
        monkeypatch.setenv(key, "private-value")
    child = isolated_environment(tmp_path, Path(sys.executable))
    assert child["HOME"] == str(tmp_path)
    assert child["PATH"] == os.defpath
    assert child["PYTHONNOUSERSITE"] == "1"
    assert "private-value" not in child.values()
    assert child["PIP_CONFIG_FILE"] == os.devnull
    assert child["GIT_TERMINAL_PROMPT"] == "0"


def test_isolated_template_disables_all_external_writes(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        isolated_config((ROOT / "config.toml").read_text()), encoding="utf-8"
    )
    cfg = TomlParser(config).load()
    assert not cfg.logging.enabled  # installer otherwise still prepares shared root
    assert not cfg.logging.shared.enabled
    assert not cfg.logging.shared.archive_conversations
    assert not cfg.logging.shared.share_conversations
    assert not cfg.install.allow_modify_shell_rc
    assert not cfg.install.allow_path_injection
    assert cfg.ollama.command_path == "/opt/ollama/ollama"
    assert cfg.llm.model == "llama3-cuttlefish:latest"


@pytest.mark.parametrize(
    "template",
    ["", "[logging]\nenabled = true\n", "[install]\nallow_modify_shell_rc = false\n"],
)
def test_unknown_isolation_template_fails_closed(template):
    with pytest.raises(ValueError, match="Cannot safely isolate"):
        isolated_config(template)


@pytest.mark.skipif(os.name != "posix", reason="Linux installation integration")
def test_real_installer_repeats_without_replacing_state_or_touching_shell(tmp_path):
    state = tmp_path / ".inkly"
    state.mkdir(mode=0o700)
    config = state / "config.toml"
    contents = isolated_config((ROOT / "config.toml").read_text())
    config.write_text(contents, encoding="utf-8")
    shell = tmp_path / ".bashrc"
    shell.write_text("# preserve shell\n", encoding="utf-8")
    child = isolated_environment(tmp_path, Path(sys.executable))
    for attempt in (1, 2):
        result = subprocess.run(
            [sys.executable, str(ROOT / "install.py")],
            cwd=tmp_path,
            env=child,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert config.read_text() == contents
        assert shell.read_text() == "# preserve shell\n"
        assert (state / "jobs.db").is_file()
        assert not (state / "gaussian.db").exists()
        if attempt == 1:
            (state / "conversations" / "preserve.txt").write_text(
                "synthetic existing history", encoding="utf-8"
            )
    assert (
        state / "conversations" / "preserve.txt"
    ).read_text() == "synthetic existing history"
    usage = subprocess.run(
        [str(state / "bin" / "ink")],
        cwd=tmp_path,
        env=child,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert usage.returncode == 1
    assert usage.stderr.strip() == "Usage: ink <prompt>"


def test_only_explicit_corpus_inputs_survive_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("INKLY_GAUSSIAN_SHARED_ROOT", "unreviewed-inherited-root")
    monkeypatch.setenv("INKLY_GAUSSIAN_EXCEPTIONS", "unreviewed-inherited-policy")
    root, policy = tmp_path / "approved root", tmp_path / "reviewed.json"
    child = isolated_environment(
        tmp_path / "fresh home",
        Path(sys.executable),
        shared_root=root,
        exceptions=policy,
    )
    assert child["INKLY_GAUSSIAN_SHARED_ROOT"] == str(root.resolve())
    assert child["INKLY_GAUSSIAN_EXCEPTIONS"] == str(policy.resolve())
    assert not any("unreviewed-inherited" in value for value in child.values())
    assert "SCRAPER_DIR" not in child
    assert "SSH_AUTH_SOCK" not in child
    assert "--allow-build" not in child.values()


def test_reviewed_omissions_cannot_be_requested_without_snapshot_root(tmp_path):
    with pytest.raises(ValueError, match="explicit shared root"):
        isolated_environment(
            tmp_path, Path(sys.executable), exceptions=tmp_path / "policy"
        )
