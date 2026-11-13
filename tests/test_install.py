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
