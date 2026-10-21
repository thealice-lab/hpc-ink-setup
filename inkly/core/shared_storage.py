from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

from inkly.core.permissions import (
    open_shared_directory,
    shared_group_id,
    grant_shared_file_access,
    validate_shared_directory,
)


PURPOSE = """# Inkly lab logs

This scratch directory supports authorized ALICE Lab members investigating
Inkly usage, errors and improvements. It is not public or durable storage.

inkly-*.jsonl diagnostic files contain allowlisted metadata, never chat text.
chat-<UID>-<subject>.jsonl files contain complete filtered conversation snapshots
for improvements. They can include personal details and unpublished research;
the configured group and NFSv4 directory collector can read them. They are not anonymous.

NEVER enter passwords, tokens or private keys into Inkly. Common credential
markers cause whole turns to be withheld, but filtering cannot detect every
secret. history-<Unix UID>/ directories contain each owner's complete private
archive (0700 directories, 0600 JSON files), including withheld originals.
Set logging.shared.enabled = false in ~/.inkly/config.toml to stop shared-root
collection. Private history in your home remains available to you and admins.

Each writer owns separate files. The lab group may read, but not edit, them.
Use group mode 3770 or the documented sticky NFSv4 collector ACL layout.
Shared files have mode 0640 and validated reader ACLs. Do not broaden
access to all cluster users. Administrators manage scratch removal; Inkly does
not delete shared files when retention_days = 0. Scratch can disappear without
notice, so private recovery histories remain in each owner's home directory.

See docs/SHARED_OBSERVABILITY.md for configuration, access, recovery and opt-outs.
See logging_schema.md for diagnostic and conversation formats.
"""


def prepare_shared_storage(shared) -> Path:
    """Create only a missing root; never repair or widen an existing root."""
    settings = replace(shared, enabled=True).validate()
    directory = Path(settings.directory)
    created = False
    if not directory.exists() and not directory.is_symlink():
        group_id = shared_group_id(settings.group)
        try:
            directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass

    if created:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("New shared root has unsafe ownership or access")
            os.fchown(descriptor, -1, group_id)
            os.fchmod(descriptor, 0o3770)
            validate_shared_directory(descriptor, group_id)
        finally:
            os.close(descriptor)

    root = open_shared_directory(directory, settings.group)
    try:
        try:
            descriptor = os.open(
                "README.md",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root,
            )
        except FileExistsError:
            return directory
        try:
            info = os.fstat(descriptor)
            if info.st_nlink != 1:
                raise PermissionError("Shared README has unsafe ownership")
            grant_shared_file_access(descriptor, root, settings.group)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
                output.write(PURPOSE)
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return directory
    finally:
        os.close(root)
