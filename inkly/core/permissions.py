from __future__ import annotations

import os
import stat
import struct
from pathlib import Path


NFS4_METADATA_MASK = 0x00120080
NFS4_DIRECTORY_DATA_MASK = 0x00000067
NFS4_FILE_READ_MASK = 0x00000001
NFS4_KNOWN_ACCESS_MASK = 0x001F01FF
NFS4_GROUP_FLAG = 0x40


def _decode_nfs4_acl(data: bytes) -> dict:
    try:
        count = struct.unpack_from(">I", data)[0]
        if len(data) > 512 or not 3 <= count <= 5:
            raise ValueError
        offset, entries = 4, {}
        for _ in range(count):
            kind, flags, mask, length = struct.unpack_from(">IIII", data, offset)
            offset += 16
            padded = (length + 3) & ~3
            if length > 32 or offset + padded > len(data):
                raise ValueError
            principal = data[offset : offset + length].decode("ascii")
            padding = data[offset + length : offset + padded]
            offset += padded
            key = (flags, principal)
            if (
                kind != 0
                or flags not in {0, NFS4_GROUP_FLAG}
                or any(padding)
                or key in entries
                or mask & ~NFS4_KNOWN_ACCESS_MASK
            ):
                raise ValueError
            entries[key] = mask
        if offset != len(data):
            raise ValueError
        return entries
    except (ValueError, UnicodeError, struct.error):
        raise PermissionError("Unrecognized NFSv4 storage ACL") from None


def _encode_nfs4_acl(entries: dict) -> bytes:
    result = struct.pack(">I", len(entries))
    for (flags, principal), mask in entries.items():
        encoded = principal.encode("ascii")
        result += struct.pack(">IIII", 0, flags, mask, len(encoded))
        result += encoded + b"\0" * ((-len(encoded)) % 4)
    return result


def validate_basic_nfs4_acl(
    data: bytes, *, directory: bool, private: bool = False
) -> None:
    """Accept only three non-inheriting ALLOW entries for special principals."""
    try:
        decoded = _decode_nfs4_acl(data)
        if set(decoded) != {(0, name) for name in ("OWNER@", "GROUP@", "EVERYONE@")}:
            raise ValueError
        entries = {name: mask for (_, name), mask in decoded.items()}
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


def shared_group_id(group: str, *, require_member: bool = True) -> int:
    if os.name != "posix":
        raise PermissionError("Shared storage requires POSIX permissions")
    import grp

    try:
        group_id = grp.getgrnam(group).gr_gid
    except KeyError:
        raise PermissionError("The configured lab group does not exist") from None
    if require_member and group_id not in {*os.getgroups(), os.getgid()}:
        raise PermissionError("This account must join the configured lab group")
    return group_id


def _validate_collector_acl(
    data: bytes, *, group_id: int, collector_id: int, directory: bool, owner_id: int
) -> None:
    entries = _decode_nfs4_acl(data)
    expected = {(0, name) for name in ("OWNER@", "GROUP@", "EVERYONE@")}
    group_key = (NFS4_GROUP_FLAG, str(group_id))
    expected.add(group_key)
    reader_mask = NFS4_METADATA_MASK | NFS4_FILE_READ_MASK
    group_mask = (
        NFS4_METADATA_MASK | NFS4_DIRECTORY_DATA_MASK if directory else reader_mask
    )
    if not directory and owner_id != collector_id:
        expected.add((0, str(collector_id)))
        if entries.get((0, str(collector_id))) != reader_mask:
            raise PermissionError("Missing collector read access")
    if (
        set(entries) != expected
        or entries.get(group_key) != group_mask
        or entries[(0, "GROUP@")] & ~NFS4_METADATA_MASK
        or entries[(0, "EVERYONE@")] & ~NFS4_METADATA_MASK
        or entries[(0, "OWNER@")] & (0x67 if directory else 3)
        != (0x67 if directory else 3)
    ):
        raise PermissionError(
            "NFSv4 storage must allow only its lab group and collector"
        )


