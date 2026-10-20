import struct

import pytest

from inkly.core.permissions import validate_basic_nfs4_acl


def encode_acl(entries):
    parts = [struct.pack(">I", len(entries))]
    for principal, mask, ace_type, flags in entries:
        encoded = principal.encode("ascii")
        parts.append(struct.pack(">IIII", ace_type, flags, mask, len(encoded)))
        parts.append(encoded + b"\0" * ((-len(encoded)) % 4))
    return b"".join(parts)


def basic_entries(*, directory=True):
    return [
        ("OWNER@", 0x001601E7 if directory else 0x00160187, 0, 0),
        ("GROUP@", 0x001200E7 if directory else 0x00120081, 0, 0),
        ("EVERYONE@", 0x00120080, 0, 0),
    ]


@pytest.mark.parametrize("directory", [True, False])
def test_basic_group_only_nfs4_acl_is_accepted(directory):
    validate_basic_nfs4_acl(
        encode_acl(basic_entries(directory=directory)), directory=directory
    )


@pytest.mark.parametrize("directory", [True, False])
def test_private_nfs4_acl_allows_no_group_data_permissions(directory):
    entries = basic_entries(directory=directory)
    with pytest.raises(PermissionError):
        validate_basic_nfs4_acl(encode_acl(entries), directory=directory, private=True)
    entries[1] = ("GROUP@", 0x00120080, 0, 0)
    validate_basic_nfs4_acl(encode_acl(entries), directory=directory, private=True)


@pytest.mark.parametrize(
    "replacement",
    [
        ("named-user@example.test", 0x00120081, 0, 0),
        ("EVERYONE@", 0x00120081, 0, 0),
        ("EVERYONE@", 0x001200A0, 0, 0),
        ("EVERYONE@", 0x00120180, 0, 0),
        ("EVERYONE@", 0x00160080, 0, 0),
        ("EVERYONE@", 0x00120080, 0, 1),
        ("EVERYONE@", 0x00120080, 1, 0),
        ("EVERYONE@", 0x80120080, 0, 0),
        ("OWNER@", 0x00120080, 0, 0),
    ],
)
def test_named_expanded_inherited_or_ambiguous_acl_is_rejected(replacement):
    entries = basic_entries()
    entries[-1] = replacement
    with pytest.raises(PermissionError):
        validate_basic_nfs4_acl(encode_acl(entries), directory=True)


def test_group_cannot_gain_write_or_acl_control_on_diagnostic_file():
    for mask in (0x00120083, 0x00160081, 0x001A0081):
        entries = basic_entries(directory=False)
        entries[1] = ("GROUP@", mask, 0, 0)
        with pytest.raises(PermissionError):
            validate_basic_nfs4_acl(encode_acl(entries), directory=False)


@pytest.mark.parametrize("data", [b"", b"\0\0\0", b"\0\0\0\xff", b"\0\0\0\x03"])
def test_malformed_nfs4_acl_is_rejected(data):
    with pytest.raises(PermissionError):
        validate_basic_nfs4_acl(data, directory=True)


def test_extra_entries_and_trailing_bytes_are_rejected():
    entries = basic_entries()
    for encoded in (
        encode_acl(entries + [("named-user", 1, 0, 0)]),
        encode_acl(entries) + b"\0\0\0\0",
        encode_acl(entries)[:-1],
    ):
        with pytest.raises(PermissionError):
            validate_basic_nfs4_acl(encoded, directory=True)
