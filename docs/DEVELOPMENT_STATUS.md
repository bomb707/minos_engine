# MINOS_ENGINE — Development Status (SSOT)

**This file is the single source of truth for *current* development state.**
If any other document disagrees with this file about what stage we are in, this
file wins and the other document is stale.

This file is authoritative for **stage status**, not for its own commit SHA: a document
cannot truthfully contain the SHA of the commit that introduces it, so only immutable
historical anchors are recorded below and the current branch tip is read from Git.

Authoritative *frozen specifications* live in `docs/specifications/*.docx` and are
never edited. This file records where we are **against** those specifications; it
does not restate or override them.

---

## Current position

| | |
|---|---|
| Architectural stage | **L2-G — expected-score model training** |
| Previous stage | L2-F2 — baseline discovery + baseline qualification — **CLOSED** at BASELINE-QUALIFIED |
| Current implementation HEAD | verify from Git (`git rev-parse HEAD`) — deliberately **not** embedded here |
| Branch | `feature/L2-F` |
| HARNESS evidence commit (immutable anchor) | `107a745f2530b841ee3b399c81f3b2385cb6de2b` |
| L2-F2 pre-implementation-plan commit (immutable anchor) | `1536f73f0b0bbce0a514d56d2f47923945741762` |
| HARNESS historical Alembic head | `0008_l2f_execution_results` (stage-scoped; the repository head advances independently) |
| Operational DB revision | `0005_l2e_feature_view` |
| BASELINE-QUALIFIED evidence commit (immutable anchor) | `f01368d9f2a9850eae9c705eb8a63f968ca0684e` — 42/42 PASS, gate `b9436bf3…` |
| Next gate | **MODELS-QUALIFIED** (designed in `docs/layer2/L2G_EXPECTED_SCORE_MODEL.md`; registry declared, **not issued**) |
| Current task | **L2-G — PRE-FIT AUTHORITY CLOSURE.** The real training dataset is FROZEN: `l2g-training-dataset-v3` / `d031758c…`, 1040 scientific cells derived by the accepted builder from sealed TRAIN evidence (861 admitted / 149 non-admission / 30 execution failure, 0 conflicts). Six candidate and four reference ModelSpec hashes exist. Runtime pinned exactly. **No model has been fitted** |
| Previous task | **L2-G — TRAINING DATA + TARGET AUTHORITY.** The training contract is corrected to v2 (`35a9ac91…`): the model factorises over **ADMISSION**, not GATK exit status, so the 154 non-admitted evaluations train the admission head and never the score regressor. The learning table dedups 1175 terminal jobs to **1040** scientific (BAM, config) cells and weights every BAM equally. **No model has been fitted** |
| Previous task | **L2-F2-G — BASELINE-QUALIFIED.** The real trusted qualification ran against an ephemeral TRAIN observer and returned 42/42 PASS; the observer was dropped and TRAIN proven unchanged |
| Previous task | **L2-F2-F — PHASE-D VALIDATION CLOSURE.** All 40 real VALIDATION executions evaluated (0 failures) and closed under the pre-registered rule |
| Selected baseline | `157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea` — rank 0, inherited design index 42, objective `0.6472585261839707`, CVaR `0.6323350350370124`. The SEED ranked **3 of 4** (last); the search found something the seed did not |
| Source Alembic head | `0026_l2f2_phase_d_closure` (migrations `0001`–`0026`) |
| TRAIN DB revision | `minos_l2f2_baseline` @ `0020_l2f2_phase_c_execution` — frozen; 1175 terminal jobs, 986 admitted / 154 non-admitted, 35 execution failures, 0 infrastructure incidents |
| VALIDATION DB revision | `minos_l2f2_validation` @ `0026_l2f2_phase_d_closure` — 40/40 EVALUATED, closed. **Not consulted during L2-G TRAIN development** |
| TEST | **SEALED until L2-I.** Never opened |
| Production score authority | pinned `minos-protocol/minos_subnet` @ `649bb92c…` — executed, not reimplemented |
| Scoring contract | `l2f2-minos-scoring-v2` (`b24a07e2…`); v1 `d6f29e11…` superseded, still recomputable |
| Quarantined forensic DB | `minos_l2f2_baseline_tainted_20260826` at `0014_l2f2_exec_failure_runtime` — **DO NOT EXECUTE.** Retained as immutable evidence of the runtime defect |

`select_config` remains deliberately blocked by `StageNotReadyError` and stays
blocked until L2-H.

### L2-F2-A status — CLOSED (source + environment)

The **source** path is complete and tested end to end: migration `0009` (evaluation ledger,
projections, `SECURITY DEFINER` persistence, evaluator grants), migration `0010` (the
corrective below), the scoring contract and authority manifest, a Minos score compatibility layer
proven at **exact parity** against the real upstream `AdvancedScorer`, TRAIN-only truth
registration, a hap.py runner and output parser, the metrics artifact contract, the
content-addressed publisher and the production orchestrator.

> **Superseded in part.** The local compatibility layer (`minos_score.py`, `happy_metrics.py`,
> `happy_runner.py`) is **no longer the production score authority** — see *L2-F2-C scoring
> oracle* below. It is retained for audits and historical tests, and enforced out of the
> production import closure by a regression test.

#### What migration `0010_l2f2_evaluation_corrective` closed

`0009` is pushed history and is never rewritten; the corrective is additive and reversible
(`0010 -> 0009 -> 0010` is inventory-exact, and CI verifies that boundary on every run).

* **XOR serialization.** `0009`'s exclusive-outcome trigger took a `FOR SHARE` lock on the
  execution result before checking the opposite outcome table. SHARE locks are mutually
  compatible, so two overlapping transactions could both observe "no other outcome" and one
  execution could end up with a success **and** a failure under the same scoring contract. The
  lock is now `FOR UPDATE`. A two-connection integration control installs the old body, proves
  it admits both outcomes, and proves the new body admits exactly one.
* **Metrics artifact identity.** `metrics_artifact_id`, `metrics_artifact_sha256` and
  `metrics_media_type` were three independent columns with only the id bound to
  `catalog.artifacts`; artifact A's id could be paired with artifact B's digest. They are now
  ONE composite foreign key against `catalog.artifacts(id, sha256, media_type)`, and the media
  type is pinned by CHECK to the L2-F2 metrics document type.
* **Registration path.** `minos_evaluator` deliberately has no `INSERT` on `catalog.artifacts`,
  so the service principal previously had no way to register the document it publishes. The
  narrow `SECURITY DEFINER` registrar `evaluation.l2f_register_metrics_artifact(sha256, uri,
  size_bytes)` fixes media type and provenance itself — the caller supplies content identity and
  nothing that could reclassify the document.
* **Publisher.** The evaluation publisher no longer has a protocol of its own. The audited
  atomic protocol (temp inode → write → fsync → fchmod/fchown → hard-link no-clobber → inode
  identity proof → directory fsync → credential re-verification) now lives in
  `minos_engine.storage.content_addressed_publisher` and is shared with the L2-F1 result
  publisher, whose own suite is the proof the factoring changed nothing.
* **Orchestrator.** `minos_engine.evaluation.orchestrator.evaluate_execution` is the single
  authoritative production path. It takes an `execution_result_id` and reads dataset, partition,
  round, VCF digest and truth digests from PostgreSQL — never from the caller — refuses any
  non-TRAIN partition **before** constructing a truth path, verifies the recorded VCF and truth
  bytes, obtains the score under ONE `ScoringAuthority`, and persists ONE `EvaluationRecord`
  whose `evaluation_hash` is computed from that record. (At L2-F2-A it ran hap.py itself through
  `HappyRunner`; since the L2-F2-C scoring-oracle corrective it calls the pinned MINOS_SUBNET
  implementation instead.)

#### Runtime and retry isolation (source-only, no migration)

Three runtime defects closed before any real hap.py execution. None of them touches the schema,
the scoring semantics or any scientific identity.

* **Container containment.** `subprocess` kills only the Docker *client* on timeout; the
  container it started keeps running, keeps writing output and can race the next attempt. Every
  production invocation now gets a unique runtime identity (`minos-happy-<uuid>`, passed as
  `--name`), and a timeout or start failure explicitly runs `docker rm --force` on **that**
  container and then proves absence with `docker inspect`. If containment cannot be established
  the runner raises `HappyContainmentError` instead of reporting a clean timeout; the
  orchestrator records it as `EVALUATION_ERROR`. Cleanup is `shell=False` and separately bounded.
  A normal exit issues no destructive cleanup — `--rm` has already reaped the container.
* **Per-attempt output isolation.** hap.py used a deterministic prefix directly inside the shared
  work directory, so a crashed or timed-out attempt could leave partial output a later retry
  would read as its own. `EvaluationProvisioning.work_dir` is now the work **root**, and every
  run gets a fresh private attempt directory (mode `0700`, never reused) created by the audited
  workspace core in `minos_engine.storage.attempt_workspace` — the same implementation L2-F1
  execution uses, factored into a neutral module so the evaluator never imports the GATK
  execution path. Raw hap.py output is a runtime intermediate and is removed after a terminal
  outcome through the retained descriptor; the metrics document and the ledger rows are what
  survive.
* **Terminal replay.** An evaluation outcome is immutable and mutually exclusive, so re-running
  hap.py could not change the answer. Before any VCF verification, truth hashing or container
  start, the orchestrator reads its own terminal state through the evaluator's granted
  projections: an existing success or failure is returned immediately with no runner invocation.
  A crash *before* a durable terminal row is a different case and still gets a fresh attempt.
  Both outcomes present at once is refused as `DualTerminalOutcomeError` rather than resolved.

#### Environment (provisioned and independently verified)

Evidence: `reports/layer2/l2f2-a-environment-result.json`.

* **Workspace** `/home/hr/bittensor/minos_l2f2_baseline` exists (`0750`), with
  `evaluation_artifacts` and `gatk_result_artifacts` at `02750` and `evaluation_work` /
  `gatk_work` at `0750` with **setgid off** — the F7-R2 inherited-setgid failure is structurally
  prevented, and fresh attempt directories were proven to be created at exactly `0700`.