def validate_shared_directory(descriptor: int, group_id: int) -> bool:
    """Return True only for the exact owner/collector plus named-lab ACL mode."""
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("Shared storage must be a directory")
    attributes = set(os.listxattr(descriptor))
    if {
        "system.posix_acl_access",
        "system.posix_acl_default",
        "system.richacl",
    } & attributes:
        raise PermissionError("Extended storage ACLs require administrator review")
    if "system.nfs4_acl" in attributes:
        data = os.getxattr(descriptor, "system.nfs4_acl")
        if len(_decode_nfs4_acl(data)) != 3:
            # Sticky protects other writers' directory entries; no inherited grants.
            if stat.S_IMODE(info.st_mode) != 0o1770:
                raise PermissionError("Collector storage must use sticky mode 1770")
            _validate_collector_acl(
                data,
                group_id=group_id,
                collector_id=info.st_uid,
                directory=True,
                owner_id=info.st_uid,
            )
            return True
    if info.st_gid != group_id or stat.S_IMODE(info.st_mode) != 0o3770:
        raise PermissionError("Shared storage must use its lab group and mode 3770")
    validate_shared_acl(descriptor, directory=True)
    return False


def open_shared_directory(directory: Path, group: str) -> int:
    group_id = shared_group_id(group, require_member=False)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        collector_mode = validate_shared_directory(descriptor, group_id)
        if group_id not in {*os.getgroups(), os.getgid()} and not (
            collector_mode and os.fstat(descriptor).st_uid == os.getuid()
        ):
            raise PermissionError(
                "Only lab members and the configured directory owner may write"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate_shared_file(descriptor: int, root: int, group: str) -> None:
    group_id = shared_group_id(group, require_member=False)
    collector_mode = validate_shared_directory(root, group_id)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o640
    ):
        raise PermissionError("Unexpected shared file ownership or mode")
    if collector_mode:
        attributes = set(os.listxattr(descriptor))
        if {
            "system.posix_acl_access",
            "system.posix_acl_default",
            "system.richacl",
        } & attributes:
            raise PermissionError("Unexpected shared file ACL")
        _validate_collector_acl(
            os.getxattr(descriptor, "system.nfs4_acl"),
            group_id=group_id,
            collector_id=os.fstat(root).st_uid,
            directory=False,
            owner_id=info.st_uid,
        )
    else:
        if info.st_gid != os.fstat(root).st_gid:
            raise PermissionError("Unexpected shared file group")
        validate_shared_acl(descriptor, directory=False)


def grant_shared_file_access(descriptor: int, root: int, group: str) -> None:
    """Publish read access only after creating/writing the file privately."""
    group_id = shared_group_id(group, require_member=False)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise PermissionError("New shared files must start owner-private")
    collector_mode = validate_shared_directory(root, group_id)
    original_acl = None
    if collector_mode:
        validate_private_acl(descriptor, directory=False)
        original_acl = os.getxattr(descriptor, "system.nfs4_acl")
    try:
        if collector_mode:
            entries = _decode_nfs4_acl(original_acl)
            reader_mask = NFS4_METADATA_MASK | NFS4_FILE_READ_MASK
            entries[(NFS4_GROUP_FLAG, str(group_id))] = reader_mask
            collector_id = os.fstat(root).st_uid
            if collector_id != os.getuid():
                entries[(0, str(collector_id))] = reader_mask
            os.setxattr(descriptor, "system.nfs4_acl", _encode_nfs4_acl(entries))
        else:
            os.fchmod(descriptor, 0o640)
        validate_shared_file(descriptor, root, group)
    except BaseException:
        if original_acl is not None:
            try:
                os.setxattr(descriptor, "system.nfs4_acl", original_acl)
            except OSError:
                pass
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        raise
