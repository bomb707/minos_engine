# L2-F — Offline Experiment Harness

The L2-F harness performs **offline** deterministic GATK CONFIG candidate generation and
experiment planning over the frozen L2-E train membership. It does **not** score, rank,
select, optimize, train, or activate `Layer2Service.select_config`. `HARNESS-READY` (F7)
is not implemented and is **not** claimed anywhere below.

## Scope of this document (F3-A)

F3-A freezes the **database foundation** — migration `0006_l2f_experiment_plan`, its
frozen migration contract, the private SQLAlchemy Core table mappings, and the seeded
direct-SQL constraint attack matrix + populated lifecycle (F3-A4). The pure
`ExperimentPlan` builder (F3-B), persistence + bounded enqueue (F3-C), the Python plan
verifier and its logical attack matrix (F3-D), and F4–F7 are unimplemented and unauthorized
here.

### Private Core table mappings (not `Base.metadata`)

`storage/l2f_tables.py` defines the five owned tables as SQLAlchemy Core `Table` objects
on a **dedicated private `l2f_metadata`** — deliberately **not** the L2-B declarative
`Base.metadata` (adding them there would silently change the accepted DB-READY storage
fingerprint). Migration `0006` remains the authoritative DDL; the mappings never call
`create_all`/`drop_all`. External L2-D/L2-E tables referenced by composite FKs are minimal
`l2f_external_target_stub` declarations (FK-resolution only, never owned/created).
`L2F_OWNED_TABLES` (5) and `L2F_EXTERNAL_TARGET_STUBS` (6) are exported. Tests prove
importing `l2f_tables` leaves `Base.metadata`, `storage_schema_hash()`
(`4508728723…`), and `role_policy_hash()` (`1dfe6e56…`) unchanged. A scratch-PG parity test
(`test_l2f_tables_parity`) checks a **precisely scoped** subset — for each owned table it
compares column names + nullability, PK columns, FK names + local/referred columns, UNIQUE
names + columns, CHECK constraint **names**, and explicit index names + columns. It does
**not** yet compare SQL types, server defaults, CHECK **expressions**, PK/FK constraint
options, triggers, ownership or grants (those are the exhaustive introspection contract,
deferred below), so it is **not** a full 1:1 schema proof — but it fails if the migration and
mapping diverge on any dimension it does compare.

The seeded direct-SQL constraint behaviour is proven separately (F3-A4): a deterministic
valid graph (`l2f_seed`) drives a 49-case attack matrix (`test_l2f_attack_matrix`,
`l2f_attacks`) that reaches each FK/UNIQUE/CHECK by name and each immutability trigger by its
stable SQLSTATE/message, plus positive controls (permitted job status/claim updates, valid
inserts) and a populated `0005↔0006` lifecycle (`test_l2f_populated_lifecycle`).

> **F3-A still open:** the **exhaustive** static-inventory + live-schema introspection
> contract (SQL types, server defaults, CHECK expressions, PK/FK constraint options, index
> definitions, triggers, ownership, grants), the frozen `0006` byte SHA + final F3-A contract
> hash, and the final documentation closure are the remaining corrective F3-A items and are
> **not** yet committed. The seeded attack matrix and populated lifecycle above **are** now
> committed.

## Why legacy tables are forbidden

- `experiments.jobs.profile_id` foreign-keys to legacy **`profiling.profiles`**, a
  compatibility-only table that is **not** the accepted L2-D profile store. No accepted
  L2-F row may reference it.
- The authoritative accepted profiles live in **`profiling.bam_profiles`**, selected
  through the frozen `profile_snapshots` → `profile_snapshot_members` → `feature_matrices`
  → `feature_matrix_members` lineage.
- `catalog.gatk_configs` stores only `config_hash` + `parameter_space_hash` — it does not
  durably bind the canonical CONFIG payload F5 needs. L2-F therefore introduces its own
  content-addressed payload binding.

Legacy `profiling.profiles`, `experiments.jobs`, and `experiments.results` are left
byte-identical; migrations 0001–0005 are unchanged (their byte SHAs are pinned in
`storage/l2f_migration_contract.py`).