* **Baseline database** `minos_l2f2_baseline` at `0010_l2f2_evaluation_corrective`, holding the
  TRAIN closure only: 50 datasets (10 per chromosome chr18–chr22), 50 TRAIN split allocations,
  **0 validation, 0 test**. The closure reuses the accepted F7-B R3 mechanism (TRAIN derived from
  the frozen profile snapshot, identities preserved, source read under a READ ONLY transaction),
  extended only by `catalog.split_allocations`, which L2-F2's TRAIN registration projection is
  defined over and which F7-B did not need.
* **Service principal** `minos_evaluator_svc` — `LOGIN`, no `SUPERUSER`/`CREATEDB`/`CREATEROLE`/
  `BYPASSRLS`, member of `minos_evaluator` and nothing else. The group role stays `NOLOGIN`. Its
  credential lives at `minos_l2f2_baseline/l2f2.env`, mode `0600`, outside Git.
* **Database connection isolation** (corrective, see below): `PUBLIC` `CONNECT` is revoked on
  both databases and replaced by explicit allowlists. `minos_evaluator_svc` may connect to
  `minos_l2f2_baseline` and is refused by `minos_engine_db` at `CONNECT` authorization.
* **Truth**: exactly **50 TRAIN** truth identities registered through the real service login
  (first run 50 created / 0 existing; replay 0 created / 50 existing). Validation and test truth
  were never resolved, opened or hashed, and the baseline database contains no non-TRAIN
  allocation at all, so closed partitions are not even enumerable there.
* **Operational database untouched**: `minos_engine_db` remains at `0005_l2e_feature_view` with
  75 datasets and the 50/10/15 split; its before/after fingerprint is byte-identical.
* **Tools verified, not executed**: hap.py and bcftools resolve locally to their exact pinned
  digests (no pull), and the GATK 4.5.0.0 runtime bundle recomputes to the accepted
  `2707ad20…` identity.

**Ledgers are empty by design**: 0 experiment plans, 0 jobs, 0 execution results, 0 evaluation
results, 0 evaluation failures, 0 metrics artifacts. No score has been produced.

#### Connect-isolation corrective (environment ACL only)

Evidence: `reports/layer2/l2f2-a-connect-isolation-corrective.json`. The original environment
evidence (`l2f2-a-environment-result.json`) is historical and deliberately unmodified.

The environment audit found that `PUBLIC` held `CONNECT` on `minos_engine_db` by PostgreSQL
default, so `minos_evaluator_svc` could open the operational database — and because role
memberships are **cluster-global**, inherit `minos_evaluator`'s historical object grants there,
including over closed-partition projections. That was originally logged as HARDENING; for
stage-closure purposes it is **reclassified as a DEFECT (credential/database isolation)**,
because a TRAIN-only evaluator credential must not be able to enter the operational store at all.

PostgreSQL privileges are additive, so revoking `CONNECT` from the service role alone would have
been ineffective while `PUBLIC` retained it. The fix is an explicit per-database allowlist:

| Database | `PUBLIC` CONNECT | Allowed LOGIN principals |
|---|---|---|
| `minos_engine_db` | **revoked** | `postgres`, `minos_f7_observer` — **not** `minos_evaluator_svc` |
| `minos_l2f2_baseline` | **revoked** | `minos_evaluator_svc`, `postgres` |

Only three LOGIN principals exist cluster-wide, and both preserved operational principals already
held explicit `CONNECT`, so revoking `PUBLIC` could not orphan them. Proven with the real
credential: connecting to `minos_l2f2_baseline` succeeds as `minos_evaluator_svc`, and the same
host/port/username/password against `minos_engine_db` fails with
`FATAL: permission denied for database "minos_engine_db"` — at `CONNECT` authorization, not
authentication. Both preserved principals reconnect successfully, and `minos_f7_observer` is
refused by the baseline database, proving `PUBLIC` no longer confers access there.

**No object-level privilege, schema grant, table grant, function grant or role membership was
changed**, and the operational scientific/schema/data fingerprint is byte-identical
(`b0e9f5c1…`) before and after. Only the two databases' `CONNECT` ACLs changed. The three
historical F7 qualification databases were deliberately left unchanged; each is at `0008` with
0 split allocations and 0 truth identities, so none exposes closed-partition or truth-sensitive
state.

### L2-F2-B status — CLOSED (PROTOCOL_FROZEN)

`l2f2-baseline-search-protocol-v1` is committed, hashed and **pre-registered before the first
real score exists**. Manifest `manifests/l2f2_baseline_protocol_v1.json`, schema
`schemas/l2f2-baseline-protocol-v1.schema.json`, evidence
`reports/layer2/l2f2-b-protocol-freeze-result.json`.

| | |
|---|---|
| Protocol hash | `c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1` |
| TRAIN schedule | `manifests/l2f2_train_schedule_v1.json` — 50 TRAIN, 10 per chromosome, 10 balanced batches of 5 |
| Budget (STANDARD) | maximum **1215** evaluation pairs: 39×5 + 48×10 + 10×50 + 4×10 |

**Protocol decisions, resolved.** D1 absolute robust score primary with rank as diagnostic only;
D2 Option B; D3 α=0.25 and weights 0.50/0.30/0.20 with λ=1.00; D4 runtime is a tie-break, never
a weighted term; D5 STANDARD; D6 validation at L2-F2-F only; D7 no simulated opponent
distribution; D8 deterministic mixed-domain Latin hypercube.

    J(c) = 0.50·CVaR₀.₂₅(c) + 0.30·min_k(chr_mean_k(c)) + 0.20·mean(c) − 1.00·failure_rate(c)

Three distinctions are load-bearing. A known candidate failure contributes aggregation utility
`0.0` **and** a penalty, but never rewrites the evaluation ledger. A *missing* evaluation is
neither zero nor failure — the candidate is simply not complete and may participate only through
the bounded racing rules. And a failure of *our* harness (hap.py, truth resolution, publication,
persistence) is phase health, not the candidate's fault; it is never charged against a config,
and a phase aborts when infrastructure failures exceed 5% of attempts.

Racing eliminates only when a candidate's optimistic bound is **strictly** below the threshold
rival's pessimistic bound, is evaluated only on complete chromosome-balanced batches, and never
eliminates the seed. Promotion is seed-controlled: exactly 10 into Phase C and exactly 4 into
validation, always including the seed.

The implementation is pure — `src/minos_engine/baseline/` runs no process, touches no truth, and
writes to no database. Determinism was proven by regenerating the protocol and schedule in two
independent temporary directories (identical bytes and hashes, and identical to the committed
manifests), by confirming the runtime environment cannot move the hash, and by confirming that
moving α from 0.25 to 0.20 does.

**Nothing has been executed.** No Phase-A run, no observed score, no baseline candidate, no
validation access. TEST stays sealed until L2-I.

### L2-F2-C status — SOURCE_READY_PENDING_ENVIRONMENT

The source-side blocker is closed. A least-privilege real-GATK boundary now exists for the
baseline database; **nothing has been executed and no environment has been provisioned.**
Evidence: `reports/layer2/l2f2-c-execution-boundary-result.json`.

**Why the historical entries could not be reused.** `execute_next_accepted_job` is the accepted
F5 production entry, but it verifies the canonical *operational* database identity and revision
`0008` on every connection it opens — pointing it at `minos_l2f2_baseline` would have meant
weakening exactly the check that makes it trustworthy. `_execute_next_job_with_trust` is private,
documented test-only and typed `FakeGatkRunner`. And the historical Python path reads the plan
graph with direct `SELECT` and persists artifacts under `SET LOCAL ROLE minos_admin`, which an
external principal holding only `minos_runner` cannot do — and must not be given.

**Phase-A authority.** `manifests/l2f2_phase_a_execution_authority_v1.json` fixes the plan before
any score exists: TRAIN batch 0 (five members, one per chromosome chr18–chr22) × the accepted 39
candidates = **195** logical jobs, `plan_hash`
`97ba598778a5fc634345ded0901e4975af9c6b875c5b70fc7e76f2ae482e1b9a`. Member science is verbatim
from the accepted 50-member plan; only the local `member_index` is renumbered.

**The canary is structural, not chosen.** It is logical job **0** — member 0
(`minos-chr18-028662fb934529d7`, round `028662fb934529d7`) against config 0, the accepted seed —
so it cannot be picked after looking at results. Being a genuine Phase-A job rather than an extra
run, its exact immutable execution may later be reused inside Phase A, which can only *reduce*
the frozen 1215-pair budget.

**Migration `0011_l2f2_runner_boundary`** is additive and reversible (`0011 → 0010 → 0011` is
inventory-exact). It adds an append-only `experiments.l2f2_execution_authorities` binding a
persisted plan to the frozen L2-F2-B protocol hash, a truth-free resolution function, and a
narrow artifact registrar that accepts only `vcf` and `result_manifest` and fixes media type and
provenance itself. `minos_runner` receives `EXECUTE` on exactly those two functions plus `SELECT`
on `alembic_version`, and **no table privilege anywhere**.

**The public entry** `execute_next_l2f2_phase_a_job(*, worker_id)` takes nothing else — no
runner, engine, plan, hash, path or trust flag — and constructs the real `SubprocessGatkRunner`
itself. Every connection it opens verifies the baseline database, the exact required revision (now
`0012_l2f_plan_member_source_idx`; see the correction below), and that the
`session_user` is a LOGIN principal with no elevated attributes whose only MINOS membership is
`minos_runner`. It never issues `SET ROLE`, never writes a table directly, and returns the
durable `execution_result_id` the evaluator needs. Input and CONFIG bytes are re-hashed and
re-validated through the *same* verification cores the historical path uses, so neither path can
drift into trusting metadata.

**`execute_next_accepted_job` is unchanged** and still refuses any non-operational database and
any revision other than `0008`. No bypass flag was added anywhere.

**Not done, and not claimed:** `minos_runner_svc` is not provisioned, no credential file exists,
the real baseline database was still at `0010` at that point, no Phase-A plan is persisted, no job
is enqueued, and no GATK, hap.py, score or evaluation has been produced. (`minos_runner_svc` has
since been provisioned and the baseline database migrated to `0011`; see the correction below.)

#### L2-F2-C canary execution — NOT RUN

The canary itself has **not** run. The former `L2-F2-C-EXECUTION-BOUNDARY-BLOCKER` is resolved
in source; for the record, the surfaces that could not be used were:

* `execute_next_accepted_job` is the accepted public entry with the real `SubprocessGatkRunner`,
  but it requires the operational database identity and revision `0008`;
