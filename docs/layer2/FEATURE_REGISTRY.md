# Layer 2 — Feature-Eligibility Registry (L2-A)

`layer2/feature_registry.py` is the code-owned, deterministic, canonically hashable
classification of every Layer 1 field the controller may (or may not) consume. It
performs classification only — no feature extraction, no file access, no Layer 1
profiler imports.

## States
| State | Meaning |
|---|---|
| `ELIGIBLE` | Normalized, truth-free measurement usable as a production feature (leakage-reviewed). |
| `CONDITIONAL` | Descriptive/magnitude field; production use requires an explicit owner-authorized promotion. |
| `RESEARCH_ONLY` | Offline research only; production entry impossible without the seven-step promotion. |
| `FORBIDDEN` | Identity/coordinate/label/structural, or external truth/score — never a model feature. |

## Coverage and counts
The registry is exhaustive over the `BamProfile` analytical field tree (184 fields)
plus 12 external FORBIDDEN sentinels (truth VCF, mutations, hap.py results, TP/FP/FN,
hidden labels, live score, operator ranking/identity, previous winning CONFIG,
dataset/round IDs, evaluation credentials). Current classification:

| State | Count |
|---|---:|
| ELIGIBLE | 80 |
| CONDITIONAL | 56 |
| RESEARCH_ONLY | 1 |
| FORBIDDEN | 59 |
| **Total** | **196** |

`variant_evidence.candidate_snp_density_per_base` is **RESEARCH_ONLY** (owner
exclusion). Coordinates, contig, identities, hashes, dataset/round IDs, truth,
mutations, scores, operator identity, and the previous winning CONFIG are FORBIDDEN.

## Guarantees (enforced by tests)
- Deterministic ordering (sorted by field path) and a stable `REGISTRY_HASH`.
- Duplicate field paths are rejected at build time.
- Unknown field paths are rejected by `state_for` and by production selection.
- FORBIDDEN and RESEARCH_ONLY fields can never enter a production feature vector.
- CONDITIONAL fields require a matching `PromotionRecord`.
- A promotion can never be justified by test-set results — `PromotionRecord` rejects
  a `TEST` evidence partition, so test-set outcomes cannot decide feature retention.
- No ELIGIBLE/CONDITIONAL/RESEARCH_ONLY record may be `truth_derived`.

## Production selection API
```python
from minos_engine.layer2 import feature_registry as FR

FR.assert_production_feature_vector(fields, promotions)  # raises on any illegal field
FR.production_eligible_fields()  # the ELIGIBLE allowlist
FR.registry_hash()  # stable canonical identity
```

The seven-step promotion protocol for RESEARCH_ONLY/CONDITIONAL fields is defined in
`reports/LAYER2_DATASET_SPLIT_POLICY.md`.
