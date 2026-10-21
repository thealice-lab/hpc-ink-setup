# Inkly JSONL Logging Schema (v1)

Each line in a log file is a single JSON object.

## Required top-level fields

- schema_version: int
- event_type: string
- timestamp: ISO 8601 UTC string
- session: object
- payload: object

## Event types (v1)

- session_start
- session_end
- user_prompt
- ai_response
- guardrail_block
- error

## Session object

- session_id: string (random per ink invocation)
- user_id: null (no raw username or reversible username hash)
- host: string (`"redacted"`; the actual hostname is deliberately omitted)
- pid: int (process ID)

## Payload rules

- Payload is event-type specific and filtered through a strict allowlist.
- Unknown fields and invalid values are dropped, including nested values.
- Raw prompts, responses, exception text, paths, environment variables, tokens,
  credentials, and user identifiers are never accepted into diagnostic payloads.
- Unfiltered conversation text belongs only in private conversation storage.

| Event | Allowed payload fields |
| --- | --- |
| `session_start` | `status`: `started` |
| `session_end` | `status`: `success` or `error`; nonnegative integer `duration_ms` |
| `user_prompt` | nonnegative integer `chars` |
| `ai_response` | nonnegative integer `chars` |
| `guardrail_block` | `code`: `cluster_scope_withheld` |
| `error` | fixed `component` and `code` values listed below |

Allowed components: `runtime`, `backend`, `plugin`, `retrieval`, `storage`.
Allowed error codes: `request_failed`, `backend_failed`, `plugin_failed`,
`retrieval_failed`, `history_unavailable`, `shared_storage_unavailable`.
Numeric metadata is capped at `2**31 - 1`. Private and shared diagnostic sinks
use the same privacy rules. The existing raw-content configuration switches
and legacy `global_log` switch are rejected when diagnostic logging is enabled.

Every invocation writes its own `inkly-<random-session-id>-<segment>.active.jsonl`.
Completed segments are renamed to `.jsonl`. Readers can identify an incomplete
last line in an active file after an I/O interruption; completed lines are
independent JSON objects. Different users/processes never append to one shared file.

See [the deployment guidance](https://github.com/thealice-lab/hpc-ink-setup/pull/5#issuecomment-6323225603) for permissions,
retention, configuration, and deployment verification.

## Filtered full-history chat snapshots

`chat-<Unix UID>-<subject-hash>.jsonl` is a separate complete conversation snapshot,
not a diagnostic stream. It uses the same v1 envelope and `user_prompt` /
`ai_response` event types. Payload fields are `turn_index: int`, `chars: int`,
`content: string`, and `redacted: bool`. `chars` counts the published content.
Each replacement has a fresh session ID; original valid UTC turn timestamps
are retained. Raw user IDs, original turn IDs and arbitrary source metadata are
not copied. Ownership, UID filenames and subject hashes are not anonymity.

Common credential markers and raw backend/plugin/retrieval errors cause whole
turns to be replaced with a fixed withheld message. This is conservative pattern
filtering, not guaranteed secret detection. Other text, including personal details
and unpublished work, is shared with the configured group as described on install.
Private originals are preserved. Diagnostic writers still reject content fields;
metadata-only readers must select `inkly-*.jsonl`, not `chat-*.jsonl` snapshots.