## Five-table normalized design (`0006`, additive)

1. `experiments.l2f_experiment_plans` — immutable plan identity: `profile_snapshot_id`,
   `train_feature_matrix_id`, `partition='train'`, `feature_set_id`, all accepted identity
   hashes, derived `train_member_count`/`candidate_count`/`logical_job_count`, `plan_hash`.
2. `experiments.l2f_experiment_plan_members` — the ordered complete train-member inventory.
3. `experiments.l2f_config_payloads` — durable `config_hash` → canonical CONFIG payload
   binding (content-addressed via `catalog.artifacts`).
4. `experiments.l2f_experiment_plan_configs` — the ordered complete candidate inventory of
   one plan (proves a job's `config_index` belongs to the plan's candidate set).
5. `experiments.l2f_experiment_jobs` — logical jobs; scientific identity immutable,
   status/claim metadata mutable (F4 owns claiming — no claim code or grants here).

## Relational train-only proof (declarative, not a caller partition flag)

Because 0001–0005 may not be edited, `0006` **additively** adds reversible composite
UNIQUE constraints to the immutable target tables (`feature_matrices`,
`profile_snapshot_members`, `feature_matrix_members`, `catalog.artifacts`) so they can be
composite-FK targets. Every cross-table invariant is then enforced by a **composite
foreign key**, not a trigger and not a caller-supplied `partition` value:

- **plan → snapshot identity**: FK `(profile_snapshot_id, snapshot_hash,
  split_manifest_hash, registry_snapshot_hash)` → `profile_snapshots(id, snapshot_hash,
  split_manifest_hash, registry_snapshot_hash)` — a valid snapshot id with any **forged**
  snapshot/split/registry hash is rejected declaratively.
- **plan → feature-set identity**: FK `(feature_set_id, feature_set_hash,
  feature_registry_hash)` → `feature_sets(id, feature_set_hash, registry_hash)` — binds the
  set hash **and** the L2-A feature `registry_hash` (distinct from the GATK
  parameter-registry hash `gatk_registry_hash`).
- **plan → train matrix**: FK `(train_feature_matrix_id, profile_snapshot_id, partition,
  train_matrix_hash, feature_set_id)` → `feature_matrices(id, profile_snapshot_id,
  partition, matrix_hash, feature_set_id)` with `CHECK(partition='train')` — the matrix
  must belong to the plan's snapshot, be partition `train`, and carry the recorded hashes.
- **plan_config → parameter space**: FK `(plan_id, parameter_space_hash)` →
  `l2f_experiment_plans(id, parameter_space_hash)` **and** `(config_payload_id, config_hash,
  parameter_space_hash)` → `l2f_config_payloads(id, config_hash, parameter_space_hash)` — a
  config payload from a **different** parameter space cannot be linked into the plan.
- **config payload → artifact schema**: FK `(artifact_id, config_hash, media_type)` →
  `catalog.artifacts(id, sha256, media_type)` plus `CHECK` fixing `schema_version =
  l2f-config-payload-v1` and `media_type = application/vnd.minos.l2f-config+json` — proves
  the artifact is the canonical L2-F CONFIG payload (bytes = canonical JSON of
  `effective_config`, `sha256 == config_hash`).
- **plan member → plan**: FK `(plan_id, profile_snapshot_id, feature_matrix_id)` →
  `l2f_experiment_plans(id, profile_snapshot_id, train_feature_matrix_id)`.
- **plan member → snapshot member**: FK on `(profile_snapshot_member_id, profile_snapshot_id,
  dataset_registry_id, bam_profile_id, partition, feature_values_hash)` with
  `CHECK(partition='train')` — proves it is that snapshot's **train** member, of that
  dataset, selecting that exact `bam_profile`, with that `feature_values_hash`.
- **plan member → matrix member**: FK on `(feature_matrix_member_id, feature_matrix_id,
  dataset_registry_id, member_index, feature_values_hash)`. Because `dataset_registry_id`
  and `feature_values_hash` are **shared columns** across the snapshot-member and
  matrix-member FKs, the two lineages are forced to agree (same dataset, matching
  feature-values), and `member_index` is the frozen matrix index.
