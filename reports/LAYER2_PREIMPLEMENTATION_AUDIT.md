# Layer 2 — Pre-Implementation Audit

> **Historical, append-only.** This report records the Layer-2 pre-implementation audit as it
> stood when written. Its tables are evidence and are never rewritten. For **current** stage
> state see `docs/DEVELOPMENT_STATUS.md`; for the L2-F2 baseline audit see
> `reports/LAYER2_BASELINE_PREIMPLEMENTATION_AUDIT.md`.

**Date:** 2026-08-17 · **Runtime:** CPython 3.12.x · **Scope:** design/audit only.
**No Layer 2 code is implemented by this report.** Companion documents:
[`LAYER1_FINAL_ACCEPTANCE_DECISION.md`](LAYER1_FINAL_ACCEPTANCE_DECISION.md) (owner
authorization) and [`LAYER2_DATASET_SPLIT_POLICY.md`](LAYER2_DATASET_SPLIT_POLICY.md)
(split + feature eligibility + input integrity).

This audit records the state verified before Layer 2 begins, the design constraints
extracted from the Layer 2 specification, the residual risks, and a staged plan
(L2-A … L2-J) each of whose exit gate must pass before the next stage may start.

---

## Append-only correction (L2-A, 2026-08-17)

The original staged plan below (Part E) labelled the **PostgreSQL storage
foundation** as L2-A. That sequencing is corrected: Layer 2 must not create or
consume any state until its exact accepted Layer 1 prerequisite is enforced in
code. The entry gate, foundational contracts, and the feature-eligibility registry
therefore become L2-A, and every later stage shifts by one. The original table is
retained unchanged (append-only evidence); the corrected sequence supersedes it.

```
Original audit L2-A: PostgreSQL foundation
Corrected  L2-A:     Exact entry gate, contracts, and feature registry
PostgreSQL foundation:      moved to L2-B
Immutable 50/10/15 split:   moved to L2-C
```

**Corrected sequence (authoritative):**

| Stage | Corrected goal | Gate |
|---|---|---|
| **L2-A** | Exact entry gate, foundational contracts, feature-eligibility registry, architecture enforcement | L2A-QUALIFIED (this stage; not L2-READY) |
| **L2-B** | PostgreSQL storage foundation (7 schemas, 5 roles, migrations, constraints) | DB-READY |
| **L2-C** | Immutable 50/10/15 dataset split manifest + registry | SPLIT-FROZEN |
| **L2-D** | Layer 1 profile ingestion (by identity tuple, fail-closed) | INGEST-READY |
| **L2-E** | Production feature view (ELIGIBLE-only, FORBIDDEN-free) | FEATURE-VIEW-READY |
| **L2-F +** | Experiment harness → baseline → models → controller → freeze → production | HARNESS-READY … PRODUCTION-READY |

L2-A is delivered by this implementation. It does **not** unblock
`Layer2Service.select_config` and introduces no PostgreSQL, storage, controller, or
manifest code. The stage table in Part E is the original (superseded) record.

---

## Part A — Verified pre-implementation state

### 1. Runtime and worktree
CPython 3.12.x. Worktree clean at HEAD `fa2a7696` (Commit H, Layer 1 v2 evidence).
No Layer 2 source, storage, experiments, or feedback packages exist yet — expected.

### 2. Specification integrity
The three specifications (overall, layer1, layer2) and `SPECIFICATION_MANIFEST.json`
were re-hashed; all SHA-256 values match the manifest (overall / layer1 / layer2 OK).
The Layer 2 spec (549 lines) was read in full.

### 3. Accepted gate identities re-verified (unchanged)
PROTOCOL-READY, TWIN-READY, and L1-READY require-pass and qualify --check all PASS at
HEAD; identities equal those pinned in the acceptance decision. This audit modifies
none of them (markdown-only change).

### 4. Layer 1 acceptance basis
Owner decision: measurement correctness **ACCEPTED**; Layer 2 progression
**APPROVED_WITH_EXCLUSIONS**. Complete numerical validation passed (229 analytical
fields/dataset, 0 L2-consumable fields unvalidated). Single exclusion:
`candidate_snp_density_per_base` (window-level SNP-localization utility; feature
limitation, not a measurement defect) → RESEARCH_ONLY.

