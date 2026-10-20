# Inkly shared scratch logging

The Cuttlefish destination is **`/scratch/inklys-logs`**. It supports usage review,
debugging and Inkly improvements without access to another person's home.
Administrators manage scratch cleanup; scratch is not durable storage.

## Notice and access scope

**Shared chats are readable by every member of the configured Unix group.**
They may include cluster information, personal details and unpublished work.
They are not anonymous or confidential to their owner. Use the lab-only group
for the original ALICE Lab access scope; using `hpc_users` instead exposes chats
to users outside the lab and requires a deliberate decision, not a permissions
workaround. Never substitute a broader group just to make installation work.

The Cuttlefish template selects **`hpc_pulatovd_lab`**, not `hpc_users`, and enables
collection for new installations. Installation must still verify membership and
directory access; otherwise it falls back to private storage. Existing configs
without the shared section remain private-only until deliberately updated.

The installer displays the destination, reader group, existing/future collection,
credential warning, retention and opt-out instructions. **Never enter passwords,
tokens, credentials or private keys into Inkly.** Common credential markers cause
whole turns to be withheld, but pattern filtering cannot identify every secret.
Unlabelled/novel secrets may not be detected. Do not treat filtering as proof
that arbitrary text is safe. Shared diagnostics never accept chat text at all.

Before installing or enabling collection, review existing history too: the next
write shares a filtered snapshot of the complete available history. If that is
not acceptable, opt out before running queries. No real conversations are copied
by the installer or uploaded to GitHub by this feature.

## Locations and permissions

| Data | Default location | Access |
| --- | --- | --- |
| Original history and recovery | `~/.inkly/conversations/` | Owner/admin |
| Configured history | `[state].conversation_dir` | Owner/admin |
| Private diagnostics | `~/.inkly/logs/` | Owner/admin |
| Shared metadata diagnostics | `/scratch/inklys-logs/inkly-*.jsonl` | Configured group |
| Filtered complete chat snapshots | `/scratch/inklys-logs/chat-<UID>-<subject>.jsonl` | Configured group |
| Original private scratch mirrors | `/scratch/inklys-logs/history-<UID>/` | Owner/admin |

Private directories use `0700`; private files use `0600` on Linux. Originals
retain their full text, including turns withheld from shared snapshots. Different
Unix accounts have different UID archive directories and snapshot names. Each
logical user has a separate history file and subject-hashed snapshot. Hashing is
filename hygiene, not anonymity: ownership/UIDs and usage timing remain visible.

The shared root uses mode `3770`: setgid, sticky, owner/group access, no access
for others. Shared files use `0640`, inherit the configured group, and cannot be
edited by other group members. The sticky root prevents members deleting other
members' files; its owner/admin retain normal control. These are debugging
records, not tamper-proof audit evidence. Foreign-owned, symlinked or hardlinked
history files and group/other-writable private directories are rejected.

## Configuration and installer behavior

```toml
[state]
inkly_home = "~/.inkly"
bin_dir = "~/.inkly/bin"
conversation_dir = "~/.inkly/conversations"
log_dir = "~/.inkly/logs"

[logging]
enabled = true
level = "info"
schema_version = 1
per_user_logs = true
global_log = false
log_user_prompts = true
log_ai_responses = true
log_raw_prompts = false
log_raw_ai_responses = false
max_log_file_mb = 10
max_log_files = 5

[logging.shared]
enabled = true
directory = "/scratch/inklys-logs"
group = "hpc_pulatovd_lab"
retention_days = 0
approval_reference = ""
archive_conversations = true
share_conversations = true
```

Use the actual authorized lab group on other installations. `approval_reference` is an
optional policy/ticket note. Administrators manage group membership; reconnect
after changes. An account must belong to that group before creating/writing the
shared root, even if it owns the directory. Existing configurations without a
shared section retain private-only behavior until explicitly updated.

`python install.py` (also called by `setup.sh`) attempts missing-root creation
when a group is configured. It creates only the final directory, initially
private, assigns the group, sets mode `3770`, validates access and writes a
purpose `README.md`. It does not create missing scratch parents. Existing roots
must already have the exact group/mode and safe ACLs; permissions are never
repaired/widened and existing READMEs are not overwritten. Creation failure gives
a fixed warning and leaves private installation usable. It never requires sudo.
Preparing a root alone does not change the configured collection switches.

If the lab root needs administrator provisioning on Cuttlefish, an administrator
can create the dedicated directory with `install -d -m 3770 -g hpc_pulatovd_lab
/scratch/inklys-logs`, check its ACLs, and add authorized accounts to the lab group.
Do not broaden it to `hpc_users` or mode `777`. A normal install then adds the
purpose README if absent. Current-user membership and second-account access must
be verified separately; a successful code test is not an access grant.