* `_execute_next_job_with_trust` is private and documented test-only (annotated
  `FakeGatkRunner`), and must not be silently reused for a real run;
* the F7 real-scratch route (`l2f_harness_ready_qualifier.run_harness_ready_qualification`) does
  run the real runner against a non-operational scratch database, but `_require_scratch_at_0008`
  refuses any revision other than `0008` and it is bound to the accepted 1950-job F7 plan.

That boundary is now implemented as `minos_engine.storage.l2f2_runner`, with control-plane
preparation in `minos_engine.storage.l2f2_canary_prepare`.

#### L2-F2-C control-plane persistence defect — FOUND AND CORRECTED

Provisioning the baseline environment and attempting the first Phase-A preparation surfaced a
real defect in the control plane, one revision deep in the schema rather than in the calling
code. `experiments.l2f_experiment_plan_members.member_index` was carrying **two incompatible
index namespaces at once**:

* the **plan-local ordinal** — contiguous `0..N-1` in plan order, unique per plan, and part of
  what `plan_hash` and `job_key` are computed over; and
* the **source feature-matrix ordinal** — because `0006`'s composite `fk_l2f_pm_matrix_member`
  bound that same column to `profiling.feature_matrix_members.member_index`.

For a plan covering the complete live TRAIN inventory the two are necessarily equal, so the
conflation was invisible. Phase A is not such a plan: it is five members of the accepted fifty,
at local indices `0..4` referencing matrix rows **`0/10/20/30/40`** — one per chromosome batch.
Preparation failed on member 1 (`minos-chr19-…`, source ordinal 10) because the resolver looked
for matrix ordinal 1; member 0 succeeded only because `0 == 0`. Had it resolved, the historical
full-inventory set-equality proof would have rejected the plan next, for covering 5 of 50 live
TRAIN members.

**Migration `0012_l2f_plan_member_source_idx`** separates the two namespaces additively. It adds
`source_matrix_member_index`, backfills it from `member_index` (a restatement, not a guess — the
`0011` FK forced them equal for every row it ever accepted), and re-points the composite lineage
FK at the new column. `UNIQUE(plan_id, member_index)`, the non-negative CHECK on the plan-local
ordinal, every hash formula, every role privilege, the `0011` execution functions, the job state
machine and the evaluation tables are all untouched. The **downgrade fails closed**: `0011` has
no representation for a row whose two ordinals differ, so a database holding a subset plan
refuses to go back rather than corrupting either namespace.

`source_matrix_member_index` is persistence lineage metadata only. It is deliberately **not** part
of `ExperimentPlan`, `plan_hash`, `job_key` or the Phase-A authority manifest, and the frozen
Phase-A identities are byte-for-byte unchanged.

On the source side, `_resolve_phase_a_upstream` proves the **complete accepted 50-member upstream
closure** with the unchanged historical resolver and set-equality check, and only then projects
the frozen five out of it — so a defect in any of the 45 *unselected* TRAIN members still blocks
Phase-A persistence. There is no `allow_subset`-style flag anywhere; the dedicated persistence and
enqueue boundaries take no plan, candidate set, member set, job key, start or count, recompute the
frozen Phase-A plan from committed authority, and refuse any other `plan_hash`.

**The runner now requires exactly `0012`.** `BASELINE_REVISION` is an exact match, never a floor
and never `head`: `0011` is refused like any other wrong revision. `0012` grants no privilege to
any role.

#### L2-F2-C scoring oracle — the score authority is MINOS_SUBNET, not this repository

**The rule.** The **caller** is GATK HaplotypeCaller and only GATK. The **scorer** is the exact
pinned MINOS_SUBNET implementation. MINOS_ENGINE is the adapter, the isolation boundary and the
ledger around them — it does not define the Minos score.

Until this corrective the production evaluator called MINOS_ENGINE's own
`compute_advanced_score` / `decide_admission`. Those had been proven at exact parity against the
upstream `AdvancedScorer`, but parity is not authority: it made this repository a *second*
scientific definition of the score, which would have to be re-proven on every upstream change.

**What production does now.** `minos_engine.evaluation.minos_subnet_oracle` executes the real
upstream code at commit `649bb92c6abccebde58a736a2b2af7fd77a701c1` — the actual
`utils.HappyScorer`, the actual `utils.AdvancedScorer`, and the validator's own
`_valid_round_score` and `_is_zero_input_advanced_fingerprint` helpers, **called rather than
reimplemented**. The final score, its normalization and the admission decision are upstream's
return values. Whatever internal tooling upstream chooses to run (hap.py, Docker, bcftools, RTG)
is its own business, and MINOS_ENGINE never rewrites, replaces, optimizes or substitutes it. If
upstream changes, a new upstream compatibility domain is pinned — never a second implementation
edited to match.

Four properties make that credible rather than aspirational:

* **Provenance is verified, not trusted.** The upstream root must be an absolute, non-symlink git
  checkout whose HEAD is exactly the authority commit, whose three authority files hash exactly
  as `manifests/l2f2_scoring_authority_v1.json` says, and whose git status is clean for those
  files. A branch name, a directory name, an mtime or a caller-supplied hash prove nothing.
* **The import is isolated.** Upstream's package names (`utils`, `templates`, `neurons`, `base`)
  are generic enough to collide with anything, so they are never imported into the evaluator
  process. A separate interpreter runs a standalone bridge with its working directory inside the
  verified checkout; a regression asserts those modules never appear in the evaluator's
  `sys.modules`.
* **Registered evidence is never handed to the scorer.** Upstream legitimately reindexes and
  writes intermediates beside the files it is given, so it is given regular-file **copies** in
  the fresh attempt workspace — never hard links, which would share the inode — and every copy is
  re-hashed against the registered identity before scoring begins.
* **The reference is bound to the execution.** The FASTA is resolved under the pinned validator's
  own layout (`<root>/<chrom>/<chrom>.fa`, beside `<chrom>.sdf`) and its digest must equal the
  `reference_sha256` the execution recorded. The SDF is evaluation-only — it never enters Layer 1,
  a Layer-2 live feature or the GATK CONFIG search — and a missing SDF fails evaluation closed
  rather than scoring approximately.

**Migration `0013_l2f2_upstream_score_oracle`** exists because
`AdvancedScorer.compute_advanced_score(metrics)` returns a single float: the four components
(core, completeness, FP-rate, quality) are local variables inside that function and are exposed
by no upstream entry point. `0009` stored them `NOT NULL`, which would have forced a local
recomputation of exactly the formula the row attests. They are now nullable and stored NULL, with
a NULL-tolerant range CHECK. What upstream *does* expose stays mandatory: `minos_score_100`,
`minos_score` and `overcall_penalty` (which upstream itself places in its metrics dictionary).
The downgrade fails closed rather than inventing component values.

**The scoring contract hash is unchanged** at
`d6f29e11eba9a25d5e28c80b0ba746795390042dee618a33f579f6c47af29fee` — the authority did not
change, only the implementation that now genuinely uses it. The **evaluation-hash domain moved to
v2**, because v1 bound locally-computed component values; v2 binds the upstream outcome and the
upstream source identity instead. No evaluation had ever been persisted, so nothing is
invalidated.

#### Scoring contract v2 — scientific authority separated from persistence envelope

Two identity defects were closed before the first real score.

**The contract claimed the wrong envelope.** `l2f2-minos-scoring-v1` embedded
`metrics_artifact_schema: l2f2-evaluation-metrics-v1` inside its `semantics`, and
`contract_content()` hashed the whole semantics block. When production moved to metrics artifact
**v2**, the v1 contract hash became an internally false statement: it asserted a v1 envelope for
rows that would be written in a v2 one. The fix is not to edit the number — it is that the
envelope never belonged in the scientific contract at all.

There are now **two identities that version independently**:

| Identity | Answers | Version |
|---|---|---|
| Scoring contract | *which Minos score is this* | `l2f2-minos-scoring-v2`, hash `b24a07e2…` |
| Metrics artifact + evaluation hash | *how did MINOS_ENGINE store it* | `l2f2-evaluation-metrics-v2`, domain `…:v2` |

The scientific contract binds upstream repository, commit, the three source digests, the
containers upstream runs, and Minos scoring semantics. It binds **no** artifact schema, no
evaluation-hash domain, no media type, no migration revision and no path — a loader guard refuses
a manifest that tries to re-admit one.

`manifests/l2f2_scoring_authority_v1.json` is untouched history and its hash still recomputes to
`d6f29e11eba9a25d5e28c80b0ba746795390042dee618a33f579f6c47af29fee`. Production loads
`manifests/l2f2_scoring_authority_v2.json`. Zero evaluations had been persisted, so no scientific
row is reinterpreted.

**The bcftools reference was never resolved.** The pinned upstream source names hap.py by digest
but bcftools by **tag** (`quay.io/biocontainers/bcftools:1.20--h8b25389_0`). MINOS_ENGINE does not
rewrite that tag — rewriting it would change the command upstream constructs, which is the exact
substitution this architecture exists to prevent. Instead the authority now records **both
identities per container**:

* `upstream_ref` — the literal string the pinned source itself uses, verbatim;
* `resolved_digest` — the immutable content that reference must resolve to locally.

Before any biological byte is read, the oracle probes the pinned checkout for its own literal
references, requires them to equal the authority's, and asks the local Docker daemon what each
one currently resolves to, requiring the audited digest. It **never pulls** — a scoring call must
not fetch new bytes off the network — never tags, never runs a container, and fails closed if the
image is absent or resolves to anything else. Upstream's commands are untouched throughout.

The metrics artifact and the SQL columns now keep the two apart by name: `happy_upstream_ref` /
`happy_resolved_digest` and `bcftools_upstream_ref` / `bcftools_resolved_digest`. A tag is never
recorded as a digest. SQL stores the **resolved** digests, one defined meaning; the literal
upstream references live in the metrics artifact. Persistence refuses any result whose tool
identities or source digests disagree with the authority.

No migration was needed: `0013` already represents these values.

#### L2-F2-C REAL CANARY — **PASS**

The first real end-to-end scientific run completed on 2026-08-25. One GATK execution, one exact
MINOS_SUBNET score, both independently verified.

