# Gaussian Documentation Source Audit

Issue: thealice-lab.github.io#8. Audited 2026-11-10 by Nathan Todd.

This covers what the Gaussian scraper (`gaussian-docs-scraper`) gives Inkly, how far the sources can be trusted, and what is missing. The full per-source list is in [`gaussian_source_inventory.csv`](gaussian_source_inventory.csv).

## What was checked

- Scraper config: `configs/gaussian.toml` at scraper commit `7a8a43b`.
- Database: a `gaussian.db` snapshot built 2026-11-01 by the scraper's daily run (100 sources, 1,903 passages). This is a local build, not the shared Cuttlefish copy, which does not exist yet.
- Scrape log for that run, to explain every source that is configured but not stored.
- Inkly side: `inkly/plugins/docs_gaussian.py` and `inkly/cluster_profiles/cuttlefish.toml` on `dev` (`bf7d02e`).
- 15 sample questions run through `search_docs("gaussian", ...)`, the same call the plugin makes.

## Inventory summary

| Category | Configured | Stored | Notes |
|---|---|---|---|
| HPC center software pages | 53 | 50 | 3 return HTTP 404 (Flinders, Minnesota MSI, Purdue Bell) |
| Course and tutorial material (PDF/HTML) | 40 | 37 | 3 stored with 1-2 passages only |
| Stack Exchange Q&A (tag feeds) | 11 | 11 | 1 stored with a single passage |
| Related program docs (NBO install notes) | 2 | 2 | |
| **Total** | **106** | **100** | |

The 6 configured sources that are not stored:

| Source | Reason |
|---|---|
| Flinders DeepThought, Minnesota MSI, Purdue RCAC (Bell) | Page moved (HTTP 404) |
| McGill Barrett Group G09W tutorial (PDF) | Network error on fetch |
| SHARCNET Gaussian16 NBO7 slides (PDF) | Downloaded but no extractable text |
| Illinois Pogorelov Lab tutorial part 2 (PDF) | Fetched (20 passages) but absent from the DB; most likely every passage was removed by cross-source deduplication against part 1 |

Every source is **external**. None of them describe Cuttlefish.

## Official Gaussian vendor documentation

**Not indexed.** No source in the config or database comes from `gaussian.com`.

Evidence on availability and restrictions (checked 2026-11-10):

- The Gaussian 16 user reference is public on `gaussian.com` (its sitemap lists about 350 pages, including the keyword pages `opt`, `freq`, `scrf`, `td`, `link0`, `iops`, `basissets`, `running` and the FAQ).
- `robots.txt` only blocks `/wp-admin/` and form uploads, so crawling the manual pages is not disallowed.
- The site's only terms page (`/terms-and-conditions`, last updated 10 January 2017) covers ordering and payment. It says nothing about copying, caching, indexing or automated access.
- No explicit reuse license was found, so the text should be treated as copyrighted by Gaussian, Inc. with no stated permission to store or redistribute it.

**Recommendation:** do not add vendor pages until Ryan (or the PI) decides. Options, from least to most risk:

1. **Link only.** Map common keywords to their `gaussian.com/<keyword>/` URL and have Inkly point users there, storing no vendor text.
2. **Ask Gaussian, Inc.** whether storing manual pages in an internal, lab-only index is acceptable under the lab's site license.
3. **Index a small keyword set** for internal use with source links, only if 2 comes back yes.

## Trusted vs. external material

| Kind | Where it lives | How Inkly treats it |
|---|---|---|
| Cuttlefish rules (module, partition, scratch, access group, CPU and memory policy) | `inkly/cluster_profiles/cuttlefish.toml`, verified 2026-09-28/29 | Trusted. Used by the SBATCH generator. |
| General Gaussian behavior (keywords, methods, errors, basis sets) | Course material, tutorials, Stack Exchange, parts of HPC pages | External reference, labeled `scope=external-not-verified-for-this-cluster` |
| Other clusters' commands and policies | The 53 HPC center pages | External. For Cuttlefish-specific questions the plugin withholds their text and only lists the source names. |

The scraper should stay external-only. Cuttlefish facts belong in the cluster profile, where they are verified and reviewed, not in scraped data.

## Metadata and provenance