### 5. Corpus and split feasibility
75 practice rounds, 15 per chromosome (CHR18–CHR22). The deterministic hash-stratified
50/10/15 split was dry-run and produced exactly {train:50, validation:10, test:15}
with 75 unique disjoint samples. No manifest was written (deferred to L2-B).

---

## Part B — Layer 2 design constraints (from the specification)

### 6. Single production entry point
`Layer2Service.select_config(DecisionRequest) -> DecisionResult` is the only path that
selects a live CONFIG. Current `service.py` raises `StageNotReadyError` (blocked) and
must remain blocked until L2-READY. `contracts.py` is already spec-§1 aligned
(`ControlMode`, `DecisionRequest`, `DecisionResult`) and is left unchanged.

### 7. Control modes
`SAFE_BASELINE` (return the qualified baseline CONFIG), `BOUNDED` (small guarded
perturbations), `FULL_CONTEXTUAL` (model-driven within the parameter space),
`REFINEMENT` (local search around a good CONFIG). Default `SAFE_BASELINE` until later
modes are qualified. Mode availability is stage-gated by the promotion sequence (§26).

### 8. Live decision algorithm
Baseline gate → candidate generation around the safe baseline → utility argmax under
the model → guardrails (bounds, downside/CVaR limit, deadline budget) → decision
manifest. On any failure (missing identity, timeout, low confidence) fall back to the
safe baseline and record `fallback_reason`. Fail-closed is the default everywhere.

### 9. Parameter registry (25 GATK params)
States FIXED / ACTIVE / CONDITIONAL / EXPERIMENTAL / DISABLED. Only ACTIVE (+ qualified
CONDITIONAL) params vary in production; EXPERIMENTAL params live in offline experiments
only. Each CONFIG canonicalizes to a stable `config_hash`; the parameter space carries
a `parameter_space_hash` bound into every decision and dataset registration.

### 10. Storage architecture
PostgreSQL 16 + SQLAlchemy 2 + Alembic + Pydantic 2. Seven schemas: `catalog`,
`profiling`, `experiments`, `evaluation` (offline), `models`, `runtime`, `audit`.
Artifacts store URI + SHA-256 (never bytes). Scientific-evidence tables are
append-only. Worker claim protocol uses `FOR UPDATE SKIP LOCKED`.

### 11. Database roles / least privilege
`minos_live` (runtime read + decision/audit append), `minos_runner` (experiment
execution), `minos_evaluator` (offline evaluation, isolated), `minos_trainer` (model
training on training rows only), `minos_admin` (migrations). The live role cannot read
truth/evaluation schemas; the evaluator role is unreachable until the candidate is
frozen — this is the SQL-level leakage barrier.

### 12. Uniqueness / integrity constraints
`UNIQUE artifacts(sha256)`, `UNIQUE gatk_configs(config_hash)`,
`UNIQUE decisions(round_id, decision_hash)`, plus FK chains
catalog→profiling→experiments→runtime. Datasets registered once in an immutable
registry; decisions may reference only a registered
`(bam_sha256, bai_sha256, reference_sha256, region_hash)` tuple.

### 13. Robust objective
The offline objective `J(c)` optimizes a downside-aware statistic (e.g. mean penalized
by CVaR of the score distribution across training samples), grouped by complete BAM,
with chromosome-held-out reporting — never a single-sample maximum. Calibration and
downside risk are first-class exit criteria for model qualification.

### 14. Decision manifest & feedback reconciliation
Every live decision emits a manifest (inputs' hashes, mode, selected `config_hash`,
model bundle id, guardrail outcomes, `decision_manifest_hash`) written append-only.
Post-round actual scores are reconciled against predictions to monitor drift; drift
never mutates a live model — it flags retraining, executed offline through the split.

### 15. Package layout (target, not yet created)
`src/minos_engine/layer2/` (service, contracts, control, guardrails),
`src/minos_engine/storage/` (SQLAlchemy models, Alembic env, repositories),
`src/minos_engine/experiments/` (candidate generation, execution harness),
`src/minos_engine/feedback/` (reconciliation, drift). `configs/layer2/default.yaml`
stays a placeholder (`implemented: false`, `blocked_until: l1-ready`,
`control.default_mode: SAFE_BASELINE`) until the relevant stage flips it.

---

## Part C — Data governance (cross-reference)

