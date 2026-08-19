# L2-E — Production Feature View (FEATURE-VIEW-READY + FEATURE-MATRIX-FROZEN)

Frozen design (E0). Owner-approved with corrections 1–7 and the E3 grant ruling
incorporated. Any change to this document after E0 acceptance is an explicit owner
decision. `Layer2Service.select_config` remains blocked throughout L2-E.

## Immutable prerequisites (pinned in `layer2/prerequisites.py`)

| Identity | Value |
|---|---|
| PROFILE-SNAPSHOT-FROZEN-1 gate | `d48c530e97fc26e85396467afa862eba5da5359707f036a33060d6e5bff30f31` |
| Snapshot hash | `cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7` |
| Feature registry hash | `0d8612707c6673060546511d8f5e8d1ba47048ef440e6c2dcf238fdc297f6e0c` |
| Accepted profiler identity | `layer1-profiler-v1` / config `d01b8e7a…` |

## Feature inventory (authoritative; correction 7 — formal L2-A documentation erratum)

*Scope note:* E0 proves only the **derived** inventory below. The committed column
manifest itself (exactly 129 unique paths, contiguous indices 0..128, sorted-path
order, the accepted registry hash, and a stable `feature_set_hash`) is an **E1 proof
obligation** — not claimed here.

**Erratum:** the L2-A registry *documentation* states 147 ELIGIBLE fields; the
*executable registry* (`production_eligible_fields()` at registry hash `0d861270…`)
yields **141**. The executable registry is authoritative. FEATURE-READY-v1 selects the
**129 BAM-only** subset:

| Set | Count |
|---|---|
| ELIGIBLE total (executable registry) | **141** |
| Selected: `bam-profile-v1` scalars (FEATURE-READY-v1) | **129** (84 REAL + 45 FRACTION, 0 COUNT) |
| Excluded: `window-profile-v1` fields | **12** (require a later versioned window-aggregation contract + gate) |

The committed **column manifest** freezes, per column: `index` (0-based, order = sorted
field paths), `path`, `source_schema` (= `bam-profile-v1`), `state` (= `ELIGIBLE`),
`value_kind`, plus the manifest-level `registry_hash`. A derivation test recomputes the
manifest from the registry function and fails on any drift.

**Correction 6 (feature_values_hash domain):** the L2-D
`extract_eligible_feature_values()` already filters to `source_schema ==
"bam-profile-v1"`, so the snapshot `feature_values_hash` covers **exactly the selected
129 paths**. It is therefore retained as the source anchor; no separate
`selected_feature_values_hash` is required. This equivalence is proven by a permanent
test (`test_feature_values_hash_domain_is_selected_set`) and re-proven by the gate.

## Materialization (correction 1 — snapshot-derived, no fixed counts)

Materialized rows derive from the frozen snapshot membership, never from constants:
**train rows = frozen snapshot train members; validation rows = frozen snapshot
validation members; test materialization = zero, always.** **No test matrix, test row,
or test payload exists anywhere** — the matrices table structurally permits only
`('train','validation')`, so test membership is rejected before it could be read.
Epoch-1's 50/10/15 may be identified only as *historical derived evidence* in generated
reports — it is never an L2-E invariant.

## Hash contracts (correction 5; all domain-separated, canonical JSON)

```
feature_set_hash = SHA256("minos:feature-set:v1\n" + canonical_json({
  schema_version: "feature-set-v1", registry_hash, column_count: 129,
  columns: [{index, path, source_schema, state, value_kind}] (index order) }))

vector_hash = SHA256("minos:feature-vector:v1\n" + canonical_json({
  schema_version: "feature-vector-v1", epoch, dataset_id, profile_id, content_hash,
  feature_values_hash, partition, snapshot_hash, registry_hash, feature_set_hash,
  value_count: 129, values: [ordered per column manifest] }))

matrix_hash = SHA256("minos:feature-matrix:v1\n" + canonical_json({
  schema_version: "feature-matrix-v1", epoch, snapshot_hash, partition,
  registry_hash, feature_set_hash, row_count, column_count: 129,
  members: [{dataset_id, vector_hash}] ordered by dataset_id }))
```

`matrix_hash` is the **logical** identity; `artifact_sha256` (exact Parquet bytes) is
recorded separately and never conflated (correction 4). Reordering columns or members,
or changing any value, epoch, count, or bound identity changes the respective hash.

## Canonical Parquet serialization (correction 4; frozen)

* Schema: `dataset_id: string` + 129 `float64` columns named by field path, in column-
  manifest index order. FRACTION values additionally validated ∈ [0,1] before write.
* Rows ordered by `dataset_id` ascending; row count == partition membership.
* **Null policy: nulls are forbidden** — any missing/null value fails closed before
  serialization (columns are non-nullable in the Arrow schema).
* Codec: `compression=NONE`; Parquet format version pinned; statistics disabled;
  no timestamps or environment-dependent metadata; the only schema metadata keys are
  `{"schema_version": "feature-matrix-parquet-v1", "feature_set_hash": …}`.
* Determinism rule: byte-identical output for identical logical content on the pinned
  writer version; the payload verifier re-serializes and compares hashes.

## Verification levels (correction 2 — three, named honestly)