**GATK.** `execute_next_l2f2_phase_a_job(worker_id="l2f2-canary-001")` — the accepted production
entry, no private helper, no manually chosen job — claimed the only enqueued row, the frozen
canary `b25fabaf…`: `minos-chr18-028662fb934529d7`, round `028662fb934529d7`, chr18, member 0,
config 0 (`4251cb85…`). **SUCCEEDED in 71 962 ms.** Execution result
`d1f43fa8-c2db-4006-a11a-99afd6a6aa1a`, result hash `ca549838…`, VCF `a91a2c0e…` (2 208 702 B),
result manifest `a477c413…`. Both artifacts are content-addressed regular files, mode `0640`,
re-hashed to their recorded digests.

**Score.** `evaluate_execution(...)` ran the pinned MINOS_SUBNET implementation at
`649bb92c…` through the isolated bridge (`l2f2-minos-subnet-bridge-v2`) under scoring contract
`b24a07e2…`:

| | |
|---|---|
| `advanced_score_100` | **61.8836338270872** |
| `minos_score` | **0.618836338270872** |
| admission | **ADMITTED** |
| `overcall_penalty` | 0.0 |
| f1_snp / f1_indel | 0.826530612244898 / 0.6 |
| truth totals (snp/indel) | 98 / 5 |

Evaluation `a523cb8a-…`, hash `7768a9c7…`, metrics artifact `c561912c…`
(`l2f2-evaluation-metrics-v2`, 33 upstream metric keys). The four AdvancedScorer components are
**NULL**, exactly as `0013` intends — upstream exposes only the combined score.

**Independent verification.** Three checks, none of which re-ran hap.py:

* the pinned upstream `AdvancedScorer` and validator helpers, invoked directly under the pinned
  worktree over the **persisted** upstream metrics, reproduced `61.8836338270872`,
  `0.618836338270872` and `zero_input=False` — bit for bit;
* `compute_evaluation_hash` over the persisted execution identity and the published artifact
  reproduced `7768a9c7…` exactly;
* a terminal replay of `evaluate_execution` returned the same evaluation id, hash, artifact id
  and artifact digest with `created=False`, publishing nothing new.

**Containment.** Exactly 1 job exists and 1 was executed. **Phase-A jobs 1..194 remain NOT
ENQUEUED and NOT EXECUTED.** One canary proves the execution and evaluation pipeline end to end;
it is not evidence about candidate quality, and no CONFIG was selected, promoted or ranked. D1–D8
are unchanged. The operational database stayed at `0005_l2e_feature_view`, and neither the
upstream clone nor the pinned worktree was modified.

#### Score-time source attestation — provenance proven at the moment of scoring

The oracle verified the pinned checkout **before** launching the scoring subprocess, then recorded
the digests from that pre-flight observation onto the result. But a pre-flight is made before the
subprocess exists: it can only ever say "this checkout *was* correct", never speak for the bytes
that process actually imported and ran. A checkout edited between the two — by a concurrent
operator, a rebuild, an errant sync — would have produced a real scientific result stamped with
digests that did not produce it. Unchanged container references were no help: they were exactly
what the previous corrective already checked, and a source edit can leave both untouched.

The identity is now established **three times**, and all three must agree with the committed
authority before any result is built:

1. **Pre-flight**, in the evaluator process, as before.
2. **Inside the scoring subprocess**, derived by the bridge itself from the root it was given —
   never from caller-supplied hashes, which would prove only that the caller can repeat itself.
   It hashes the three authority files before its upstream imports, again after them, and again
   after scoring completes, and requires all three snapshots to be identical. It also proves the
   modules it actually imported resolve beneath that root, so a shadowing `sys.path` entry cannot
   supply the scientific implementation.
3. **Post-score**, re-derived in the evaluator process after the subprocess has exited.

Any disagreement is a typed `MinosSubnetSourceAttestationError` and no result reaches persistence.
A post-score failure is reported distinctly from a pre-flight one, because "this checkout was
never right" and "this checkout changed while we were scoring it" call for different operator
responses.

The bridge protocol is `l2f2-minos-subnet-bridge-v2`. That is MINOS_ENGINE adapter plumbing: the
scoring contract hash, the metrics artifact schema and the evaluation-hash domain are all
unchanged, and no migration was needed.

The evaluator service will need `MINOS_L2F_MINOS_SUBNET_ROOT` pointing at a detached pinned
worktree; see `docs/layer2/EVALUATOR_SERVICE_PROVISIONING.md`. **That worktree must not be edited
while an evaluation is running** — an edit mid-score is now detected and refused rather than
silently mis-attributed.

The evaluator service needs `MINOS_L2F_MINOS_SUBNET_ROOT` pointing at the detached pinned
worktree, which is provisioned. *(Historical: the paragraph that stood here described the
environment work that preceded the canary — the `0011`→`0012` migration and preparation replay.
Both were completed, and the canary below then ran.)*

**Not yet done, and not claimed:**

* Phase-A logical jobs **1..194 are not enqueued and not executed**; exactly one job exists;
* **no Phase-A result has been aggregated**, no influential dimension selected and no Phase-B
  design produced — the analysis wrapper refuses anything short of all 195 decided observations;
* `BASELINE-QUALIFIED` is **not** issued. The objective (D1–D8) **is** frozen, and nothing in
  this stage may alter it.

### L2-F2-D status — SOURCE READY (the screen has NOT been expanded)

Phase A is 195 logical jobs: 5 chromosome-balanced TRAIN members × the 39 accepted OAT
candidates. Job 0 is the canary and is done. This substage builds the control plane for the
remaining 194 — and deliberately does not use it.

**A bounded expansion boundary, not an enqueue-all.** `expand_l2f2_phase_a_jobs(engine, start=,
count=)` inserts ONE contiguous slice of the frozen logical order. `start` is at least 1, so the
completed canary cannot be re-enqueued by arithmetic accident; `count` is bounded by the same
`MAX_ENQUEUE_BATCH = 64` the historical path uses; and the slice must lie inside the frozen 195.
Four explicit operator acts — `(1,64)`, `(65,64)`, `(129,64)`, `(193,2)` — tile jobs 1..194 exactly
once. Nothing scientific is a caller argument: plan, members, candidates, job keys and order are
all recomputed from committed authority, so an operator chooses *when*, never *what*. A replay
inserts nothing and resets nothing — status, `claimed_by` and any terminal outcome are untouched.

The readiness gate before expansion is a **pipeline** gate, never a quality gate: the canary must
be `SUCCEEDED` with exactly one execution result, no execution failure, and exactly one terminal
evaluation under contract `b24a07e2…` with no evaluation failure. **The score itself is not
consulted.** A near-zero score and a refused admission both still permit expansion, because
conditioning the screen on the canary's own number *after seeing it* would be a protocol change
made from a single observation — exactly what freezing D1–D8 exists to prevent.

**Migration `0014_l2f2_exec_failure_runtime` — a failed execution now carries its own runtime.**
`BaselineObservation` requires `gatk_runtime_ms` for every decided outcome and `aggregate_candidate`
uses mean GATK runtime as the frozen tie-break (D4). A success always carried its runtime; a
failure carried none, so a failed Phase-A job could not become a faithful observation without
inventing a duration — a zero, a timeout constant or the successful candidates' average — and each
of those would flow straight into candidate ranking. The column is `NOT NULL`, non-negative, and
supplied by the runner's own **monotonic** clock through the narrow `SECURITY DEFINER` writer,
whose signature widens by one argument; the runner gains no table DML. The upgrade **refuses** if
any pre-existing failure row is present: such a row predates the measurement and stamping one on
would be the fabrication this exists to prevent. The real store holds zero failure rows.

*(Two defects were found by the structural migration control and fixed before commit: the widened
writer was initially created by the migration login rather than `minos_admin` — a `SECURITY
DEFINER` function executes as its OWNER, so that would have silently widened the failure writer to
superuser authority — and it used standard SQLSTATEs where `0008` defines stable `MN0xx` codes the
Python boundary maps to typed errors.)*

**The ledger → observation reader.** `load_phase_a_observations` derives every DECIDED outcome from
the immutable ledgers and nothing else. Each case means something different to the objective and is
kept distinct: an admitted evaluation yields the exact persisted `minos_score`; a refused admission
yields `admitted=False`, `minos_score=None` and **no** failure code — it is emphatically not a
score of zero; a GATK failure yields its bounded code with the measured attempt runtime; an
evaluation failure yields its bounded code with the runtime of the GATK execution that did succeed;
and a job that is `PENDING`, `CLAIMED`, `RUNNING` or executed-but-unscored yields **no observation
at all**, because absence is what keeps "failed" and "not yet run" from collapsing into each other.
A state the ledger's own invariants forbid — a `SUCCEEDED` job with no result, both outcomes at
once, an evaluation under another scoring contract — is refused rather than interpreted.

**The analysis wrapper is complete-only.** `analyze_completed_phase_a(snapshot)` reuses the frozen
`aggregate_candidate`, `parameter_impacts`, `select_influential_dimensions`, `select_anchors` and
`build_phase_b_design` unchanged and adds no rule of its own. It requires **all 195** decided
observations: an impact is a mean over members and the K=6 cut is a comparison *between*
dimensions, so analysing a partial screen would let job completion order decide what Phase B
explores. It selects no baseline, reaches no validation or test data, and changes no D1–D8
decision.

**Containment.** Exactly 1 job exists in the real store and 1 has been executed. **Phase-A jobs
1..194 remain NOT ENQUEUED and NOT EXECUTED**, no GATK ran, no MINOS_SUBNET scoring ran, no new
score was observed, and the real database was not modified. The real store is at `0013` while the
source now requires `0014`; migrating it is a separate environment task.

### L2-F2-D — the Phase-A campaign was lost to a RUNTIME, and what closed it

**What happened.** The bounded queue expansion succeeded: all 195 frozen jobs were enqueued, the
canary untouched. The first five-job execution checkpoint then produced five durable
`GATK_NONZERO_EXIT` failures in 730–761 ms each, which the frozen objective reads as
CANDIDATE_FAILURE — five candidates apparently failing on ordinary parameter values while the seed
had run 71 962 ms to a valid VCF a day earlier.

**What it actually was.** A diagnostic re-ran each failed job's exact invocation outside the job
state machine and captured what production discards. All five, plus a seed control on the very
configuration that produced the ADMITTED canary score, exited **127** with one byte-identical
54-byte stderr:

```
/usr/bin/env: ‘python’: No such file or directory
```

