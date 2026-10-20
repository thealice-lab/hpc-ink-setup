from __future__ import annotations

import os
import stat
import struct
from pathlib import Path


NFS4_METADATA_MASK = 0x00120080
NFS4_DIRECTORY_DATA_MASK = 0x00000067
NFS4_FILE_READ_MASK = 0x00000001
NFS4_KNOWN_ACCESS_MASK = 0x001F01FF


def validate_basic_nfs4_acl(
    data: bytes, *, directory: bool, private: bool = False
) -> None:
    """Accept only three non-inheriting ALLOW entries for special principals."""
    try:
        if len(data) > 256 or struct.unpack_from(">I", data)[0] != 3:
            raise ValueError
        offset = 4
        entries = {}
        for _ in range(3):
            ace_type, flags, mask, length = struct.unpack_from(">IIII", data, offset)
            offset += 16
            padded_length = (length + 3) & ~3
            if length > 32 or offset + padded_length > len(data):
                raise ValueError
            principal = data[offset : offset + length].decode("ascii")
            padding = data[offset + length : offset + padded_length]
            offset += padded_length
            if (
                ace_type != 0
                or flags != 0
                or any(padding)
                or principal not in {"OWNER@", "GROUP@", "EVERYONE@"}
                or principal in entries
                or mask & ~NFS4_KNOWN_ACCESS_MASK
            ):
                raise ValueError
            entries[principal] = mask
        if offset != len(data):
            raise ValueError
        group_mask = NFS4_METADATA_MASK
        if not private:
            group_mask |= NFS4_DIRECTORY_DATA_MASK if directory else NFS4_FILE_READ_MASK
        if (
            entries["EVERYONE@"] & ~NFS4_METADATA_MASK
            or entries["GROUP@"] & ~group_mask
        ):
            raise ValueError
    except (ValueError, KeyError, struct.error):
        raise PermissionError(
            "Shared NFSv4 ACL requires administrator review"
        ) from None


def _validate_acl(descriptor: int, *, directory: bool, private: bool) -> None:
    attributes = set(os.listxattr(descriptor))
    if {
        "system.posix_acl_access",
        "system.posix_acl_default",
        "system.richacl",
    } & attributes:
        raise PermissionError("Extended storage ACLs require administrator review")
    if "system.nfs4_acl" in attributes:
        validate_basic_nfs4_acl(
            os.getxattr(descriptor, "system.nfs4_acl"),
            directory=directory,
            private=private,
        )


def validate_shared_acl(descriptor: int, *, directory: bool) -> None:
    _validate_acl(descriptor, directory=directory, private=False)


def validate_private_acl(descriptor: int, *, directory: bool) -> None:
    _validate_acl(descriptor, directory=directory, private=True)


def shared_group_id(group: str) -> int:
    if os.name != "posix":
        raise PermissionError("Shared storage requires POSIX permissions")
    import grp

    try:
        group_id = grp.getgrnam(group).gr_gid
    except KeyError:
        raise PermissionError("The configured lab group does not exist") from None
    if group_id not in {*os.getgroups(), os.getgid()}:
        raise PermissionError("This account must join the configured lab group")
    return group_id


def validate_shared_directory(descriptor: int, group_id: int) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_gid != group_id
        or stat.S_IMODE(info.st_mode) != 0o3770
    ):
        raise PermissionError("Shared storage must use its lab group and mode 3770")
    validate_shared_acl(descriptor, directory=True)


def open_shared_directory(directory: Path, group: str) -> int:
    group_id = shared_group_id(group)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        validate_shared_directory(descriptor, group_id)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
