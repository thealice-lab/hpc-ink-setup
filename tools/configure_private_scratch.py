from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inkly.config import ConfigError, TomlParser, tomllib
from inkly.core.permissions import validate_private_acl
from inkly.core.storage import private_directory, private_open


PURPOSE = """# Personal Inkly logs

This is an owner-only scratch directory for Inkly debugging and improvement.
logs/ contains metadata-only diagnostic JSONL files. conversations/ contains
this owner's full private conversation histories. Home copies are retained.
Other users' private histories are not collected. Group sharing is disabled.
Directories are mode 0700 and files 0600; administrators retain normal access.
Never enter passwords, tokens or private keys. Scratch can be removed by admins.

To stop scratch diagnostics, set logging.private_scratch_dir = "" in
~/.inkly/config.toml. To stop scratch history writes, set state.conversation_dir
back to ~/.inkly/conversations. Existing scratch files are not automatically removed.
"""


def _set_setting(text, section, key, value):
    header = re.search(r"(?m)^\[" + re.escape(section) + r"\][ \t]*(?:#[^\n]*)?$", text)
    setting = key + " = " + json.dumps(value) + "\n"
    if header is None:
        return text.rstrip() + f"\n\n[{section}]\n" + setting
    start = header.end()
    following = re.search(r"(?m)^\s*\[", text[start:])
    end = start + following.start() if following else len(text)
    body = text[start:end]
    pattern = r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[^\n]*(?:\n|$)"
    if re.search(pattern, body):
        body = re.sub(pattern, lambda match: setting, body, count=1)
    else:
        body = "\n" + setting + body
    return text[:start] + body + text[end:]


def _write_private(path, data):
    descriptor = private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def configure_private_scratch(config_path, directory, *, install_runtime=False):
    config_path = Path(config_path).expanduser().absolute()
    directory = Path(directory).expanduser().absolute()
    if directory.exists() or directory.is_symlink():
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
            os.name == "posix"
            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise PermissionError(
                "Existing scratch root must be owner-only, not shared"
            )
    descriptor = private_open(config_path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as source:
        original_bytes = source.read()
    original = original_bytes.decode("utf-8").replace("\r\n", "\n")
    expected = copy.deepcopy(tomllib.loads(original))
    old_history_setting = expected.get("state", {}).get("conversation_dir")
    if old_history_setting in {None, "~/.inkly/conversations"}:
        old_history = Path.home() / ".inkly/conversations"
    else:
        old_history = Path(old_history_setting).expanduser().absolute()
    if old_history not in {
        Path.home() / ".inkly/conversations",
        directory / "conversations",
    }:
        raise ValueError(
            "Migrate customized history separately before changing its path"
        )
    changes = {
        "state": {
            "log_dir": str(Path.home() / ".inkly/logs"),
            "conversation_dir": str(directory / "conversations"),
        },
        "logging": {
            "enabled": True,
            "per_user_logs": True,
            "global_log": False,
            "log_raw_prompts": False,
            "log_raw_ai_responses": False,
            "private_scratch_dir": str(directory / "logs"),
        },
        "logging.shared": {"enabled": False},
    }
    updated = original
    for section, values in changes.items():
        target = expected
        for part in section.split("."):
            target = target.setdefault(part, {})
        target.update(values)
        for key, value in values.items():
            updated = _set_setting(updated, section, key, value)
    if tomllib.loads(updated) != expected:
        raise ValueError("Configuration changes could not preserve unrelated settings")
    private_directory(directory)
    private_directory(directory / "logs")
    private_directory(directory / "conversations")
    backups = private_directory(config_path.parent / "private-scratch-backups")
    backup = backups / f"config-{uuid4().hex}.toml"
    _write_private(backup, original_bytes)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".private-scratch-", dir=config_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as candidate:
            if os.name == "posix":
                validate_private_acl(candidate.fileno(), directory=False)
            candidate.write(updated)
            candidate.flush()
            os.fsync(candidate.fileno())
        config = TomlParser(Path(temporary)).load()
        if install_runtime:
            import install

            runtime = config.state.inkly_home / "lib/inkly"
            launcher = config.state.bin_dir / "ink"
            if any(
                path.is_symlink()
                for path in (
                    config.state.inkly_home,
                    runtime.parent,
                    runtime,
                    config.state.bin_dir,
                    launcher,
                )
            ) or (
                runtime.is_dir()
                and any(path.is_symlink() for path in runtime.rglob("*"))
            ):
                raise PermissionError(
                    "Installed runtime symlinks require manual review"
                )
            if runtime.is_dir():
                with os.fdopen(
                    private_open(
                        backups / f"runtime-{uuid4().hex}.tar.gz",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    ),
                    "wb",
                ) as output:
                    with tarfile.open(fileobj=output, mode="w:gz") as archive:
                        archive.add(runtime, arcname="inkly")
            if launcher.is_file():
                _write_private(
                    backups / f"launcher-{uuid4().hex}", launcher.read_bytes()
                )
            install.STATE_CFG = config.state
            install.setup_runtime_dirs()
            install.install_ink()
        os.replace(temporary, config_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    readme = directory / "README.md"
    if not readme.exists():
        _write_private(readme, PURPOSE.encode("utf-8"))
    return config, backup


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Configure owner-only Inkly scratch storage."
    )
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".inkly/config.toml"
    )
    parser.add_argument("--directory", type=Path, default=Path("/scratch/inklys-logs"))
    parser.add_argument("--install-runtime", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        _, backup = configure_private_scratch(
            arguments.config,
            arguments.directory,
            install_runtime=arguments.install_runtime,
        )
    except (OSError, ValueError, ConfigError):
        print(
            "Owner-only scratch setup failed. Configuration backups are retained; no broader access was granted.",
            file=sys.stderr,
        )
        return 1
    print(f"Owner-only Inkly scratch ready: {arguments.directory}")
    print(f"Private configuration backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
