import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import install
import pytest


def test_install_ink_uses_current_python_interpreter(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(
        install,
        "STATE_CFG",
        SimpleNamespace(bin_dir=bin_dir),
    )

    install.install_ink()

    installed_ink = bin_dir / "ink"

    assert installed_ink.exists()

    first_line = installed_ink.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == f"#!{sys.executable}"

    if os.name == "posix":
        assert installed_ink.stat().st_mode & 0o111


def _shell_path_config(monkeypatch, tmp_path, *, enabled=True):
    bin_dir = tmp_path / ".inkly" / "bin"
    shell_rc = tmp_path / ".bashrc"
    monkeypatch.setattr(
        install,
        "INSTALL_CFG",
        SimpleNamespace(
            allow_path_injection=enabled,
            allow_modify_shell_rc=enabled,
            shell_rc=str(shell_rc),
        ),
    )
    monkeypatch.setattr(install, "STATE_CFG", SimpleNamespace(bin_dir=bin_dir))
    return shell_rc, f'export PATH="{bin_dir}:$PATH"'


def _active_path_lines(shell_rc, line):
    return [
        entry
        for entry in shell_rc.read_text(encoding="utf-8").splitlines()
        if entry == line
    ]


def test_shell_path_is_added_once(tmp_path, monkeypatch):
    shell_rc, line = _shell_path_config(monkeypatch, tmp_path)
    shell_rc.write_text("alias ll='ls -l'\n", encoding="utf-8")

    install.configure_shell_path()
    install.configure_shell_path()

    assert shell_rc.read_text(encoding="utf-8").startswith("alias ll='ls -l'\n")
    assert len(_active_path_lines(shell_rc, line)) == 1


def test_shell_path_is_added_when_only_a_commented_line_names_it(tmp_path, monkeypatch):
    shell_rc, line = _shell_path_config(monkeypatch, tmp_path)
    commented = f"  # {line}"
    shell_rc.write_text(f"{commented}\n", encoding="utf-8")

    install.configure_shell_path()

    text = shell_rc.read_text(encoding="utf-8")
    assert commented in text.splitlines()
    assert len(_active_path_lines(shell_rc, line)) == 1


@pytest.mark.parametrize(
    "existing",
    [
        'export PATH="{bin}:$PATH"',
        'export PATH="{bin}:$PATH"  # added by Inkly',
        "    PATH={bin}:$PATH",
    ],
)
def test_an_active_line_with_the_bin_directory_is_kept_as_it_is(
    tmp_path, monkeypatch, existing
):
    shell_rc, _ = _shell_path_config(monkeypatch, tmp_path)
    contents = existing.format(bin=tmp_path / ".inkly" / "bin") + "\n"
    shell_rc.write_text(contents, encoding="utf-8")

    install.configure_shell_path()

    assert shell_rc.read_text(encoding="utf-8") == contents


def test_shell_file_is_untouched_when_path_injection_is_off(tmp_path, monkeypatch):
    shell_rc, _ = _shell_path_config(monkeypatch, tmp_path, enabled=False)
    shell_rc.write_text("# keep\n", encoding="utf-8")

    install.configure_shell_path()

    assert shell_rc.read_text(encoding="utf-8") == "# keep\n"


def _write_fake_runtime(package_root: Path, marker: str) -> None:
    package = package_root / "inkly"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "ink_core.py").write_text(
        f"def main():\n    print({marker!r})\n    return 0\n",
        encoding="utf-8",
    )


def test_source_launcher_prefers_checkout_runtime_over_installed_copy(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    launcher = tmp_path / "checkout" / "ink"
    launcher.parent.mkdir()
    launcher.write_text(
        (repo_root / "ink").read_text(encoding="utf-8"), encoding="utf-8"
    )

    _write_fake_runtime(launcher.parent, "checkout-runtime")

    home = tmp_path / "home"
    _write_fake_runtime(home / ".inkly" / "lib", "stale-installed-runtime")

    result = subprocess.run(
        [sys.executable, str(launcher)],
        cwd=launcher.parent,
        env={"HOME": str(home), "USERPROFILE": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "checkout-runtime"


def test_installed_launcher_uses_private_runtime(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    launcher = tmp_path / "bin" / "ink"
    launcher.parent.mkdir()
    launcher.write_text(
        (repo_root / "ink").read_text(encoding="utf-8"), encoding="utf-8"
    )

    home = tmp_path / "home"
    _write_fake_runtime(home / ".inkly" / "lib", "installed-runtime")

    result = subprocess.run(
        [sys.executable, str(launcher)],
        cwd=launcher.parent,
        env={"HOME": str(home), "USERPROFILE": str(home)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "installed-runtime"


@pytest.mark.skipif(os.name != "posix", reason="Source venv launcher is for HPC Linux")
@pytest.mark.parametrize("developer_venv", [False, True])
def test_source_launcher_uses_dependencies_without_shadowing_source(
    tmp_path, developer_venv
):
    import venv

    home = tmp_path / "home"
    installed_venv = home / ".inkly" / "venv"
    venv.EnvBuilder(with_pip=False).create(installed_venv)
    selected = installed_venv
    if developer_venv:
        selected = tmp_path / "developer-venv"
        venv.EnvBuilder(with_pip=False).create(selected)
    expected_python = selected / "bin" / "python"
    launcher = tmp_path / "source" / "ink"
    launcher.parent.mkdir()
    launcher.write_text(
        (Path(__file__).resolve().parents[1] / "ink").read_text(), encoding="utf-8"
    )
    _write_fake_runtime(home / ".inkly" / "lib", "stale-installed-runtime")
    _write_fake_runtime(launcher.parent, "source-runtime")
    (launcher.parent / "inkly" / "ink_core.py").write_text(
        "import sys\ndef main():\n    print('source-runtime', sys.executable)\n    return 0\n",
        encoding="utf-8",
    )
    initial_python = expected_python if developer_venv else Path(sys._base_executable)
    result = subprocess.run(
        [str(initial_python), str(launcher)],
        env={"HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"source-runtime {expected_python}"


@pytest.mark.skipif(
    os.name != "posix", reason="Direct executable launch is HPC Linux behavior"
)
def test_bare_ink_and_dot_slash_ink_resolve_their_own_runtime(tmp_path, monkeypatch):
    from scripts.validate_fresh_install import isolated_environment

    home = tmp_path / "independent researcher"
    home.mkdir()
    bin_dir = home / ".inkly/bin"
    monkeypatch.setattr(install, "STATE_CFG", SimpleNamespace(bin_dir=bin_dir))
    bin_dir.mkdir(parents=True)
    install.install_ink()
    _write_fake_runtime(home / ".inkly/lib", "private-installed-runtime")

    checkout = tmp_path / "independent checkout"
    checkout.mkdir()
    launcher = checkout / "ink"
    launcher.write_text(
        f"#!{sys.executable}\n"
        + (Path(__file__).resolve().parents[1] / "ink").read_text().split("\n", 1)[1],
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    _write_fake_runtime(checkout, "independent-source-runtime")
    child = isolated_environment(home, Path(sys.executable))
    child["PATH"] = str(bin_dir) + os.pathsep + os.defpath
    for command, cwd, expected in (
        (["ink"], tmp_path, "private-installed-runtime"),
        (["./ink"], checkout, "independent-source-runtime"),
    ):
        result = subprocess.run(
            command,
            cwd=cwd,
            env=child,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