POSIX extended access/default ACLs and rich ACLs require administrator review.
NFSv4 supports only basic non-inheriting ALLOW entries for `OWNER@`, `GROUP@` and
`EVERYONE@`, without named principals. Everyone may have ordinary metadata rights
but no data/listing/traversal/write/delete access. Shared file group rights are
read-only. Other shapes are rejected, not stripped. Checks follow the
[NFSv4 ACL format](https://www.rfc-editor.org/rfc/rfc7530.html#section-6.2.1).

## Opt out

### Owner-only scratch setup without a lab group

For a personal deployment, run from the source checkout with Inkly's Python:

```bash
~/.inkly/venv/bin/python tools/configure_private_scratch.py --install-runtime
```

This backs up configuration/runtime privately, creates `/scratch/inklys-logs` at
mode `0700`, disables group sharing, and keeps metadata diagnostics in both
`~/.inkly/logs/` and `/scratch/inklys-logs/logs/`. Complete history uses
`/scratch/inklys-logs/conversations/` with the home recovery copy retained.
Files are `0600`: only the owner and administrators can read them. Other users'
histories are not collected. An existing shared/foreign-owned root is refused,
not converted. Set `[logging] private_scratch_dir = ""` to stop the extra private
diagnostic destination; change `state.conversation_dir` back to its home default
to stop scratch history writes. Existing scratch files are not deleted.

### Disable group sharing

Edit `~/.inkly/config.toml` before the next query:

```toml
[logging.shared]
enabled = false
```

Change the existing section rather than adding a duplicate TOML table. This
stops new shared diagnostics, chat snapshots and private scratch mirrors; private
home history continues. Alternatively, `share_conversations = false` disables
shared text while retaining metadata and the owner-private scratch mirror.
`archive_conversations = false` disables only the private scratch mirror.
`[logging] enabled = false` disables all diagnostic and scratch collection, not
the separate private conversation history. `[conversation] enabled = false`
also disables conversation persistence/sharing; it is not needed just to opt out
of scratch. Restart running processes after editing configuration.

**Opting out does not delete files already shared or copies readers have made.**
Contact the storage administrator if existing shared data needs removal. Private
copies remain accessible to their owner and administrators, not ordinary readers.

## History preservation, concurrency and outages

`~/.inkly/conversations/` remains a private legacy/recovery copy when the configured
history path changes. Every write merges available valid histories, preserves
repeated legacy turns using occurrence IDs and saves through private temporary
files, `fsync` and atomic replacement under a per-user interprocess lock. New
turns have UUIDs. Corrupt originals are preserved/excluded from writes. If no
private destination is usable, Inkly reports failure rather than claiming a save.
Stop old Inkly versions before migration; they do not honor these locks.

Shared chat snapshots are generated after private persistence, under the same
history lock. A private temporary snapshot is fully written and synced before
becoming group-readable and atomically replacing that owner's snapshot. Readers
see the previous complete file or the new complete file, not interleaved lines.
Different users have separate files. Replacement failure preserves the previous
snapshot and private history. The next successful write rebuilds the complete
snapshot, including turns from an outage. It is a current full-history snapshot,
not an append-only or size-bounded chat log.

Diagnostic sessions instead exclusively own random-named segmented JSONL streams;
threads serialize writes, and different processes/users do not append to one
file. Active names end `.active.jsonl`, completed names `.jsonl`. Rotation respects
`max_log_file_mb`. Python's
[logging cookbook](https://docs.python.org/3.9/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes)
distinguishes thread safety from cross-process serialization.

Unavailable/unsafe/full scratch yields fixed warnings, never raw error dumps, and
does not prevent private history saves. Missing shared roots are not recreated
by runtime; rerun the installer or ask an administrator to provision them. Once
scratch returns, the next write restores the private mirror and shared snapshot.
Diagnostic events may be dropped if no destination works; private diagnostic
files are never automatically replayed to shared storage.

## Retention and readers

**`retention_days = 0` delegates shared removal to the administrator.** No Inkly
age or file-count pruning applies to shared diagnostic files in this mode. A
positive value optionally prunes that account's aged completed diagnostic files
when it next finalizes a stream; unrelated, foreign-owned and active files are
not pruned. Snapshots and full histories are never age/file-count pruned by Inkly.
Private diagnostic streams separately retain `max_log_files` completed segments.
Administrators must manage scratch cleanup, quotas and total capacity. Available
`df` space is not a verified project/user quota or a durability guarantee.

Authorized readers may parse `inkly-*.jsonl` for metadata only, or `chat-*.jsonl`
for filtered conversation text. Both use the existing
[schema-v1 JSONL envelope](../logging_schema.md), with distinct payload contracts.
No private archive access is needed. Never copy raw chat or cluster logs into
GitHub issues or the private development notebook.

## Deployment verification

Run with synthetic data on Linux, including the actual scratch mount:

```bash
python -m pytest -q
ruff check .
ruff format --check .
stat -c '%a %G %n' /scratch/inklys-logs
getfacl /scratch/inklys-logs
df -h /scratch/inklys-logs
```

Regression tests cover filtering, separate diagnostic/chat contracts, original
preservation, archival, opt-outs, fallback, migration, ACLs, permissions, concurrent
writers, failed/atomic replacements and rotation.

Before claiming live shared access, **two intended reader accounts** must each
write synthetic diagnostics/chats and read each other's shared files, but not
edit/delete them or read the other's private archives. An account outside the
configured group must not list/read the root. Verify quota/cleanup policy with
the administrator. Never change another user's home access for testing.

Record only sanitized outcomes on the issue and in the private notebook. Several
processes under one account do not establish real second-account access; unit
tests do not prove deployment readiness or perfect credential detection.
