# L2-F — Offline Experiment Harness

The L2-F harness performs **offline** deterministic GATK CONFIG candidate generation and
experiment planning over the frozen L2-E train membership. It does **not** score, rank,
select, optimize, train, or activate `Layer2Service.select_config`. `HARNESS-READY` (F7)
is not implemented and is **not** claimed anywhere below.

## Scope (F3-A accepted; F3-B pure plan contract)

**F3-A is accepted** at commit `ff8daa4bf9bd9891e46017e3f7bcb4ea9a8edb6d`. The accepted
identities are the migration `0006_l2f_experiment_plan` byte SHA-256
`1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6` and the F3-A contract hash
`c7a2e978857830ccff67821ded1196472d5f38baacb19a64352ec686ce74916b` (over the exhaustive static
inventory with raw + effective ACLs). F3-A comprises migration `0006`, the private SQLAlchemy
Core mappings, the frozen static inventory proven equal to the live schema, the 47-case
direct-SQL attack matrix, and the all-MINOS-schema populated `0005↔0006` lifecycle.

**F3-B (this commit) adds the pure, deterministic `ExperimentPlan` contract + accepted
no-override constructor.** It performs **no** PostgreSQL writes, artifact publication, job
enqueueing, claiming, execution, scoring, training, optimization, or configuration selection —
plan verification only reads committed files + git history. Persistence + bounded enqueue
(F3-C), the Python plan verifier + logical attack matrix (F3-D), and F4–F7 remain
**unimplemented and unauthorized**. `HARNESS-READY` (F7) is **not** issued, and
`Layer2Service.select_config` remains blocked.

### F3-B pure ExperimentPlan contract

`experiments/plan.py` defines immutable, strictly-validated (`extra=forbid`, pydantic strict)
`ExperimentPlanMember` / `ExperimentPlanConfig` / `ExperimentPlan` contracts (partition
structurally fixed to `train`, all hash fields lowercase 64-hex, self-binding `plan_hash`),
the two frozen domain-separated hash formulas, and a pure in-memory logical-job enumerator.
`experiments/accepted_plan.py` exposes the sole production boundary
`build_accepted_experiment_plan()` (no arguments): it verifies the pinned E5 gate closure
(exact gate hashes + PASS + qualified source commit/tree + the `E4-evidence → E5-source →
E5-evidence → HEAD` ancestry chain + the accepted train feature-view hash — a locally
regenerated PASS gate can never authorize it), consumes the committed epoch-1 member manifest
and E4 train metadata report verbatim (strict, duplicate-key-rejecting), reconstructs and pins
the train feature view, generates and independently verifies the accepted candidate set, and
assembles a deterministic plan. There is **no** public generic/accepted constructor with
override parameters; the pure structural assembler is private (`_assemble_experiment_plan`).

**Counts are derived** — `train_member_count` from the accepted snapshot's train membership,
`candidate_count` from the accepted candidate set, and `logical_job_count =
train_member_count * candidate_count`; no percentage or magic count is a contract constant.
The epoch-1 figures (**50** train members, **41** candidates, **2050** logical jobs) are
historical derived results, not inputs. **Validation and test membership are excluded** from
the plan and the logical-job iterator; the sealed test set remains untouched.

**Pure logical-job enumeration vs F3-C enqueueing.** `iter_logical_jobs` / `logical_job_keys`
enumerate the member × config product **in memory only**, in member-major then config-index
order, yielding exactly `logical_job_count` unique, repeatable `job_key`s. This is a pure
derivation — it never persists, publishes, enqueues, claims, or executes anything. Durable
enqueueing into `experiments.l2f_experiment_jobs` (with the append-only / identity triggers of
migration `0006`) is **F3-C**, which is not implemented here.

### Frozen F3-B hash formulas

Both are domain-separated: `sha256(DOMAIN_bytes + canonical_json(content))` (sorted keys,
compact separators). The `plan_hash` field is excluded from its own preimage; neither preimage
contains timestamps, UUIDs, filesystem paths, database URLs, hostnames, worker identities,
claim state, scores, labels, truth data, mutation tokens, raw feature values, or artifact
locations.

```
plan_hash = sha256(
  "minos:l2f-experiment-plan:v1\n" + canonical_json({
    schema_version, epoch, partition: "train",
    snapshot_hash, split_manifest_hash, registry_snapshot_hash, train_matrix_hash,
    train_feature_view_hash, feature_set_hash, feature_registry_hash, gatk_registry_hash,
    parameter_space_hash, experiment_parameter_policy_hash, candidate_set_hash,
    ordered_members: [{dataset_id, profile_id, content_hash, feature_values_hash,
                       vector_hash, member_index}],
    ordered_configs: [{config_index, config_hash, parameter_space_hash}],
    train_member_count, candidate_count, logical_job_count
  }))   # plan_hash itself excluded from the preimage

job_key = sha256(
  "minos:l2f-job:v1\n" + canonical_json({
    plan_hash, member_index, dataset_id, profile_id, content_hash,
    feature_values_hash, config_index, config_hash
  }))
```

The accepted epoch-1 plan derives `plan_hash =
1e6c4a5e70f370d800af91fc02ce6e312ebff29e39d0b8d554afa938f89959d8` (a historical derived
result, not a pinned constant).

### Migration 0006 frozen by byte SHA + contract hash

