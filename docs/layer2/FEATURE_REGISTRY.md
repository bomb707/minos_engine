# Layer 2 — Feature-Eligibility Registry (L2-A, remediated)

`layer2/feature_registry.py` is the code-owned, deterministic, canonically hashable
classification of every Layer 1 field the controller may (or may not) consume. It
performs classification only — no feature extraction, no file access, no Layer 1
profiler imports.

## Scalar exhaustiveness (remediation)
Every Layer 1 v2 analytical field maps to exactly one registry record. Config-bound
dynamic maps are expanded to **concrete scalar leaves** — e.g.
`mapping_quality.quantiles.P50`, `coverage.fragment_primary.depth_quantiles.P95`,
`variant_evidence.support_threshold_site_counts.support_ge_2` — so production
vectors never carry a structured container. The legal keys for each dynamic map are
derived from the accepted Layer 1 configuration (`configs/layer1/default.yaml`) and
bound to the accepted profiler-config hash; unknown keys (e.g. `quantiles.P42`) are
rejected. Data-dependent maps whose key set varies by dataset
(`reference_context.homopolymer_length_histogram`, `spatial.stratum_window_counts`)
remain documentation containers (`model_feature = False`) and are never scalar
features. Window-profile scalar columns live in the `window.*` namespace.

The reconciliation artifact `reports/LAYER2_FEATURE_REGISTRY_RECONCILIATION.json`
(schema `layer2-feature-reconciliation-v1`) records: missing = 0, duplicate = 0,
unclassified = 0, unknown = 0, non-scalar-model-feature = 0.

## Record fields
`field_path, state, family, value_kind, model_feature, source_schema,
source_schema_hash, config_bound, truth_derived`. `value_kind ∈
{FRACTION, REAL, COUNT, BOOL, CATEGORICAL, IDENTIFIER, OPERATIONAL, CONTAINER}`.
`model_feature` is True only for a scalar numeric leaf (FRACTION/REAL/COUNT) in an
ELIGIBLE/CONDITIONAL/RESEARCH_ONLY state — a container is never a model feature.

## States and counts
| State | Meaning | Count |
|---|---|---:|
| `ELIGIBLE` | production feature (leakage-reviewed, truth-free scalar) | 147 |
| `CONDITIONAL` | magnitude/count/derived; needs a future accepted promotion | 60 |
| `RESEARCH_ONLY` | offline research only (SNP density, both namespaces) | 2 |
| `FORBIDDEN` | identity/coordinate/label/operational/external | 76 |
| **Total records** | | **285** |

Scalar model features: 198 (bam-profile 184 + window 14). Containers: 21.
`variant_evidence.candidate_snp_density_per_base` and
`window.candidate_snp_density_per_base` are RESEARCH_ONLY.

## Production API (ELIGIBLE-only; no caller promotions)
```python
from minos_engine.layer2 import feature_registry as FR

FR.assert_production_feature_vector(fields)  # ELIGIBLE scalar leaves only
FR.validate_production_feature_mapping(mapping)  # -> CanonicalFeatureVector
FR.production_eligible_fields()  # the ELIGIBLE allowlist
FR.registry_hash()  # stable canonical identity
```
`assert_production_feature_vector` has **no promotion parameter**. CONDITIONAL,
RESEARCH_ONLY, FORBIDDEN, containers, unknown paths, and duplicates are always
rejected. `validate_production_feature_mapping` additionally validates each value
per `value_kind` (via `validate_scalar_value`) and returns a frozen, deterministic,
canonically-hashable `CanonicalFeatureVector`.

Per-value policy (`validate_scalar_value`): **COUNT** — built-in `int`, not `bool`,
`0 <= v <= 2**53` (exactly representable); floats (even `1.0`), strings, `Decimal`,
NumPy scalars, and `None` are rejected. **REAL/FRACTION** — built-in `int` or
`float` (never `bool`), finite (no NaN/Infinity), integers within `±2**53`;
FRACTION additionally in `[0.0, 1.0]`. `CanonicalFeatureVector` direct construction
independently rejects bool/NaN/Infinity/non-numeric values, unsorted or duplicate
fields, length mismatches, malformed hashes, and an incorrect supplied vector hash.

## Promotion security
There are **no accepted promotions in L2-A**. A caller-constructed
`PromotionRecord` authorizes nothing (it is a descriptive future contract; the
production API cannot receive it). A real promotion is a future stage requiring a
repository-owned, hash-bound, git-bound accepted promotion artifact binding: field
path, previous/new states, registry hash, split-manifest hash, training and
validation evidence hashes, qualification report, accepted commit/tree, and explicit
owner authorization. Test-set evidence may never authorize a promotion
(`PromotionRecord` rejects a `TEST` evidence partition). The seven-step promotion
protocol is in `reports/LAYER2_DATASET_SPLIT_POLICY.md`.

## Hash binding
`REGISTRY_HASH` binds every scalar path, state, family, value kind, model-feature
flag, config-bound flag, truth-derived status, and source-schema identity, plus the
accepted profiler-config hash and Layer 1 schema hash. Changing any of these changes
the hash.