| Level | Needs | Proves |
|---|---|---|
| **manifest verifier** | Git checkout only | gate contract, metadata/membership manifests, hash bindings, counts, registry/set identity. *Never described as full matrix verification.* |
| **payload verifier** | + content-addressed Parquet bytes (no PG) | recomputed vectors from payload, vector/matrix hash recomputation, artifact_sha256 equality, canonical-serialization determinism |
| **operational verifier** | + PostgreSQL store | DB rows/uniques/append-only, grants and role denials, operational artifact bytes vs committed hashes |

## Storage schema (migration `0005_l2e_feature_view`, single head on 0004)

* `profiling.feature_sets` — `feature_set_hash` UNIQUE, `registry_hash`,
  `column_count`, `column_manifest` JSONB; append-only.
* `profiling.feature_matrices` — FK → `profile_snapshots`, `partition` **CHECK IN
  ('train','validation')**, FK → `feature_sets`, `matrix_hash` UNIQUE,
  `artifact_sha256`, `matrix_artifact_id` FK → `catalog.artifacts`
  (kind `l2e:feature-matrix-parquet`), `row_count`, `column_count`,
  **UNIQUE(snapshot, partition, feature_set)** = logical identity; same identity with a
  different `matrix_hash` → typed `MatrixConflictError`; equal hash → idempotent return.
  Append-only.
* `profiling.feature_matrix_members` — FK matrix / dataset_registry, `member_index`,
  `vector_hash`, `feature_values_hash`; UNIQUE(matrix, dataset), UNIQUE(matrix, index);
  hashes only, never plaintext values. Append-only.
* Views: `profiling.training_matrix` → `minos_trainer` only;
  `evaluation.validation_matrix` → `minos_evaluator` only. Base tables: no app grants.

## Grant + artifact-boundary model (E3 ruling)

* Migration 0005 **revokes unrestricted `catalog.artifacts` SELECT from
  `minos_trainer` and `minos_evaluator`**; the exact prior grants are restored on
  downgrade. `minos_live`/`minos_runner` legacy grants are untouched.
* Artifact references are reachable only through the caller's partition view: trainer
  can resolve only the train matrix artifact; evaluator only the validation artifact.
* **Retrieval boundary (not URI hiding):** matrix artifacts are stored under
  partition-scoped locations (`l2e/train/…`, `l2e/validation/…`) whose retrieval
  credentials are role-separated. In the local pgserver/filesystem deployment this is
  enforced with per-partition directory ownership/permissions; any remote object store
  deployment MUST map the same boundary onto per-role credentials. This deployment
  invariant is asserted by the operational verifier.
* **Trainer runtime isolation (correction 3):** the guarantee is that the trainer
  runtime image/checkout contains **no validation values, no validation payload, no
  retrievable validation URI, and no credential able to retrieve it**. Committed
  evidence *hashes* are allowed to be visible. Proven by: grant tests per role, a
  simulated trainer checkout/image assembly test asserting the absence of validation
  payload/URI/credential material, and the operational credential-boundary check.

## Gates

* **FEATURE-VIEW-READY** (capability, corpus-independent): contracts, migration
  lifecycle 0005↔0004, verifiers, grant/credential boundary, structural test-partition
  rejection; binds the pinned snapshot prerequisites. Two-commit S/E closure, full
  S2-pattern gate-contract verification, CI manifest-verifier step, tamper matrix
  (column/member reordering, value mutation, wrong set/registry hash, forged manifests,
  type-boundary violations, conflicting-idempotency, access denial).
* **FEATURE-MATRIX-FROZEN-1** (epoch evidence): the built epoch-1 train +
  validation matrices (row counts = their frozen snapshot partition memberships) —
  committed metadata/hash manifests (no plaintext vectors),
  content-addressed artifacts inventoried by hash, payload + operational verification,
  own S/E closure.

## Stage sequence

E0 (this document + pins + inventory test) → E1 contracts → E2 extraction/verifiers →
E3 migration + storage + grant boundary → E4 epoch-1 build + evidence →
E5 gates/closures. Each step starts only on explicit owner authorization.

## Count policy (frozen — owner clarification)

L2-E **never applies split percentages**. L2-C owns percentage-based allocation (as the
exact rational basis **10:2:3 over 15** — never floating-point constants); L2-D freezes
the exact resulting membership; L2-E consumes the frozen snapshot membership **verbatim**
(no reassignment, no re-rounding, grandfathered allocations consumed unchanged).

Derived expectations (computed from the frozen snapshot, never hardcoded):

* `expected_train_count`      = |frozen snapshot train members|
* `expected_validation_count` = |frozen snapshot validation members|
* `expected_test_count`       = |frozen snapshot test members|
* `materialized_test_matrix_count` **== 0 always**

For epoch 1 the values 50/10/15 may appear only as *derived evidence* in generated
reports. No L2-E schema, qualifier, builder, required-check name, or test fixture may
require those constants.

Mandatory check names (generic; never partition-count-specific):

* `train_matrix_count_matches_snapshot`
* `validation_matrix_count_matches_snapshot`
* `matrix_membership_matches_snapshot`
* `sealed_test_matrix_absent`

Required tests: at least **two non-75 synthetic snapshots with uneven chromosome sizes**
proving (1) matrix counts derive from actual frozen membership; (2) no fixed 50/10/15
assumption remains; (3) train and validation matrices exactly cover their snapshot
partitions; (4) grandfathered allocations are consumed unchanged; (5) no test matrix is
created.