`storage/l2f_migration_contract.py` freezes `L2F_MIGRATION_SHA256` (the exact SHA-256 of the
authoritative migration file) and `L2F_CONTRACT_HASH` (a domain/version-separated canonical
hash over the migration SHA, revision lineage, accepted prior-migration byte hashes, and the
full static inventory — with no self-reference in its preimage). Tests recompute both and
require exact equality, prove per-section mutation sensitivity, and re-assert that migrations
`0001–0005` are byte-identical.

### Private Core table mappings (not `Base.metadata`)

`storage/l2f_tables.py` defines the five owned tables as SQLAlchemy Core `Table` objects on a
**dedicated private `l2f_metadata`** — deliberately **not** the L2-B declarative
`Base.metadata` (adding them there would silently change the accepted DB-READY storage
fingerprint `storage_schema_hash()` `4508728723…` / `role_policy_hash()` `1dfe6e56…`, which
tests prove is unchanged). Migration `0006` remains the authoritative DDL; the mappings never
call `create_all`/`drop_all` on any production path. External L2-D/L2-E tables referenced by
composite FKs are minimal `l2f_external_target_stub` declarations (FK-resolution only, never
owned/created). `L2F_OWNED_TABLES` (5) and `L2F_EXTERNAL_TARGET_STUBS` (6) are exported.

The parity test (`test_l2f_tables_parity`) now proves a **full mapping-relevant schema match**:
it materializes the private `l2f_metadata` via `create_all` into an isolated throwaway scratch
database and compares it, using the shared normalized introspector, against an alembic-built
`0006` on every mapping dimension — ordered columns, exact PostgreSQL types, nullability,
normalized server defaults, PK name + columns, FK names/columns/targets/options, UNIQUE
names/columns, CHECK names + **normalized expressions**, and explicit **index definitions**.
Triggers, ownership and grants are the migration/live-inventory responsibility, not the Core
mapping's.

### Exhaustive static + live schema equality

`tests/integration/layer2_db/l2f_introspect.py` is a reusable read-only normalized
introspector (**test infrastructure**, not a production module) built entirely from
`pg_catalog` + `aclexplode` over `COALESCE(acl, acldefault(...))` (never `information_schema`
grant views). The **frozen static inventory pending owner acceptance** `L2F_STATIC_INVENTORY`
enumerates every owned table's ordered columns/types/nullability/defaults, every PK/UNIQUE/CHECK/FK
with full definitions and options, every index, the six composite targets, the six triggers, the
job function, ownership, and both **raw and effective** ACLs (so the job function's NULL raw ACL
surfaces its implicit PUBLIC EXECUTE and the owned tables' absence of effective application-role
or PUBLIC grants is explicit). `test_l2f_static_inventory` proves live `0006` equals the frozen
inventory exactly, with a mutation matrix over every nested section, the revision lineage, each
prior-migration SHA, the migration SHA and the static constants, and an explicit
`0005→0006→0005→0006` lifecycle that re-derives it and restores the captured normalized `0005`
state exactly. `test_l2f_introspect_regressions` proves the effective-ACL facts on a live schema
(incl. sealed-test views remaining ungranted) and that identically-named indexes in two schemas
do not cross-contaminate.

### Curated 47-case behavioral attack matrix

A deterministic valid graph (`l2f_seed`) drives a **curated 47-case** attack matrix
(`test_l2f_attack_matrix`, `l2f_attacks`) — 12 plan, 10 plan-member, 10 config/payload, 15
job/immutability. Each of the 47 selected attacks reaches its declared named mechanism: a
specific FK/UNIQUE/CHECK constraint (asserted by `constraint_name`) or a specific immutability
trigger (asserted by stable SQLSTATE/message), with explicit isolation checks where a competing
constraint could otherwise fire, so each is correct independent of PostgreSQL's FK evaluation
order. The matrix is a **curated selection, not exhaustive coverage of every FK/UNIQUE/CHECK**;
the exhaustive constraint inventory above is the authority for full coverage. One member case,
`member_duplicate_logical_membership`, is a **single three-constraint equivalence class**: a
fully FK-valid duplicate plan-member necessarily collides with all three member UNIQUE
constraints at once (`uq_l2f_pm_plan_snapshot_member` / `uq_l2f_pm_plan_matrix_member` /
`uq_l2f_pm_plan_member_index`) because the composite-FK graph fixes the dataset, feature-values
hash and matrix index — so it asserts SQLSTATE `23505` with an observed constraint from that
set, not one isolatable invariant. Positive controls (permitted job status/claim updates
preserve scientific identity; every table accepts a valid new row) and the populated
`0005↔0006` lifecycle (`test_l2f_populated_lifecycle`) — which proves a downgrade preserves the
complete upstream row snapshot and restores the full normalized `0005` state across **every
MINOS schema** (catalog, profiling, experiments, evaluation, models, runtime, audit): every
relation kind incl. **views** (definitions, security_barrier, check_option), constraints,
indexes, triggers, functions, raw + effective ACLs for schemas/tables/views/functions/columns,
default ACLs, roles (with membership/admin/inherit/set options), the database and the alembic
revision — are included.

The operational `minos_engine_db` remains at `0005`; `0006` is never applied operationally in
any F3-A step. F3-B and later stages remain unauthorized.

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