- **plan config → payload**: FK `(config_payload_id, config_hash, parameter_space_hash)` →
  `l2f_config_payloads(id, config_hash, parameter_space_hash)` (so a plan-config cannot forge
  a config_hash or bind a payload from a different parameter space); and the payload's own FK
  `(artifact_id, config_hash, media_type)` → `catalog.artifacts(id, sha256, media_type)`
  proves `artifact.sha256 == config_hash` at the canonical CONFIG media type (exact-byte
  payload binding for F5 reconstruction).
- **job → member/config of same plan**: FKs `(plan_member_id, plan_id)` and
  `(plan_config_id, plan_id)` → the plan-scoped composite targets, so a job can never mix
  rows from different plans.

Direct SQL cannot insert a validation/test member, a member from another snapshot/matrix,
a mismatched dataset/feature-values/index, a substituted `bam_profile`, or a cross-plan
job — the composite FKs reject it at the database boundary.

## Config-payload reconstruction contract

The canonical CONFIG payload is the **canonical JSON bytes of `effective_config`** stored
as a content-addressed `catalog.artifacts` row whose `sha256 == config_hash`. F5 rebuilds
the exact CONFIG byte-for-byte from that artifact; a hash without a reconstructable
payload is forbidden. (Publication code is F3-C, not F3-A.)

## Identity binding levels (honest)

Not every scientific identity has a persisted upstream row to foreign-key against. Each
is one of:

- **Database-bound** (a composite FK independently proves it against an immutable upstream
  row): `snapshot_hash`, `split_manifest_hash`, `registry_snapshot_hash` (→
  `profile_snapshots`); `feature_set_hash`, `feature_registry_hash` (→ `feature_sets`);
  `train_matrix_hash` + `partition='train'` + `feature_set_id` (→ `feature_matrices`); the
  full member lineage (→ `profile_snapshot_members` + `feature_matrix_members`);
  `config_hash` + `parameter_space_hash` + media type (→ `l2f_config_payloads` →
  `catalog.artifacts`); and every job's member/config belong to its plan.
- **Structurally constrained, verified downstream**: `train_feature_view_hash`,
  `gatk_registry_hash`, `experiment_parameter_policy_hash`, `candidate_set_hash` — these
  have **no** upstream persisted row to FK against, so PostgreSQL only enforces hex64 shape
  + membership in the complete logical-identity UNIQUE. Their correctness is established by
  the future **F3-B accepted constructor** (which derives them from repository-owned
  contracts) and re-checked by the future **F3-D verifier**. F3-A does **not** claim
  PostgreSQL independently proves these.
- **Derived + re-verified**: `train_member_count`, `candidate_count`, `logical_job_count`
  (CHECK `logical_job_count = train_member_count * candidate_count`; the member/candidate
  values themselves are re-derived by F3-B/F3-D). Counts are intentionally excluded from
  the logical-identity UNIQUE.

## Immutability, grants, lifecycle

- Plans, members, config payloads, and plan-configs are fully append-only (reusing
  `audit.minos_reject_mutation`). Jobs are identity-immutable with mutable status/claim
  (trigger `minos_l2f_reject_job_identity_change`).
- All five tables and functions are owned by `minos_admin`; **no** grants to
  `minos_live`/`minos_runner`/`minos_trainer`/`minos_evaluator` in F3-A (F4 owns claim
  authority). Downgrade restores exactly the 0005 schema, grants, and composite targets.
- Alembic head advances to `0006_l2f_experiment_plan` (down_revision `0005`); single head;
  `0005↔0006` lifecycle is CI-verified on scratch PostgreSQL only. `0006` is **never**
  applied to the operational `minos_engine_db` in this source step.

## Not in F3-A / unauthorized

`ExperimentPlan` builder, `plan_hash`/`job_key` formulas (F3-B), persistence, bounded
enqueue, and the verifier (F3-C/D); `claim_next_job`, execution, results, scoring (F4+);
`HARNESS-READY` (F7); `select_config` (blocked). No operational data is created.
