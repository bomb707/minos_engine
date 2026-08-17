# Validator Twin — Fixture Provenance

All Stage 1 fixtures live under `tests/fixtures/twin/` and are **small synthetic
JSON**. No BAM, VCF, reference, SDF, or truth *bytes* are committed — only
content identities (sha256 hex) and metadata. `FIXTURE_MANIFEST.json` records,
per fixture: `path`, `sha256`, `classification`, `truth_isolation`, `origin`,
`license`.

## Layout
| Dir | Purpose |
|---|---|
| `replay/` | full replay bundles (request + raw comparison + identities) |
| `raw/` | raw comparison payloads for parser-level tests |
| `parity/` | expectation / observation pairs |
| `truth/` | OFFLINE truth-fixture identities (carry a non-leak sentinel) |

## Replay bundles
`valid`, `snp_heavy`, `indel_heavy`, `high_fp`, `high_fn`, `zero_boundary`
(valid); `invalid_caller`, `unknown_parameter`, `out_of_range` (plan-build must
reject).

## Raw comparison
`valid`, `missing_indel`, `malformed_numeric`, `negative_count`,
`inconsistent_supplied` (parser must reject the malformed ones and detect the
inconsistency).

## Truth fixtures (offline only)
`truth/practice_truth.json` carries `sentinel: TRUTH_SENTINEL_DO_NOT_LEAK_...`.
A leakage test loads it via `twin.offline.truth_loader` and proves the sentinel
cannot appear in any production contract; an architecture test proves no
production package imports `twin.offline`.

## Classification
`synthetic` — generated, no real genomic data. Everything in Stage 1 is
synthetic. Externally-derived fixtures (none in Stage 1) would carry a license
note and `public`/`practice` classification.
