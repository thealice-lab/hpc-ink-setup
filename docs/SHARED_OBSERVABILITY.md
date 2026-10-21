# Inkly shared logging

Inkly can write diagnostic metadata and filtered conversation snapshots to
approved lab storage without granting access to another user's home directory.
The Cuttlefish template uses `/scratch/inklys-logs`; scratch is not durable.

## Collection and readers

Every member of the configured Unix group can read shared chat snapshots. In
the NFSv4 collector layout, the directory owner can also read them. They
may contain personal details and unpublished work; they are not anonymous.
Use the approved lab-only group, never a broader group to bypass access failures.

The installer explains collection, readers and opt-outs. The next history save
can share a filtered snapshot of **existing and future history**, so review the
settings before running queries. Never enter passwords, tokens or private keys.
Common credential/error markers withhold whole turns, but filtering cannot
guarantee secret removal. Metadata diagnostics never contain conversation text.

## Locations and permissions

| Data | Location | Readers |
| --- | --- | --- |
| Private history and home recovery | `[state].conversation_dir` and `~/.inkly/conversations/` | Owner/admin |
| Private diagnostics | `[state].log_dir`, default `~/.inkly/logs/` | Owner/admin |
| Shared metadata | `<shared root>/inkly-*.jsonl` | Lab group/collector |
| Filtered full-history snapshots | `<shared root>/chat-<UID>-<subject>.jsonl` | Lab group/collector |
| Optional complete private archives | `<shared root>/history-<UID>/` | Owner/admin |

Private directories/files use `0700`/`0600` on Linux. The group-owned layout requires
the configured group and exact mode `3770` (setgid and sticky); shared files use
`0640`. Other group members may read files but cannot edit or delete them;
owners/administrators retain control. Supported ACLs are also validated. Unsafe
roots/files are rejected, including symlinked or foreign-owned history files.
UIDs, ownership and timestamps remain visible; hashed filenames are not anonymity.

An explicitly provisioned NFSv4 collector layout instead uses sticky mode `1770`.
Its non-inheriting ACL grants the configured numeric lab group directory access,
retains owner control, and gives the owning group/everyone no data access. Each
published file grants read access only to that lab group and the root owner
(collector), even when the collector is not a lab-group member. Other accounts
are rejected. Additional readers, write/control rights on shared files,
inheritance and Deny entries are rejected. Installation validates this layout
without converting an existing directory or widening private subdirectories.

## Configuration

Edit the existing sections in `~/.inkly/config.toml`; do not duplicate TOML tables.
This example uses a placeholder group: select the actual approved lab group.
See [config.toml](../config.toml) for the Cuttlefish template and other settings.

```toml
[state]
conversation_dir = "~/.inkly/conversations"
log_dir = "~/.inkly/logs"

[logging]
enabled = true
per_user_logs = true

[logging.shared]
enabled = true
directory = "/scratch/inklys-logs"
group = "approved-lab-group"
retention_days = 0
archive_conversations = true
share_conversations = true
```

Existing configurations without a shared section remain private-only. The
installer can create a missing final root and purpose README when the configured
group exists and the installing account is a member. Existing roots must already
have the correct group, mode and supported ACLs; the installer does not widen
permissions, create missing parent directories or require sudo. Administrators
must arrange group membership and any required storage provisioning.

## Opt-outs and owner-only scratch

Restart running Inkly processes after editing configuration.

| Setting | Effect on future writes |
| --- | --- |
| `[logging.shared] enabled = false` | Stops shared diagnostics, chat snapshots and shared-root private archives |
| `share_conversations = false` | Stops shared chat snapshots; keeps enabled diagnostics/private archives |
| `archive_conversations = false` | Stops the shared-root private archive only |
| `[logging] enabled = false` | Stops diagnostics and shared-root collection; private history remains enabled separately |
| `[conversation] enabled = false` | Stops conversation persistence and snapshots |

Opting out does not delete existing files or copies made by readers. Ask the
storage administrator about removal. Private originals include withheld turns.

For owner-only scratch, run from the source checkout with Inkly's Python:

```bash
~/.inkly/venv/bin/python tools/configure_private_scratch.py --install-runtime
```

The utility backs up configuration/runtime/launcher privately, disables group
sharing, keeps home diagnostics, and adds private scratch logs/history with home
recovery. An existing shared or foreign-owned root is refused, not converted.
This mode does not collect other users' histories or establish shared lab access.
Clear `[logging].private_scratch_dir` to stop the extra private diagnostic sink;
restore `[state].conversation_dir` to its home default to stop private scratch
history writes. These destinations are independent of `[logging.shared].enabled`.

## Recovery, retention and verification

History saves merge available valid legacy/configured/recovery records using
per-user interprocess locks and atomic replacement. Corrupt originals are
preserved. Stop older Inkly versions before migration; they do not honor these
locks. If no private destination is usable, saving reports failure.

Shared snapshots are replaced atomically after private persistence. Each account
and logical user has separate files; readers see a complete old or new snapshot.
Unavailable shared storage does not block usable private history, and the next
successful save rebuilds the full snapshot. Snapshots are not append-only or
size-bounded. Diagnostic streams are per invocation and rotate independently;
events lost during an outage are not replayed from private logs. See
[logging_schema.md](../logging_schema.md) for both JSONL formats.

`retention_days = 0` delegates shared removal to administrators, without Inkly
age/file-count pruning. Positive retention prunes only that account's aged,
completed diagnostic files; snapshots/full histories are not pruned by Inkly.
Private diagnostics use `max_log_files`. Confirm quota, capacity and cleanup
policy with administrators; free filesystem space does not establish quota.

[UNCW's Cuttlefish policy](https://www.uncw.edu/seahawk-life/services/technology/support/services/academic-research-computing/high-performance-computing)
states that scratch is not backed up and files not accessed for 30 days are
automatically deleted. Inkly's retention zero does not override that policy.

Before declaring shared deployment complete, use synthetic records on the actual
storage mount. Two independently authenticated authorized lab accounts must each
write and read shared records, without editing/deleting the other's files or
reading private archives. Verify an account outside the group cannot read/list
the root. Run `python -m pytest -q`, `ruff check .` and `ruff format --check .`;
multiple processes under one account do not prove independent account access.
Record sanitized outcomes on the issue; keep conversations, credentials and raw
cluster logs out of GitHub and private notes.
