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
| Alembic head (source) | `0008_l2f_execution_results` (migrations `0001`–`0008` only) |
| Operational DB revision | `0005_l2e_feature_view` |
| Next gate | **BASELINE-QUALIFIED** (designed in `docs/layer2/BASELINE_QUALIFICATION.md`, not implemented) |
| Current task | L2-F2 pre-implementation audit — see `reports/LAYER2_BASELINE_PREIMPLEMENTATION_AUDIT.md` |

`select_config` remains deliberately blocked by `StageNotReadyError` and stays
blocked until L2-H.

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