The GATK 4.5.0.0 launcher is a `#!/usr/bin/env python` script. The worker's allowlisted child
`PATH` provided `/usr/bin/python3` and `java` but **no `python`**, so `env` aborted before a single
argument was parsed. A control proved the mechanism exactly: same launcher, same allowlist, only
`PATH` differing — with no `python`, exit 127; with one, `The Genome Analysis Toolkit (GATK)
v4.5.0.0`, exit 0. Classification `RUNTIME_ENVIRONMENT_DEFECT`; the candidates are exonerated.

**The five observations are TAINTED and were NOT rewritten.** They truthfully record what the
ledger was told; what they mean scientifically is wrong. They remain immutable historical evidence,
and the corrective is an append-only rebuild rather than a rewrite of history.

**Why an in-place retry was rejected.** `l2f_experiment_jobs` enforces `UNIQUE(job_key)` and
`UNIQUE(plan_id, plan_member_id, plan_config_id)`: one canonical row per scientific identity per
plan. Retrying in place would need a mutable status reset, an attempt counter or a supersession
concept — every one of which weakens the guarantee that a member × CONFIG has exactly one outcome.
The current database therefore becomes a **quarantined forensic campaign** and the same frozen
195-question plan is asked again on a fresh store.

**The runtime is now an identity, not an assumption.**
`minos_engine.experiments.execution_environment` binds the launcher digest, the scientific payload
bundle, the explicit interpreter (content + version), the JVM (content + version) and the
child-environment policy version `l2f-gatk-child-env-v2` into one domain-separated
`execution_environment_hash`. Host paths, hostname, PID, worker id, timestamps and the literal
`PATH` are excluded, so two hosts with the same runtime agree and the same host with a different
interpreter does not.

**Production no longer trusts the shebang.** `MINOS_L2F_GATK_PYTHON` and
`MINOS_L2F_GATK_PYTHON_SHA256` are mandatory, the child is started as `[python, launcher, *argv]`,
and `JAVA_HOME/bin/java` is resolved without any `PATH` lookup. Nothing is located through `PATH`,
`which`, `/usr/bin/env` or `sys.executable`. A regression pins the exact defect: with a child
`PATH` containing no `python` at all, `gatk --version` still succeeds.

**A broken worker now consumes nothing.** `execute_next_l2f2_phase_a_job` preflights the whole
runtime — launcher, bundle, interpreter, JVM, and the real `gatk --version` equalling the pinned
`4.5.0.0` — **before** claiming a job, and raises without touching a row if it cannot. The same
identity is re-verified immediately before and after HaplotypeCaller; a runtime that moved is
`EXECUTION_ERROR`, classified INFRASTRUCTURE_INCIDENT, never a candidate failure.

**A failure can now be diagnosed from the ledger.** `SubprocessGatkRunner` computed the exit code
and the stderr digest and then raised `GatkExecutionError("GATK exited with code 127")` carrying
neither, so the ledger stored NULL for both — 127 alone would have identified this cause without
any re-run. `GatkNonzeroExitError` now carries `exit_code`, `stderr_sha256`, `stdout_sha256` and
`runtime_ms` as attributes, and persistence takes them structurally, never by parsing a message.

**Migration `0015_l2f2_exec_environment`** adds `execution_environment_hash` (NOT NULL, lowercase
hex) to both outcome ledgers and widens both `SECURITY DEFINER` writers by one argument, dropping
the narrower signatures. It **REFUSES to upgrade any database that already holds an execution
result or failure**: those rows predate the identity, a default would be a lie and a backfill a
guess, and relabelling them would present the contaminated campaign as a corrected one. That
refusal is what protects the quarantined store.

**Result identity v2.** `l2f-gatk-execution-result-v2` / `minos:l2f-gatk-execution-result:v2` binds
the environment hash into the reproducible result preimage. v1 is untouched and still recomputable:
v1 rows truthfully mean "identified without a runtime", and silently widening what that hash covers
would rewrite the meaning of history.

**The scientific plan is unchanged.** Protocol `c548e190…`, Phase-A plan `97ba5987…`, authority
`9ad0ba48…`, candidate set `50d5f369…`, parameter space `b2d40191…` and scoring contract
`b24a07e2…` all still recompute. No parameter range moved, no candidate was removed or
reclassified, and D1–D8 are untouched — the new campaign asks the same 195 questions under one
verified runtime.

**Next step after green CI: CLEAN CAMPAIGN REBUILD** (a separate environment task) — archive the
contaminated database under a forensic name such as `minos_l2f2_baseline_tainted_20260826`, create
a fresh `minos_l2f2_baseline`, migrate base → `0015`, materialize the same frozen Phase-A plan,
provision the corrected runner environment, run a fresh canary, then expand and re-run Phase A.
**Phase A is not complete, no Phase-B work is authorized, and BASELINE-QUALIFIED is not issued.**

#### Terminology — D1–D8 are protocol decisions

An earlier document convention labelled the open L2-F2 questions D1–D8 "OWNER DECISION
REQUIRED". That framing is **not part of the engine architecture** and is no longer used:
there is no owner-decision layer, no approval workflow, no approval field, no owner
identity or signature, and no owner-specific gate anywhere in MINOS_ENGINE.

D1–D8 are **protocol decisions** — engineering and scientific choices that must be resolved
and pre-registered before any score-producing experiment, because the experiment protocol
has to be deterministic and fixed in advance, not because anyone must personally approve
them. The decisions themselves are unchanged:

| # | Protocol decision |
|---|---|
| D1 | Primary optimization target |
| D2 | Objective form |
| D3 | Robustness parameters |
| D4 | Runtime treatment |
| D5 | Compute budget |
| D6 | Validation timing |
| D7 | Platform-reward modelling policy |
| D8 | Phase-B design family |

Once L2-F2-B freezes them into `l2f2-baseline-search-protocol-v1`, their authority comes
from the committed protocol, its canonical manifest, its protocol hash, its tests and its
evidence — never from an "owner" concept.


### L2-F2-E status — PHASE-B CONTROL PLANE READY, PHASE-B EXECUTION HELD

**Phase A is COMPLETE.** The clean campaign asked all 195 frozen questions under one verified
execution environment `71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3`:
195/195 decided, 190 execution results, 5 execution failures, 190 evaluations, 0 evaluation
failures, 179 admitted, 16 candidate failures, and **0 infrastructure incidents** — nothing in the
screen was decided by a defect of ours.

**The Phase-B design is derived, never chosen.** `derive_completed_phase_a_analysis` refuses
anything less than a complete, incident-free, single-runtime Phase A and returns the analysis plus
its identity; the design is then the frozen recipe — seed + one anchor per influential dimension +
41 LHS points — over the six dimensions Phase A actually moved:

| | |
|---|---|
| Phase-A analysis hash | `25794987e49ca2a17776bf355326e8ff396366a6e3340fe7f9d2e24a855c80ae` |
| Phase-B candidate-set hash | `63b0244935edb46c799583cae9715733e52b25fba85f581a33ebe6949c09de0e` |
| Phase-B plan hash | `e80594043580334ddf2504577e2fa030dff0c1217ac334804d9304a0ec72596b` |
| Shape | 48 candidates × 10 TRAIN members = 480 logical jobs (a budget CEILING, not a quota) |
| Members | TRAIN batches 0 and 1 — source ordinals `0, 10, 20, 30, 40, 1, 11, 21, 31, 41`, chromosomes `chr18…chr22` twice |
| Influential dimensions | `min_base_quality_score`, `base_quality_score_threshold`, `active_probability_threshold`, `min_pruning`, `phred_scaled_global_read_mismapping_rate`, `contamination_fraction_to_filter` |

The design derived live from the database is **identical** to the design recorded in the completed
Phase-A run artifact — ordered config hashes, seed, anchors, dimensions and candidate count all
match — and re-derivation from the immutable ledger is deterministic.

**Two anchors are total-failure configurations, and that is not a defect.** The frozen selector
picks the best Phase-A alternative in each selected dimension; in two dimensions every alternative
failed. Impact measures *sensitivity*, so a dimension that breaks the caller is exactly the kind of
dimension Phase B must explore. They are carried as produced; no override was applied.

**Multi-plan scoping was a prerequisite, not a side effect.** The Phase-A readers selected every
job in the database and refused the first row they did not recognise — correct while Phase A was
the only plan, and wrong the instant a second is persisted. Reading is now scoped to a
`plan_hash` through one shared plan-scoped core, which narrows *what is read* and relaxes nothing
about what a row must prove: a forged job claiming a plan's hash still fails closed. Both plans now
coexist in one store under test, with the scoping pinned in both directions.

**What is ready:** the derived authority, plan persistence beside Phase A (no migration needed —
the generic plan schema already represents a second plan), bounded batch materialization,
plan-scoped progress, the frozen racing decision, and seed-controlled promotion to exactly ten.

#### FINDING (CORRECTED by `0016`) — Phase-B jobs could not be claimed.

`0011_l2f2_runner_boundary` states the constraint in its own words — *"the ONLY phase 0011 admits.
A later phase is a later migration, never a looser CHECK."* Concretely,
`ck_l2f2_authority_phase` permits only `PHASE_A`, so a Phase-B execution authority row cannot be
recorded, and `experiments.l2f2_resolve_claimed_execution` looks the authority up with a hardcoded
`a.phase = 'PHASE_A'`, so even a recorded one would not be found. A materialized Phase-B job
therefore fails at claim time with *"plan … has no PHASE_A L2-F2 execution authority"*.

Both were corrected by `0016` and its administrative preparation path, described below. The
finding is kept because the boundary it describes was deliberate, and the correction preserves it:
the Phase-A interface is still Phase-A-only, and Phase C is still a later migration.

#### FINDING — racing cannot eliminate anyone after batch 0.

At five of ten members the frozen `-1.0 · failure_rate` term moves both bounds by exactly 0.5: the
worst reachable optimistic bound (every seen member failed, every unseen one perfect) and the best
reachable pessimistic bound (every seen member perfect, every unseen one failed) are the same
number. Elimination requires a **strict** inequality, so a single balanced batch can never
eliminate anybody, whatever the field scores — Phase B will spend all 480 jobs, not fewer. The rule
is frozen and errs in the safe direction, so this is recorded, not adjusted; it means the budget
saving racing exists for is only reachable at a larger batch count.