### 16. Fixed 50/10/15 split
Frozen before optimization; see split-policy §1–§2. Complete-sample partitioning; no
sample in two partitions; test untouched until the final locked evaluation.

### 17. Feature eligibility
ELIGIBLE / CONDITIONAL / RESEARCH_ONLY / FORBIDDEN per split-policy §4.
`candidate_snp_density_per_base` = RESEARCH_ONLY. Coordinates, identities, truth,
scores (at inference), and previous winning CONFIG = FORBIDDEN.

### 18. Leakage prevention
Feature allowlist + training-only transforms + role isolation + BAM-grouped CV; see
split-policy §6. Validation/test outcomes never become HPO observations.

### 19. RESEARCH_ONLY promotion
Seven-step protocol (split-policy §5); test-set results may never decide retention.

### 20. Input-integrity compensating controls
Mandatory BAM/BAI/reference/FAI SHA-256, dataset/region/parameter-space hashes, exact
tuple association, optional @SQ:M5 check, DB uniqueness/FK, fail-closed on missing
identity (split-policy §7). These compensate for the two documented Layer 1
input-integrity gaps (BAI has no BAM checksum; wrong same-name/length reference
undetectable without @SQ:M5).

---

## Part D — Risk & readiness

### 21. FORBIDDEN-consumption risk (highest)
A leaked identity/coordinate/score/previous-winner feature would inflate offline
metrics and fail live. **Controls:** SQL allowlist views, evaluator/trainer role
isolation, feature-schema hash recorded per model snapshot, CI check that the
production feature schema contains no FORBIDDEN field. Residual risk after controls: low.

### 22. Split-integrity / test-contamination risk (high)
Any reuse of validation/test samples for tuning invalidates the evaluation.
**Controls:** immutable split manifest + `dataset_registry_hash`; locked-test DB role
inaccessible pre-freeze; every model snapshot records the split-manifest hash.

### 23. Input-identity confusion risk (high)
Wrong reference or mismatched BAI silently corrupts profiling. **Controls:** §20
compensating controls; reject unregistered identity tuples; fail-closed.

### 24. Baseline-safety / availability risk (medium)
A controller fault must never break live variant calling. **Controls:** SAFE_BASELINE
default; unconditional fallback with recorded `fallback_reason`; deadline budget guard;
guardrail bounds on every emitted CONFIG.

### 25. Open specification ambiguities (owner decisions still required)
(a) Exact form/weights of the robust objective `J(c)` (CVaR level α, penalty weight).
(b) Numeric guardrail thresholds (max perturbation per BOUNDED step, downside limit,
minimum model confidence to leave SAFE_BASELINE). (c) Per-parameter ACTIVE/CONDITIONAL
assignment within the 25-param registry. (d) Concrete promotion-metric thresholds for
`candidate_snp_density_per_base`. (e) Model family/version pinning for the first
qualified bundle. These are recorded for owner input; none is resolved in this audit.

---

## Part E — Staged plan (L2-A … L2-J)

Each stage: **entry** = prior stage's exit gate PASS + explicit owner authorization;
**exit** = the listed criteria all pass. `ruff check .`, `ruff format --check .`,
`mypy src`, `pytest`, and `pytest --cov=src/minos_engine --cov-fail-under=90` are
exit criteria for **every** implementation stage, in addition to the stage-specific
items. No stage is implemented in this audit.

### 26. Promotion sequence (spec)
`DB-READY → HARNESS-READY → BASELINE-QUALIFIED → MODELS-QUALIFIED → CONTROLLER-FROZEN
→ L2-READY → PRODUCTION-READY`. The stage gates below realize this sequence.

