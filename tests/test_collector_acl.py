"""Exact named-ACL policy tests; independent Unix-account checks remain a rollout gate."""

import os
import stat
from pathlib import Path

import pytest

from inkly.config import SharedLoggingConfig, TomlParser
from inkly.core import permissions
from inkly.core.diagnostics import EventSink
from inkly.core.shared_conversations import publish_conversation_snapshot
from inkly.core.shared_storage import prepare_shared_storage
from tests.test_observability_permissions import basic_entries, encode_acl


LAB = 987654321
COLLECTOR = 123456789
READER = permissions.NFS4_METADATA_MASK | 1


@pytest.mark.skipif(os.name != "posix", reason="Cuttlefish uses POSIX absolute paths")
def test_default_collection_root_does_not_overlap_private_scratch(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[state]\nconversation_dir = "/scratch/inklys-logs/conversations"\n'
        '[logging]\nprivate_scratch_dir = "/scratch/inklys-logs/logs"\n'
        '[logging.shared]\nenabled = true\ngroup = "synthetic-lab"\n',
        encoding="utf-8",
    )
    config = TomlParser(path).load()
    assert config.logging.shared.directory == "/scratch/inklys-logs/shared"


def named_entries(*, directory=False, owner=COLLECTOR):
    entries = basic_entries(directory=directory)
    entries[1] = ("GROUP@", permissions.NFS4_METADATA_MASK, 0, 0)
    group_mask = READER | 0x66 if directory else READER
    entries.append((str(LAB), group_mask, 0, 0x40))
    if not directory and owner != COLLECTOR:
        entries.append((str(COLLECTOR), READER, 0, 0))
    return entries


@pytest.mark.parametrize(
    "directory,owner", [(True, COLLECTOR), (False, COLLECTOR), (False, 42)]
)
def test_exact_collector_and_lab_grants_accept_server_reordering(directory, owner):
    entries = named_entries(directory=directory, owner=owner)
    permissions._validate_collector_acl(
        encode_acl(list(reversed(entries))),
        group_id=LAB,
        collector_id=COLLECTOR,
        directory=directory,
        owner_id=owner,
    )
    with pytest.raises(PermissionError):
        permissions.validate_basic_nfs4_acl(encode_acl(entries), directory=directory)
    with pytest.raises(PermissionError):
        permissions.validate_basic_nfs4_acl(
            encode_acl(entries), directory=directory, private=True
        )


@pytest.mark.parametrize(
    "change",
    [
        "extra_reader",
        "wrong_lab",
        "wrong_collector",
        "missing_collector",
        "group_write",
        "collector_write",
        "world_read",
        "owning_group_read",
        "inheritance",
        "deny",
        "duplicate",
        "user_instead_of_group",
    ],
)
def test_unsafe_or_incomplete_named_file_grants_are_rejected(change):
    entries = named_entries(owner=42)
    if change == "extra_reader":
        entries.append(("314159", READER, 0, 0))
    elif change == "wrong_lab":
        entries[3] = (str(LAB + 1), READER, 0, 0x40)
    elif change == "wrong_collector":
        entries[4] = (str(COLLECTOR + 1), READER, 0, 0)
    elif change == "missing_collector":
        entries.pop()
    elif change == "group_write":
        entries[3] = (str(LAB), READER | 2, 0, 0x40)
    elif change == "collector_write":
        entries[4] = (str(COLLECTOR), READER | 2, 0, 0)
    elif change == "world_read":
        entries[2] = ("EVERYONE@", READER, 0, 0)
    elif change == "owning_group_read":
        entries[1] = ("GROUP@", READER, 0, 0)
    elif change == "inheritance":
        entries[3] = (str(LAB), READER, 0, 0x41)
    elif change == "deny":
        entries[3] = (str(LAB), READER, 1, 0x40)
    elif change == "duplicate":
        entries.append(entries[4])
    elif change == "user_instead_of_group":
        entries[3] = (str(LAB), READER, 0, 0)
    with pytest.raises(PermissionError):
        permissions._validate_collector_acl(
            encode_acl(entries),
            group_id=LAB,
            collector_id=COLLECTOR,
            directory=False,
            owner_id=42,
        )


@pytest.fixture
def named_storage(tmp_path, monkeypatch):
    """Model NFS xattrs on ordinary POSIX files; this is not a second account."""
    if os.name != "posix":
        pytest.skip("File-descriptor storage integration requires POSIX")
    root = tmp_path / "shared"
    root.mkdir(mode=0o700)
    root.chmod(0o1770)
    owner = os.getuid()
    entries = named_entries(directory=True)
    attributes = {root.stat().st_ino: encode_acl(entries)}

    def get_acl(descriptor, name):
        assert name == "system.nfs4_acl"
        info = os.fstat(descriptor)
        if info.st_ino in attributes:
            return attributes[info.st_ino]
        basic = basic_entries(directory=stat.S_ISDIR(info.st_mode))
        basic[1] = ("GROUP@", permissions.NFS4_METADATA_MASK, 0, 0)
        return encode_acl(basic)

    def set_acl(descriptor, name, data):
        assert name == "system.nfs4_acl"
        attributes[os.fstat(descriptor).st_ino] = data
        os.fchmod(descriptor, 0o640)

    def resolve_group(group, *, require_member=True):
        assert group == "synthetic-lab"
        if require_member:
            raise PermissionError("Synthetic owner is not a lab member")
        return LAB

    monkeypatch.setattr(permissions, "shared_group_id", resolve_group)
    monkeypatch.setattr(os, "listxattr", lambda fd: ["system.nfs4_acl"])
    monkeypatch.setattr(os, "getxattr", get_acl)
    monkeypatch.setattr(os, "setxattr", set_acl)
    settings = SharedLoggingConfig(
        enabled=True, directory=str(root), group="synthetic-lab"
    )
    return settings, attributes, owner