**The blocker below is now corrected by `0016`.** Phase A is complete, the Phase-B design and
control plane are ready, the execution boundary is open in source, and **no Phase-B GATK execution
and no Phase-B score exist**. BASELINE-QUALIFIED is not issued.

### L2-F2-E status — PHASE-B EXECUTION AUTHORITY (migration `0016`)

`0011` was explicit that a second phase would be a later migration, never a looser CHECK.
`0016_l2f2_phase_b_execution` is that migration and stays exactly as narrow:

| | |
|---|---|
| Phase vocabulary | `ck_l2f2_authority_phase` becomes `PHASE_A` or `PHASE_B`. Phase C remains a later migration |
| Canary | `canary_job_key` becomes nullable under `ck_l2f2_authority_canary_phase`: Phase A must carry one, Phase B must not. No canary is invented for Phase B |
| Phase-A resolver | **untouched** — same signature, same body, same `phase = 'PHASE_A'` predicate. The privileged Phase-A interface remains Phase-A-only |
| Phase-B resolver | new `experiments.l2f2_resolve_claimed_phase_b_execution(text, uuid, text)`, owned by `minos_admin`, `SECURITY DEFINER`, fixed `phase = 'PHASE_B'`. Same truth-free result shape; no `p_phase` argument exists |
| Grants | `EXECUTE` to `minos_runner` and `minos_admin` only. No role gains any table privilege; `PUBLIC`, `minos_live`, `minos_trainer` and `minos_evaluator` are denied |
| Claiming | **unchanged.** `minos_l2f_claim_next_job(plan_hash, worker_id)` from `0007` was already plan-scoped, and each authority supplies its own plan hash — the queue never needed to know phases exist |
| Populated stores | `0016` upgrades a store that already holds execution evidence, unlike `0015`. The real baseline is exactly such a store and nothing here reinterprets a row of it |
| Downgrade | REFUSES while any `PHASE_B` authority exists, before any schema mutation. Squeezing that row back into a Phase-A-only CHECK would mean deleting or relabelling append-only lineage |

**The authority is prepared, not assumed.** `prepare_l2f2_phase_b_execution_authority` derives
every value from the completed Phase-A ledger — plan, candidate set, schedule, parameter space and
counts — and accepts no override for any of them. It requires the derived plan to be persisted
first, is idempotent, and raises a typed conflict rather than repairing a row that disagrees.

**The runner never names a phase.** Each authority carries its own `phase`, and the execution core
selects the resolver from it. `execute_next_l2f2_phase_a_job` and `execute_next_l2f2_phase_b_job`
share one execution core, one claim function, one byte-verification path, one artifact registrar
and one pair of outcome writers; only the resolution boundary differs.

**Proven end-to-end, on ephemeral PostgreSQL only.** A complete Phase-A ledger, a persisted
Phase-B plan, a prepared authority, a claim, a Phase-B resolve, `CLAIMED → RUNNING`, and a durable
decided observation — all under a principal whose only membership is `minos_runner`. **The active
baseline was not migrated and holds no Phase-B row of any kind.**

### L2-F2-E status — PHASE-B RUNNER BOOTSTRAP (migration `0019`)

The first real Phase-B invocation failed before it claimed anything, and it failed for the right
reason. `execute_next_l2f2_phase_b_job` opened the store as `minos_runner_svc` and then called
`build_l2f2_phase_b_authority`, whose derivation reads the completed Phase-A **scientific** ledger:
evaluations, scores, dataset identities. `minos_runner` has no `USAGE` on `evaluation` and no
`SELECT` on the jobs, plans, dataset-registry or authority tables — every one of those denials is
deliberate, because the runner is truth-free by construction. **The runner boundary had
accidentally taken a dependency on the control plane's derivation** (finding L2F2E-F6).

Phase A never showed this: `execute_next_l2f2_phase_a_job` builds its authority from committed
source manifests, not from a database read, so the defect could not surface until Phase B ran.

Granting the runner those reads would trade the entire boundary for convenience. The runner does
not need the Phase-B authority — 48 configurations, ten members, six dimensions, the analysis they
came from. It needs two strings: **which plan** it may claim within, and **which runtime** that
plan's science was chosen under, so a worker on a different JVM refuses before consuming an
observation.

`0019_l2f2_phase_b_bootstrap` adds one narrow `SECURITY DEFINER` function,
`experiments.l2f2_resolve_phase_b_runner_bootstrap()`, owned by `minos_admin`, `STABLE`, with no
arguments — so no worker can nominate a plan or a runtime. Everything the answer depends on is
checked inside: exactly one `PHASE_B` authority under the frozen protocol, bound to its exact
persisted TRAIN plan with matching identities and the frozen 10 × 48 = 480 shape; exactly one
`PHASE_A` authority; that campaign durably complete and terminal; and its execution outcomes
carrying exactly one execution-environment hash. It **reads nothing in the `evaluation` schema** —
runtime lineage is a question about execution, not results, which is what makes it safe to hand to
a truth-free principal. A Phase-A campaign with legitimate candidate execution failures (the real
one has five) is complete for this purpose: a failure records its runtime exactly as a success
does.

The runner now consumes a `_PhaseBRunnerAuthority` carrying only `plan_hash` and `phase`. The
claim stays plan-scoped through `minos_l2f_claim_next_job`, and the claimed-job resolver
`l2f2_resolve_claimed_phase_b_execution` still enforces the exact job identity afterwards — the
two boundaries answer different questions and both remain. No table, grant, role or relation
changed; the runner gained `EXECUTE` on one function and no table access whatsoever.

### L2-F2-E status — JVM DISPATCH CLOSURE

Real Phase-B activation surfaced one concrete gap, and it is the exit-127 defect one level down.
`0015` fixed how the launcher *starts*: an explicit, content-verified interpreter, so a `PATH`
without `python` can no longer stop a job. But Broad's launcher then builds its own command as
`["java", ...]` — a bare token — so the JVM that actually runs HaplotypeCaller is whatever the
child `PATH` resolves. `JAVA_HOME/bin/java` was the pinned *identity*; nothing proved it was the
*dispatch*.

The two coincided only because the provisioned `PATH` happens to put the pinned JDK first. That
was verified read-only on the live runtime — bare `java` resolves to `/home/hr/.local/opt/java17/
bin/java`, sha `b48547e8…`, the pinned binary — so **the completed Phase-A campaign is unaffected
and is not reopened**. What was missing was a proof, not a correct outcome.

The runner now derives that proof from the exact environment dictionary it is about to pass as
`env=`: bare `java` must resolve to the pinned JVM by canonical path *and* by content. It runs at
`preflight()` — before any launcher process exists, so a bad `PATH` costs no observation — and
again immediately before every scientific launch, because a `PATH` can move between service
startup and job N. The same dictionary that is proven is the one that is launched; verifying one
environment and running another is precisely the hole being closed.

Deliberately unchanged: policy stays `l2f-gatk-child-env-v2` and the execution-environment hash
stays `71e14a49…`. v2 already claimed the JVM was pinned — this makes the implementation enforce
what it claimed, and a new policy version would falsely imply a different runtime. Source comments
that said the JVM "is never located through PATH" were literally untrue of the upstream launcher
and now state the real contract.

### L2-F2-E status — PRIVILEGED-OWNER CORRECTIVE (migration `0017`)

A `SECURITY DEFINER` function executes with its OWNER's authority, so **who owns one is its
privilege boundary**. `0008` creates its writers under `SET ROLE minos_admin` for exactly that
reason and `0016` created the Phase-B resolver the same way. `0011` did not: it created

* `experiments.l2f2_resolve_claimed_execution(text, uuid, text)` and
* `experiments.l2f2_register_execution_artifact(text, char, text, integer)`

as whatever principal ran the migration — a SUPERUSER on the real store. The runner calls both on
every execution, so the boundary whose entire purpose is to need no administrative authority has
been making two of its calls with more authority than the control plane itself holds. The runner's
grants were never wrong; the definer was. **No scientific result is invalidated** — the function
bodies are narrow and unchanged, and nothing about what they returned depended on the owner — but
real Phase-B activation was held until it was corrected.

`0017_l2f2_owner_corrective` changes ownership metadata and nothing else. Both functions are
`ALTER FUNCTION ... OWNER TO minos_admin` — never recreated — so their OIDs, signatures, bodies,
`SECURITY DEFINER` flag and `search_path` are identical afterwards, proven field by field.

| | |
|---|---|
| Corrected | the two `0011` definers above, owner → `minos_admin` (`rolsuper` false, `NOLOGIN`) |
| Not corrected | `experiments.l2f2_execution_authorities` keeps its owner. Owning it would implicitly give the control plane `UPDATE`, `DELETE`, `TRUNCATE`, `ALTER` and `DROP` over append-only lineage — what `0011` deliberately withheld — and nothing needs it: a table has no definer semantics, and the re-owned resolver reads it through the `SELECT` grant it already has |
| ACL | PostgreSQL rewrites the ACL's **grantor** when ownership moves; it cannot do otherwise. Every MINOS role's effective `EXECUTE` is unchanged, and the superuser stops being the grantor |
| Downgrade | returns both to the migration principal (who created them in `0011`) **and re-issues `0011`'s two `EXECUTE` grants** — while `minos_admin` owns a function its explicit grant is absorbed into the owner entry, so handing ownership back would otherwise strip the control plane's own access silently |
| Runtime | ownership is a privilege context, not decoration, so it is proven by execution: the re-owned resolver and registrar both work for a principal whose only membership is `minos_runner`, and a whole Phase-A execution runs end to end |
| Hard regression | every `SECURITY DEFINER` function reachable from `execute_next_l2f2_phase_a_job` or `execute_next_l2f2_phase_b_job` is checked against `pg_roles.rolsuper`, not against an owner name. After `0017` the count of superuser-owned ones is **0** |

#### FINDING (CORRECTED by `0018`) — four `evaluation` definers had the same defect

`0009`/`0010` created `evaluation.l2f_record_evaluation_result`,
`evaluation.l2f_record_evaluation_failure`, `evaluation.l2f_register_metrics_artifact` and
`evaluation.l2f_register_train_truth_identity` the same way: `SECURITY DEFINER`, owned by the
migration superuser. So were the tables `evaluation.l2f_evaluation_results` and
`evaluation.l2f_evaluation_failures`. They were reported rather than folded into `0017` — they are
evaluator-facing, not runner-facing, and widening a privileged corrective past its authorization
is how privileged changes go wrong. `0018` is their own migration.

