import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import install
from inkly.config import SharedLoggingConfig
from inkly.core.diagnostics import EventSink
from inkly.core.shared_storage import PURPOSE, prepare_shared_storage


@pytest.fixture
def settings(tmp_path):
    if os.name != "posix":
        pytest.skip("Shared storage requires POSIX permissions")
    import grp

    return SharedLoggingConfig(
        enabled=True,
        directory=str(tmp_path / "inklys-logs"),
        group=grp.getgrgid(os.getgid()).gr_name,
    )


def test_installer_creates_group_only_root_and_purpose_readme(settings):
    directory = prepare_shared_storage(settings)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o3770
    assert directory.stat().st_gid == os.getgid()
    readme = directory / "README.md"
    assert stat.S_IMODE(readme.stat().st_mode) == 0o640
    assert readme.stat().st_gid == os.getgid()
    assert readme.read_text(encoding="utf-8") == PURPOSE


def test_installer_reuses_root_without_overwriting_readme(settings):
    directory = prepare_shared_storage(settings)
    readme = directory / "README.md"
    readme.write_text("existing lab instructions", encoding="utf-8")
    prepare_shared_storage(settings)
    assert readme.read_text(encoding="utf-8") == "existing lab instructions"


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o2770, 0o3777])
def test_installer_never_repairs_existing_root_permissions(settings, mode):
    directory = Path(settings.directory)
    directory.mkdir()
    os.chown(directory, -1, os.getgid())
    directory.chmod(mode)
    with pytest.raises(PermissionError):
        prepare_shared_storage(settings)
    assert stat.S_IMODE(directory.stat().st_mode) == mode
    assert not list(directory.iterdir())


def test_installer_rejects_existing_root_symlink(settings, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    Path(settings.directory).symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        prepare_shared_storage(settings)
    assert not list(target.iterdir())


def test_missing_group_membership_does_not_create_shared_root(settings, monkeypatch):
    import grp

    monkeypatch.setattr(grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=987654321))
    with pytest.raises(PermissionError):
        prepare_shared_storage(settings)
    assert not Path(settings.directory).exists()


def test_missing_scratch_does_not_create_parent_directories(settings, tmp_path):
    settings.directory = str(tmp_path / "missing-scratch" / "inklys-logs")
    with pytest.raises(FileNotFoundError):
        prepare_shared_storage(settings)
    assert not (tmp_path / "missing-scratch").exists()


def test_installer_storage_failure_is_nonfatal_and_does_not_print_exception(
    monkeypatch, capsys
):
    def unavailable(shared):
        raise PermissionError("PRIVATE_EXCEPTION_SENTINEL")

    monkeypatch.setattr(install, "prepare_shared_storage", unavailable)
    assert not install.setup_shared_storage(SharedLoggingConfig(group="lab"))
    output = capsys.readouterr()
    assert "PRIVATE_EXCEPTION_SENTINEL" not in output.err
    assert "Private history remains available" in output.err


def test_installer_without_group_keeps_private_defaults(capsys):
    assert not install.setup_shared_storage(SharedLoggingConfig())
    assert "lab-only Unix group" in capsys.readouterr().err


def test_disabled_shared_config_error_is_nonfatal_to_private_install(capsys):
    assert not install.setup_shared_storage(
        SharedLoggingConfig(group="fixture-group", directory="relative")
    )
    assert "Private history remains available" in capsys.readouterr().err


def test_global_logging_opt_out_skips_shared_setup_and_reports_disabled(
    monkeypatch, capsys
):
    def unexpected_setup(shared):
        raise AssertionError("Shared setup should not run")

    monkeypatch.setattr(install, "prepare_shared_storage", unexpected_setup)
    assert not install.setup_shared_storage(
        SharedLoggingConfig(enabled=True, group="fixture-group"), logging_enabled=False
    )
    assert "currently DISABLED" in capsys.readouterr().out


def test_disabled_sharing_can_prepare_root_without_enabling_collection(settings):
    settings.enabled = False
    assert install.setup_shared_storage(settings)
    assert not settings.enabled
    assert (Path(settings.directory) / "README.md").exists()


def test_admin_managed_retention_never_prunes_shared_files(settings):
    directory = prepare_shared_storage(settings)
    settings.validate()
    for index in range(8):
        sink = EventSink(directory, f"{index:032x}", 100, 1, settings)
        sink.append(b'{"synthetic":true}\n')
        sink.close()
    assert len(list(directory.glob("*.jsonl"))) == 8
    assert (directory / "README.md").read_text(encoding="utf-8") == PURPOSE


def test_explicit_age_retention_does_not_apply_private_file_count(settings):
    directory = prepare_shared_storage(settings)
    settings.retention_days = 7
    for index in range(4):
        sink = EventSink(directory, f"{index:032x}", 100, 1, settings)
        sink.append(b'{"synthetic":true}\n')
        sink.close()
    old_file = sorted(directory.glob("*.jsonl"))[0]
    old_time = time.time() - 8 * 86400
    os.utime(old_file, (old_time, old_time))
    sink = EventSink(directory, "f" * 32, 100, 1, settings)
    sink.append(b'{"synthetic":true}\n')
    sink.close()
    assert not old_file.exists()
    assert len(list(directory.glob("*.jsonl"))) == 4