| Stage | Goal | Entry | Exit criteria |
|---|---|---|---|
| **L2-A** | Storage foundation (schemas, SQLAlchemy models, Alembic migrations, roles) | Owner auth; audit accepted | 7 schemas + 5 roles created; migrations up/down clean; uniqueness/FK constraints enforced by tests; least-privilege verified (live role cannot read eval/truth); **DB-READY** gate PASS |
| **L2-B** | Immutable dataset registry + frozen 50/10/15 split manifest | L2-A PASS | `schemas/layer2-dataset-split-v1.schema.json` added; deterministic generator reproduces byte-identical manifest; all 75 samples registered with BAM/BAI/ref/FAI SHA-256 + region hash; disjointness + 50/10/15 counts asserted; registry hash frozen |
| **L2-C** | Profiling ingestion (Layer 1 profiles → `profiling`) | L2-B PASS | Profiles ingested by identity tuple only; identity-mismatch rejected/fail-closed; @SQ:M5 checked when present; ingestion idempotent under content hash |
| **L2-D** | Feature view + eligibility enforcement | L2-C PASS | Training SQL views expose ELIGIBLE (+qualified CONDITIONAL) fields only; FORBIDDEN fields absent; feature-schema hash emitted; CI asserts no FORBIDDEN field in the production schema |
| **L2-E** | Experiment harness + candidate generation (offline) | L2-D PASS | Deterministic candidate generation around baseline within parameter space; `FOR UPDATE SKIP LOCKED` worker claim; append-only experiment results with `config_hash`; **HARNESS-READY** gate PASS |
| **L2-F** | Baseline qualification | L2-E PASS | Safe baseline CONFIG qualified over the 50 training samples; robust objective `J(c)` computed (owner-approved α/weights); baseline reproducible + hashed; **BASELINE-QUALIFIED** gate PASS |
| **L2-G** | Model training (training rows only) | L2-F PASS | Transforms fit on 50 training only; BAM-grouped, chromosome-held-out CV; calibration + downside metrics reported; model bundle hashed with feature-schema + split-manifest hashes; validation used only for selection; **MODELS-QUALIFIED** gate PASS |
| **L2-H** | Controller integration + guardrails | L2-G PASS | `select_config` implemented for SAFE_BASELINE/BOUNDED under guardrails; unconditional fallback with `fallback_reason`; deadline budget honored; decision manifest emitted + hashed; no live consumption of truth/score/previous-winner |
| **L2-I** | Controller freeze + locked-test evaluation | L2-H PASS | Full pipeline frozen; single locked evaluation on the 15 test samples (first and only use); RESEARCH_ONLY promotion decided per protocol; **CONTROLLER-FROZEN** then **L2-READY** gate PASS |
| **L2-J** | Production readiness + feedback reconciliation | L2-I PASS | Feedback reconciliation + drift monitoring live (offline retraining path only); runbook + rollback to SAFE_BASELINE; audit trail complete; **PRODUCTION-READY** gate PASS |

### 27. L2-READY qualification design
L2-READY follows the established non-circular two-commit pattern: Commit A = frozen
Layer 2 source; Commit B (parents A) = generated gate artifact + report, bound to A's
git tree by re-hashing committed blobs. The non-mutating check verifies HEAD properly
descends from the qualified source via `git merge-base --is-ancestor` and re-hashes
source to confirm the pinned identity — never rebuilt. L2-READY additionally binds:
the DB migration head, the frozen split-manifest hash, the production feature-schema
hash (asserted FORBIDDEN-free), the qualified baseline `config_hash`, the model-bundle
hash, and the locked-test evaluation record. It also requires the accepted
PROTOCOL/TWIN/L1 identities to remain unchanged.

### 28. Global invariants across all stages
Fail-closed on any missing/mismatched identity; test set untouched until L2-I;
append-only scientific evidence; SAFE_BASELINE is always an available fallback;
accepted Layer 1 gate identities immutable (any breaking Layer 1 change re-blocks
Layer 2); no FORBIDDEN field ever reaches the production controller; every stage
re-runs the five gate checks to confirm no accepted gate regressed.

---

## Highest-risk gaps (summary)
1. FORBIDDEN-field leakage into the production feature schema (§21) — mitigated by CI
   schema assertion + role isolation, but requires the L2-D check to exist before any
   model is trusted.
2. Test-set contamination (§22) — mitigated by immutable manifest + locked-test role;
   depends on L2-B being frozen before L2-G.
3. Input-identity confusion (§23) — inherent Layer 1 gap; fully dependent on the §20
   compensating controls being enforced at ingestion (L2-C).
4. Unresolved owner decisions (§25) block L2-F/L2-G/L2-I from completing and must be
   answered before those stages start.

**Conclusion:** Pre-implementation state is verified and consistent with the Layer 2
specification. Layer 2 remains **not started**. Proceeding to L2-A requires explicit
owner authorization and resolution of the §25 decisions where their stages are reached.