### L2-F2-E status — EVALUATOR-OWNER CORRECTIVE (migration `0018`)

`0008` wrapped its entire upgrade in `SET ROLE minos_admin`, so the execution ledgers and their
writers are control-plane-owned. `0009` and `0010` did not. `0018` restores the evaluation side to
that same model — it does not invent one.

| | |
|---|---|
| Functions re-owned | the four above → `minos_admin`, by `ALTER FUNCTION ... OWNER TO`. OIDs, bodies, signatures, `SECURITY DEFINER` and `search_path` all identical, asserted field by field |
| Tables re-owned | `evaluation.l2f_evaluation_results`, `evaluation.l2f_evaluation_failures` → `minos_admin`, by `ALTER TABLE ... OWNER TO`. OID, columns, constraints, indexes, triggers, row count and row digest all identical |
| Why these tables, when `0017` left its table alone | `experiments.l2f2_execution_authorities` is a control-plane relation on which `0011` deliberately restricted `minos_admin` to explicit `INSERT, SELECT`, and no definer needed more. These two are the evaluator's append-only outcome ledgers, exactly analogous to `0008`'s — and the re-owned writers genuinely need `INSERT` on them, which `minos_admin` held by no grant at all |
| Application privileges | unchanged, to the privilege. No role gains a direct write anywhere; the evaluator still writes only through the four functions and reads only what it already read |
| Runtime proof | truth registration, metrics registration, the success writer and the failure writer all exercised through production code under a principal whose only membership is `minos_evaluator`, on a `0018` store — plus the ledger XOR and append-only rules, from that same principal |
| Hard regression | zero superuser-owned `SECURITY DEFINER` functions on the evaluator path, and `0017`'s runner-side guarantee re-asserted so fixing one side cannot regress the other |

One observation recorded rather than changed: `minos_evaluator` already holds direct `INSERT` on
`evaluation.dataset_evaluation_identity` from the L2-E identity path, long predating this work.
`0018` neither grants nor removes it.

### L2-F2-E status — PHASE B IS COMPLETE, TEN CANDIDATES PROMOTED

The 480-pair Phase-B screen ran to completion on the real baseline under the same single verified
execution environment as Phase A, `71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3`:

| | |
|---|---|
| Decided | 480 / 480 |
| Executions | 450 succeeded, 30 execution failures |
| Evaluations | 450, of which 308 admitted and 142 validator non-admissions |
| Candidate failures | 172 (30 execution + 142 non-admission) |
| Infrastructure incidents | **0** — nothing in the screen was decided by a defect of ours |
| Racing | as predicted, batch 0 eliminated nobody; all 48 candidates ran both batches |

**Promotion selected exactly ten, and the seed placed fifth on its own merit.** The promoted set
is one seed, three Phase-A anchors and six LHS points. Nothing was reserved for the seed: the rule
that it can never be eliminated did not have to be used.

| | |
|---|---|
| Phase-B completion hash | `0c98017a7a79bc7d8bf897983b4b25765d64a52d4f4caa70458abaf1d508e1fd` |
| Phase-C candidate-set hash | `923e45d59799c34ca1831c65b57604405165935a9e51d4c0e690abbfaf122bd4` |
| Phase-C plan hash | `03b846e735e5817a8df7d5c37ae15778a955828a56513b16cef8ff2193a0aa43` |

### L2-F2-E status — PHASE-C CONTROL PLANE (migration `0020`)

Phase C is the TRAIN **confirmation**: the ten promoted configurations against all fifty TRAIN
members, raced batch by batch down to four finalists. Its shape is the frozen one — 10 batches × 5
chromosomes (`chr18…chr22`), 50 members, 500 logical jobs as a **ceiling**, not a quota, since a
candidate eliminated at batch *k* legitimately stops there.

#### The tie-break index — the ambiguity that had to be resolved before anything was written

Phase C carries **two** orderings of the same ten configurations, and they are not the same fact:

| Ordering | Range | What it is |
|---|---|---|
| Promotion order | `0..9` | plan-local bookkeeping — where a candidate sits in Phase C's own member/config grid |
| **Inherited Phase-B design position** | `0..47` | **the scientific tie-break index** — where the candidate sat in the frozen Phase-B design |

The frozen tie-break is *higher J → lower mean GATK runtime → lower candidate index in the frozen
phase design → lexical config hash*. Reading "candidate index" as the promotion position would be
a **new** tie-break invented after the ten were known, and on this campaign the two readings
disagree: the promoted set's inherited indices are `[42, 6, 31, 3, 0, 36, 25, 11, 5, 43]`, which
is not `[0..9]` in any order. The rule is now pinned in exactly one place —
`minos_engine.baseline.design.phase_c_inherited_candidate_index` — and
`tests/unit/baseline/test_phase_c_candidate_index.py` constructs a genuine tie in which the two
readings choose **different winners**, so a silent regression to the wrong one cannot pass.

Both orderings are bound into the Phase-C candidate-set identity, because both are facts about the
set; only the inherited one ever breaks a tie, and it is carried unchanged into the finalist set.

#### Migration `0020_l2f2_phase_c_execution`

`0016` widened the runner boundary from one phase to two and said the third would be a later
migration. `0020` is that migration, and it is deliberately the same shape as `0016` — a boundary
that grows differently each time is a boundary nobody can audit:

| | |
|---|---|
| Phase vocabulary | `ck_l2f2_authority_phase` becomes `PHASE_A`, `PHASE_B` or `PHASE_C`. The TRAIN vocabulary is now closed; VALIDATION remains a later migration |
| Canary | `ck_l2f2_authority_canary_phase` extends the Phase-B arm to Phase C: Phase A must carry a canary, B and C must not. No canary is invented for a phase that inherits a proven chain |
| Phase-C resolver | new `experiments.l2f2_resolve_claimed_phase_c_execution(text, uuid, text)`, owned by `minos_admin`, `SECURITY DEFINER`, fixed `phase = 'PHASE_C'`. Truth-free result shape; no `p_phase` argument exists, so no caller can name a phase |
| Phase-C bootstrap | new no-argument `experiments.l2f2_resolve_phase_c_runner_bootstrap()`, returning only `(plan_hash, execution_environment_hash)` — the `0019` shape, and asserted to touch no `evaluation` relation |
| Privileges | `EXECUTE` to `minos_runner` and `minos_admin` only; no role gains a direct table grant, and the runner still has no `USAGE` on `evaluation` |
| Nothing else moves | relations, indexes, triggers, roles, memberships, schema security and default ACLs are all byte-identical across `0019 → 0020`, and no existing function is redefined |
| Downgrade | REFUSES while any `PHASE_C` authority exists, before any schema mutation, for the same reason `0016`'s did |

#### What the control plane does and does not decide

`expand_l2f2_phase_c_batch` takes a batch index and a bounded slice and nothing else. **Which**
candidates a batch may contain is derived inside, recomputed from the immutable ledger on every
call: everyone for batch 0, and thereafter whoever the frozen racing rule still permits. There is
no caller-supplied survivor list and no enqueue-all API; `MAX_ENQUEUE_BATCH` stays 64.

`select_l2f2_validation_finalists` ranks only non-eliminated candidates holding a complete
fifty-member aggregate. An eliminated candidate's unseen remainder is never fabricated to make its
aggregate look complete.

#### OBSERVATION — a per-batch racing decision is a decision, not a historical record.

`race_l2f2_phase_c_batch(batch_index=k)` is evaluated against the ledger **as it stands**, not as
it stood when batch *k* closed. During a campaign the two coincide, because batch *k+1* is
materialized before any of it is decided. Afterwards they do not: replaying batch 0 against a
finished confirmation sees all fifty members and eliminates candidates the live campaign carried
further. That is the frozen rule behaving correctly — more observation narrows the racing bounds
and can never widen them, so elimination is monotone and no candidate is ever resurrected — and
the operational guarantee is unaffected: at the moment batch *k+1* was materialized, only
candidates surviving through batch *k* received jobs, and what actually happened to the queue is
durable in the queue. What it means is that a retrospective per-batch elimination list must not be
read as a record of what the control plane knew at the time. This is recorded, not changed; the
end-to-end suite asserts the invariant that *is* guaranteed — every candidate holds a whole number
of leading batches and nothing after them.

**The whole TRAIN chain is now proved end to end against real PostgreSQL.** One store holds a
complete Phase A, a complete 480-pair Phase B, and a TRAIN-complete Phase C — three plans
coexisting, with plan scoping pinned in all three directions — driven entirely through the
production persistence, materialization, least-privilege runner and evaluator boundaries. **No
Phase-C plan, job, GATK execution or score exists on the real baseline.**

---

---

## Canonical roadmap

```
Stage 0                          PASS — PROTOCOL-READY
  ↓
Stage 1                          PASS — TWIN-READY
  ↓
Layer 1                          PASS — L1-READY
  ↓
L2-A  Entry / contracts          PASS
  ↓
L2-B  PostgreSQL evidence        PASS — DB-READY
  ↓
L2-C  50/10/15 split             PASS — SPLIT-FROZEN
  ↓
L2-D  Profile ingestion          PASS — frozen profile snapshot
  ↓
L2-E  Feature infrastructure     PASS — FEATURE-VIEW-READY, FEATURE-MATRIX-FROZEN-1
  ↓
L2-F1 Experiment harness         PASS
  ↓
HARNESS-READY                    ISSUED
  ↓
L2-F2 Baseline discovery + qualification      ← CURRENT
  ↓
BASELINE-QUALIFIED               NOT ISSUED
  ↓
L2-G  Contextual learning        NOT STARTED
  ↓
MODELS-QUALIFIED                 NOT ISSUED
  ↓
L2-H  Controller                 NOT STARTED
  ↓
CONTROLLER-FROZEN                NOT ISSUED
  ↓
L2-I  Locked evaluation          NOT STARTED
  ↓
L2-READY / PRODUCTION-READY      NOT ISSUED
  ↓
L2-J  Production + delayed feedback           NOT STARTED
```

---

## Architectural stages vs implementation checkpoints

These are **not** the same thing, and conflating them has previously caused
roadmap drift.

* **Architectural stages** are the roadmap above (Stage 0, Stage 1, Layer 1,
  L2-A … L2-J). Each ends in a named, committed gate artifact.