def test_nonmember_directory_owner_can_install_and_publish(named_storage):
    settings, attributes, owner = named_storage
    root = prepare_shared_storage(settings)
    sink = EventSink(root, "a" * 32, 4096, 1, settings)
    sink.append(b'{"synthetic":true}\n')
    sink.close()
    publish_conversation_snapshot(
        settings, "synthetic", [{"role": "user", "content": "test"}]
    )
    publish_conversation_snapshot(
        settings, "synthetic", [{"role": "user", "content": "updated"}]
    )
    files = [root / "README.md", *root.glob("*.jsonl")]
    assert len(files) == 3
    for path in files:
        permissions._validate_collector_acl(
            attributes[path.stat().st_ino],
            group_id=LAB,
            collector_id=owner,
            directory=False,
            owner_id=owner,
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_unapproved_nonowner_is_denied_even_if_directory_can_be_opened(
    named_storage, monkeypatch
):
    settings, _, owner = named_storage
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    monkeypatch.setattr(os, "getgroups", lambda: [])
    with pytest.raises(PermissionError):
        permissions.open_shared_directory(Path(settings.directory), settings.group)
    monkeypatch.setattr(os, "getgroups", lambda: [LAB])
    descriptor = permissions.open_shared_directory(
        Path(settings.directory), settings.group
    )
    os.close(descriptor)


def test_named_root_requires_sticky_and_exact_group_permissions(named_storage):
    settings, attributes, _ = named_storage
    root = Path(settings.directory)
    root.chmod(0o770)
    with pytest.raises(PermissionError):
        permissions.open_shared_directory(root, settings.group)
    root.chmod(0o1770)
    entries = named_entries(directory=True)
    entries[3] = (str(LAB), READER | 0x20, 0, 0x40)
    attributes[root.stat().st_ino] = encode_acl(entries)
    with pytest.raises(PermissionError):
        permissions.open_shared_directory(root, settings.group)


def test_existing_snapshot_acl_tampering_does_not_replace_file(named_storage):
    settings, attributes, _ = named_storage
    publish_conversation_snapshot(
        settings, "synthetic", [{"role": "user", "content": "original"}]
    )
    snapshot = next(Path(settings.directory).glob("chat-*.jsonl"))
    original = snapshot.read_bytes()
    entries = named_entries(owner=os.getuid())
    entries[2] = ("EVERYONE@", READER, 0, 0)
    attributes[snapshot.stat().st_ino] = encode_acl(entries)
    with pytest.raises(PermissionError):
        publish_conversation_snapshot(
            settings, "synthetic", [{"role": "user", "content": "new"}]
        )
    assert snapshot.read_bytes() == original


def test_lab_writer_grants_nonmember_collector_read_access(named_storage, monkeypatch):
    settings, attributes, _ = named_storage
    root_inode = Path(settings.directory).stat().st_ino
    original_fstat = os.fstat

    def collector_owns_root(descriptor):
        info = original_fstat(descriptor)
        if info.st_ino == root_inode:
            values = list(info)
            values[4] = COLLECTOR
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", collector_owns_root)
    monkeypatch.setattr(os, "getgroups", lambda: [LAB])
    publish_conversation_snapshot(
        settings, "synthetic", [{"role": "user", "content": "test"}]
    )
    snapshot = next(Path(settings.directory).glob("chat-*.jsonl"))
    acl = permissions._decode_nfs4_acl(attributes[snapshot.stat().st_ino])
    assert acl[(0, str(COLLECTOR))] == READER
    assert acl[(0x40, str(LAB))] == READER
    assert not acl[(0, "GROUP@")] & ~permissions.NFS4_METADATA_MASK
    assert not acl[(0, "EVERYONE@")] & ~permissions.NFS4_METADATA_MASK


def test_acl_publication_failure_preserves_previous_snapshot(
    named_storage, monkeypatch
):
    settings, _, _ = named_storage
    publish_conversation_snapshot(
        settings, "synthetic", [{"role": "user", "content": "original"}]
    )
    snapshot = next(Path(settings.directory).glob("chat-*.jsonl"))
    original = snapshot.read_bytes()

    def unavailable(*args):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(os, "setxattr", unavailable)
    with pytest.raises(OSError):
        publish_conversation_snapshot(
            settings, "synthetic", [{"role": "user", "content": "new"}]
        )
    assert snapshot.read_bytes() == original
    assert not list(Path(settings.directory).glob(".chat-*.tmp"))
