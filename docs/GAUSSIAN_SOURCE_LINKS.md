# Gaussian source-link integration contract

Inkly labels every scraped Gaussian result as
`scope=external-not-verified-for-this-cluster`. Source links identify external
references; they do not establish Cuttlefish modules, partitions, access or
resource policy. Verified local facts continue to come from the reviewed
cluster profile.

The plugin retains an optional `match.url` alongside the source label and score.
Only bounded HTTP(S) URLs without credentials, whitespace, control characters
or markup delimiters are retained. Missing/invalid URLs are explicitly
`unavailable`; retrieval still works with the existing scraper. Full citation
lines fit within the context budget or are omitted, never partially truncated.
The runtime response contract asks the model to cite available labels/URLs
verbatim, and to identify unavailable links. Its deterministic response to an
unverified cluster-specific question includes the external references while
continuing to withhold other clusters' commands. Model-generated citation
completeness still needs evaluation with a configured backend.

## Nathan's remaining scraper dependency (Issue #9)

Read-only inspection at scraper revision
`8b8cb4b1402c1486bc944360e028847ec395a225` confirms `PassageMatch` contains
`domain`, `label`, `text`, `score`, `tool`, with **no `url`**. Inkly cannot
recover a reliable passage URL from that object. The audit inventory is
historical evidence, not a current passage-to-URL mapping, so Inkly does not
invent links from labels or use that CSV as a runtime lookup.

In Nathan's separate repository:

1. Retain source `url` in `storage.load_domain_records`, carry it through
   `PassageIndex` entries, and add an optional `url: str | None = None` to
   `PassageMatch` without changing ranking or breaking older callers.
2. Preserve actual per-question Stack Exchange permalinks at passage level.
   A tag-feed URL does not identify the question that supplied an answer.
3. Add storage/index/search round-trip tests; preserve labels and passage URL
   association through deduplication. Existing HTML/PDF source URLs should pass
   through unchanged. Source-wide links cannot stand in for missing question URLs.
4. Coordinate a separately reviewed scraper release/ref update and authorized
   corpus rebuild if passage-level metadata requires a schema change. This PR
   neither changes the scraper nor rebuilds any corpus.

The checked-in audit assigns upstream provenance to Nathan and Inkly-side
integration to Ryan. The private team Issue #9 and its comments/board ownership
were inaccessible to the connector and browser in this session; current team
assignment still needs verification. No overlapping source-links branch or open
PR was found in the canonical code repository at startup.

Official vendor text remains unindexed; this change grants no new indexing
permission and fabricates no vendor links.