* **Implementation checkpoints** are internal working increments *inside* one
  stage. `F1, F2, F3-A, F3-B, F3-C1, F3-C2, F3-D, F4, F5, F6, F7-A, F7-B`
  (including the R1/R2/R3 qualification attempts) were all checkpoints **inside
  L2-F1**. They are historical working markers, not roadmap stages, and no future
  document should treat them as such.

L2-F1 is **closed**. Do not open `F7-C`, `F7-D` or "F8 hardening". A new F7
increment is justified only by a demonstrated regression of an already-frozen
HARNESS invariant.

---

## Accepted gates

| Gate | Status | Hash |
|---|---|---|
| PROTOCOL-READY | PASS | `gates/protocol-ready.json` |
| TWIN-READY | PASS | `gates/twin-ready.json` |
| FEATURE-VIEW-READY | PASS | `c0ff49856689c994499dd3a7c04d7a1fb8ba0992b2eb1e099672bf828d515234` |
| FEATURE-MATRIX-FROZEN-1 | PASS | `cd34bdf96f3e7853039b2719e74a12a95740904c1b15f2f5c747516e0260d3ef` |
| **HARNESS-READY** | **PASS** | gate `0e8411ebffa9b6a27ec47cd896efd234bd60cdb30edf6f8f998ff8f06419fcc3` |
| BASELINE-QUALIFIED | not issued | — |

HARNESS-READY evidence:

* gate — `gates/harness-ready.json`
* qualification — `reports/layer2/harness-ready-result.json`
* qualification hash — `b1d1cc5d6a43520ba2b75cd27f3b4bdd70bbcc1b22845721853850c9fa7d3d09`
* qualifier — `l2f-harness-ready-qualification-v1/f7a-2`, 40/40 mandatory checks true
* qualified source — `488af0a8e8d49574fc301d9d5ea2ba2707704428`
* qualified tree — `71a6ae05d32835383405f26c378e3ca0787b062b`

The gate binds the **qualified source**, not the evidence commit that carries it.

---

## Accepted scientific identities (frozen)

| Identity | Value |
|---|---|
| Live GATK parameter space | `b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e` |
| Accepted policy | `a40321a676422121460bb110250812eacc8f1e203e788d244c661ec7c854daed` |
| Accepted candidate set (39) | `50d5f36918758de204e4b34cdd3fc8560a14debfcdb25869f713690c6085057d` |
| Accepted experiment plan (1950 logical jobs) | `eb8de84db2e35074957ed2f812cbb4f9495195cadb99563780d00d3cfe2b5d0a` |
| F5 DB/migration contract | `8b7d8e8961934f46d295646b4bc049bf118ba352c644d6e5d4d5d256dd201bdc` |
| GATK runtime bundle (4.5.0.0) | `2707ad203c82a9498fb2ffad8d97d8fbdb7e07a9bb963d0c4f4bc427e8372600` |
| Phase-A analysis (COMPLETE screen) | `25794987e49ca2a17776bf355326e8ff396366a6e3340fe7f9d2e24a855c80ae` |
| Phase-B candidate set (48) | `63b0244935edb46c799583cae9715733e52b25fba85f581a33ebe6949c09de0e` |
| Phase-B plan (480 logical jobs) | `e80594043580334ddf2504577e2fa030dff0c1217ac334804d9304a0ec72596b` |
| Phase-B completion (COMPLETE screen) | `0c98017a7a79bc7d8bf897983b4b25765d64a52d4f4caa70458abaf1d508e1fd` |
| Phase-C candidate set (10 promoted) | `923e45d59799c34ca1831c65b57604405165935a9e51d4c0e690abbfaf122bd4` |
| Phase-C plan (500-job ceiling) | `03b846e735e5817a8df7d5c37ae15778a955828a56513b16cef8ff2193a0aa43` |
| Execution environment (Phases A and B) | `71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3` |

DB-V2 is **abandoned** and stays abandoned. A future additive evaluation
migration is not permission to resurrect it.

---

## Canonical workspace rule

The canonical MINOS workspace is **`/home/hr/bittensor`**.

* Every new MINOS/SN107 runtime, dataset, database, qualification, artifact or
  temporary work directory is created beneath `/home/hr/bittensor`.
* No new MINOS-specific directory is created directly beneath `/home/hr`.
* Before using an existing path: inspect its realpath; decide whether it is
  MINOS-related; if it is, require its *physical* location beneath
  `/home/hr/bittensor`; leave unrelated user/application directories alone.
* `/home/hr/minos_f7b` is a **compatibility symlink** retained solely so the
  attempt-#1 forensic database's stored artifact URIs still resolve. It must
  never be a root for new work.

Canonical paths:

| | |
|---|---|
| Repository | `/home/hr/bittensor/minos_engine` |
| L2-F1 qualification workspace | `/home/hr/bittensor/minos_f7b` |
| Proposed L2-F2 baseline workspace | `/home/hr/bittensor/minos_l2f2_baseline` |

---

## Finding-severity policy

Every audit or review finding is classified into exactly one of these. This
policy exists to stop indefinite hardening loops on already-frozen contracts.

| Class | Meaning | Effect on the current stage |
|---|---|---|
| **BLOCKER** | Violates a mandatory stage invariant. | Must be fixed before promotion. |
| **DEFECT** | Actual implementation error. | Fix with focused regression tests. |
| **HARDENING** | Improvement, but the current frozen contract remains valid. | Backlog. **Does not block** the current stage. |
| **COSMETIC** | Docs, messages, refactors. | Batch cheaply. |

A HARDENING item never justifies reopening a closed stage.

---

## Validation cost policy

The full regression suite is the **CI's** job, not every local iteration's. This policy exists
because verification cost had grown quadratic in process: every commit paid a full serial suite
locally *and* again in CI, and CI itself ran the full suite twice (JUnit + coverage).

**Local, while iterating:** focused suites for the changed area, plus — always, because a
formatter-only red CI once cost a full task cycle — the exact commands CI runs first:
`ruff check .`, `ruff format --check .`, `mypy src`.

**Local, before a commit touching** migrations, CI, `conftest`, security grants, or accepted
identities: one full run.

**Database modes.** *Serial* runs may use either a shared persistent cluster
(`MINOS_DATABASE_URL` set) or ephemeral pgserver clusters (unset). *Parallel* runs REQUIRE
`MINOS_DATABASE_URL` to be **unset** — per-worker isolation exists only in ephemeral mode, where
each xdist worker process provisions its own pgserver cluster. Against a shared cluster the
workers would `DROP DATABASE … WITH (FORCE)` each other's fixed-name scratch databases, so the
conftest **fails closed** (`_require_xdist_isolation`) rather than silently corrupting results:

    env -u MINOS_DATABASE_URL pytest -n auto --dist loadscope ...

**CI stays serial** until a dedicated audit proves no cross-worker collision on shared service
resources (ports, roles, template names); parallelism is a local-iteration convenience, never
the authority.

**CI (the authority):** one full-suite invocation producing JUnit *and* the ≥90 % coverage gate
together. Coverage is a whole-suite property and is enforced only here.

**Infrastructure that keeps this cheap** (`tests/integration/layer2_db/conftest.py`):

* *Template cloning* — the first `alembic_upgrade` to a revision builds a template database by
  running the **real** migration chain once per session; every later fresh scratch database is
  `CREATE DATABASE … TEMPLATE` (~0.1 s instead of a full replay). Strictly fail-open: staged
  upgrades, downgrades, non-fresh databases and any error fall back to real Alembic, so
  correctness never depends on the optimization — migrations are still genuinely executed and
  inventoried every session. Cache identity is the **resolved concrete** Alembic revision
  (never the symbolic string `head`), so `head` and its explicit revision share one template and
  an ambiguous multi-head tree declines the fast path entirely.
* *Session-final template cleanup* — every template THIS process created is dropped at session
  end (pass **or** fail; `atexit` as last resort), so persistent clusters never accumulate
  `minos_tmpl_*` databases across runs. Cleanup drops only the process's own cached templates,
  never sweeps by name pattern, and is best-effort — it can never mask a test failure.
* *Ephemeral-cluster tuning* — throwaway pgserver clusters run with `synchronous_commit=off`;
  the CI service container is never touched.

Measured effect: the two most DB-heavy modules went **3 m 46 s → 1 m 39 s** wall (CPU
37 s → 17 s), and the complete serial suite with coverage went from the historical **60–90 min**
to **~20 min** — confirming the removed cost was migration replay and commit latency, not test
logic. One real interaction surfaced and was fixed generically: persistent template databases
pinned the MINOS roles during downgrade, so ``alembic_downgrade`` now drops the cluster's cached
templates first, restoring pre-optimization downgrade semantics exactly.

---

## Testing policy (three tiers)

| Tier | Scope | Cost |
|---|---|---|
| **Tier 1** | Pure contracts, hashes, candidate generation, objective math, statistics, serialization, tie-breaking, racing decisions. | cheap |
| **Tier 2** | Real PostgreSQL migration, FK/role permissions, evaluator persistence, content-addressed artifacts, filesystem modes, hap.py command construction, tiny deterministic fixture evaluation. | moderate |
| **Tier 3** | Real full-BAM GATK, real hap.py, practice truth, large corpus. | expensive |

Tier 3 runs **only** after Tier 1 and Tier 2 pass. Every production SQL **happy
path** must have at least one *positive* integration test — negative-only tests
are insufficient. (An L2-F1 defect reached a real GATK qualification precisely
because the only test of a production query exercised an early-return guard and
never executed the SQL.)

---

## Authoritative references

| Purpose | Document |
|---|---|
| Frozen specifications (never edited) | `docs/specifications/*.docx` |
| Current status (this file) | `docs/DEVELOPMENT_STATUS.md` |
| L2-F1 harness contract | `docs/layer2/EXPERIMENT_HARNESS.md` |
| L2-F2 proposed contract | `docs/layer2/BASELINE_QUALIFICATION.md` |
| L2-F2 pre-implementation audit | `reports/LAYER2_BASELINE_PREIMPLEMENTATION_AUDIT.md` |
| Historical Layer-2 audit (append-only) | `reports/LAYER2_PREIMPLEMENTATION_AUDIT.md` |

Historical reports are append-only evidence. Do not rewrite their tables; add
new sections or new reports instead.