| Field | State |
|---|---|
| `label` | Present and unique for all 100 sources |
| `url` | Present for all 89 HTML sources; **empty for the 11 Stack Exchange sources** (only `site` and `tag` are stored, not per-question links) |
| `last_scraped_at` | Present (all 2026-11-01). This is the scrape time, not the page's own last-updated date, which is not captured. |
| `content_hash` | Present; no two sources share one |
| `summary` | Empty for all sources (the run used `--skip-summary`) |
| `tool` | Empty, as expected for the Gaussian domain |

**Provenance gap:** `PassageMatch` only carries `label`, `text`, `score` and `tool`, so Inkly can name a source but cannot give the user a link. Issue #9 (source-backed answers) will need the URL passed through `search_docs`.

## Sample question results

| Question | Result |
|---|---|
| `%mem` vs. Slurm `--mem` | Good: QMUL, NeSI and NIH answers |
| `%NProcShared` under Slurm | Partial: CSC answer at rank 3 |
| Module command for Gaussian 16 | Good but external (correctly not Cuttlefish) |
| `GAUSS_SCRDIR` on a cluster | Partial: UCL answer at rank 3 |
| Restart an optimization from a checkpoint | Good: GWDG |
| Opt then Freq | Partial: tutorial step at rank 3 |
| Transition metal basis sets | Good: Lakehead lab manual |
| Link 9999 error termination | **Miss** |
| `Opt=CalcFC` vs. `Opt=ReadFC` | **Miss** |
| SCRF solvent keyword | Weak |
| SCF convergence failure | **Miss** |
| TD-DFT excited states | **Miss** |
| `formchk` usage | **Miss** |
| Gaussian license on Cuttlefish | External only (expected; answered by the profile) |
| Cuttlefish partition | External only (expected; answered by the profile) |

Two patterns stand out:

- **Keyword and error questions are the weak spot.** These are exactly what the vendor manual covers, which is the strongest argument for resolving the vendor question.
- **Heading-only passages crowd out answers.** The median passage is 88 characters. A 75-character NDSU FAQ heading ("I would like to use a mixed basis set...") with no answer text ranked first for 7 of the 15 questions. Short passages score high under TF-IDF, so headings should be merged with the text that follows them.

## Stale, duplicated or conflicting material

- **Version drift:** 32 sources mention Gaussian 09; Cuttlefish runs G16 Rev. C.02. G09-only sources (Utah CHPC G09, NCI Gadi G09, both NBO G09 install notes) should be tagged or dropped.
- **Duplicates:** no duplicate pages. Deduplication removed 125 repeated passages in the last run.
- **Conflicting guidance:** `%mem` advice differs across centers (e.g. "1-4G less than the request" vs. setting it to the full amount). The generator's 0.75 rule is the trusted one for Cuttlefish; the external advice should stay labeled as other clusters' policy, as it is now.
- **Near-empty sources:** Illinois SCS GaussView PDF, Valencia PDF, Harvard Chem106 PDF and the SciComp `hpc` tag each give 1-2 passages and should be checked or removed.
- **Off-topic:** a few non-English course PDFs (Spanish, French, German) are low value for English questions.

## Recommended actions and owners

| # | Action | Owner |
|---|---|---|
| 1 | Decide the vendor documentation option above | Ryan / PI |
| 2 | Merge heading-only passages with the following text in the scraper | Nathan (scraper) |
| 3 | Fix or remove the 3 HTTP 404 sources and the 4 near-empty sources | Nathan (scraper) |
| 4 | Tag or drop G09-only sources | Nathan (scraper) |
| 5 | Store per-question URLs for Stack Exchange sources | Nathan (scraper) |
| 6 | Pass `url` through `PassageMatch` and `search_docs` for source links (#9) | Nathan (scraper) + Ryan (plugin) |
| 7 | Keep Cuttlefish facts in `cuttlefish.toml`, not in scraped data | Ryan |
| 8 | Rebuild the full `gaussian.db` after 2-5, through a Slurm job, into the shared location | Nathan, once the shared directory exists |

No new sources are recommended until 1 is decided; the gaps found are keyword and error coverage, which the vendor manual would fill better than more HPC center pages.
