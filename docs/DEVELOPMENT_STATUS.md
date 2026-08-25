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
| Architectural stage | **L2-F2 — baseline discovery + baseline qualification** |
| Previous stage | L2-F1 — experiment harness — **CLOSED** at HARNESS-READY |
| Current implementation HEAD | verify from Git (`git rev-parse HEAD`) — deliberately **not** embedded here |
| Branch | `feature/L2-F` |
| HARNESS evidence commit (immutable anchor) | `107a745f2530b841ee3b399c81f3b2385cb6de2b` |
| L2-F2 pre-implementation-plan commit (immutable anchor) | `1536f73f0b0bbce0a514d56d2f47923945741762` |
| HARNESS historical Alembic head | `0008_l2f_execution_results` (stage-scoped; the repository head advances independently) |
| Operational DB revision | `0005_l2e_feature_view` |
| Next gate | **BASELINE-QUALIFIED** (designed in `docs/layer2/BASELINE_QUALIFICATION.md`, not implemented) |
| Current task | **L2-F2-C — MINOS-SUBNET-SCORING-ORACLE (source); canary environment prepared at `0012`, canary NOT run** |
| Previous task | L2-F2-B — baseline search protocol — **CLOSED** at PROTOCOL_FROZEN |
| Source Alembic head | `0013_l2f2_upstream_score_oracle` (migrations `0001`–`0013`) |
| Baseline DB revision required by the runner | `0013_l2f2_upstream_score_oracle` (exact; every other revision is refused) |
| Production score authority | pinned `minos-protocol/minos_subnet` @ `649bb92c…` — executed, not reimplemented |
| Scoring contract | `l2f2-minos-scoring-v2` (`b24a07e2…`); v1 `d6f29e11…` superseded, still recomputable |
| Real baseline DB revision | `0012_l2f_plan_member_source_idx` — migrating it to `0013` is the next environment task |

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

The evaluator service will need `MINOS_L2F_MINOS_SUBNET_ROOT` pointing at a detached pinned
worktree; see `docs/layer2/EVALUATOR_SERVICE_PROVISIONING.md`.

Running the canary requires the next environment task: migrate the real baseline database from
`0011` to `0012`, then replay preparation. `minos_runner_svc` is already provisioned with
`CONNECT`; the real store still holds 0 plans, 0 authorities, 0 jobs, 0 execution results and
0 evaluation results, and no GATK, hap.py or score has been produced.

**Not yet done, and not claimed:**

* the L2-F2-C canary has **not** run; no real hap.py or GATK evaluation has been performed;
* no baseline search plan, candidate set or job exists;
* `BASELINE-QUALIFIED` is **not** issued and the objective (D1–D8) is **not** frozen.

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
