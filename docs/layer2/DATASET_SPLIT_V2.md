# Layer 2 — Dataset Split v2 (Epoched, Growth-Stable)

**Status:** authoritative for SPLIT-FROZEN-V2. Append-only: this document is extended,
never rewritten. The v1 document (`DATASET_SPLIT.md`) remains the frozen historical
record of the accepted v1 fixed-count split.

## 1. Why v2 exists

The corpus grows in periodic batches (currently 75 practice rounds). The accepted v1
split partitioned by fixed count, which is only valid at exactly 75 samples: any growth
would force a full re-draw, silently moving accepted samples between partitions —
including test → train, which is evaluation leakage. v2 replaces the fixed count with a
fixed **ratio basis** (10 train / 2 validation / 3 test per 15 ≈ 66.7 / 13.3 / 20 %) and
an **epoched, grandfathered** assignment rule.

## 2. Core invariants (enforced by the verifier, the gate, and the database)

1. **Epoch-1 inheritance.** Epoch 1 inherits the accepted v1 per-sample partitions
   *verbatim*: zero assignment transitions; no accepted test or validation sample moves.
   The v2 salt is never applied to an accepted sample.
2. **Assignment immutability.** Once a sample has a partition in any epoch, that
   partition never changes in any later epoch (`origin_epoch` records where it was first
   assigned). The test set is monotonic: once test, always test.
3. **Additive growth only.** Epoch N+1 is a strict superset of epoch N. Sample removal
   and identity replacement are prohibited and rejected (verifier + persistence + the
   append-only triggers).
4. **New samples only are policy-assigned.** Genuinely new samples are ordered by
   `sha256("minos-l2-split-v2:" + round_id)` and fill the partition with the largest
   remaining per-chromosome deficit (largest-remainder targets from the ratio basis).
5. **Every epoch is a frozen snapshot** with its own `registry_snapshot_hash` (over the
   epoch's full identity set), bound to its parent by `parent_manifest_hash`,
   `parent_registry_snapshot_hash`, and a real `parent_snapshot_id` FK, and to v1 by the
   pinned `ancestor_v1_dataset_registry_hash`.

## 3. Epoch creation procedure (owner-authorized)

Creating an epoch is an explicit design decision — never automatic:

1. New rounds are registered in `catalog.dataset_registry` (full identity tuples).
2. `build_next_epoch_manifest(parent_manifest, new_samples)` produces the canonical
   epoch manifest (deterministic; regeneration is byte-identical).
3. `verify_epoch_manifest(manifest, parent_manifest=parent)` must pass in full
   (schema, hashes, counts, parent immutability, growth-only, zero transitions).
4. `storage.dataset_split_v2.persist_epoch` re-runs the complete verifier, resolves the
   parent snapshot FK, matches every sample on **all four** identity columns
   (`dataset_id`, `round_id`, `chromosome`, `identity_tuple_hash`), and writes the
   snapshot + allocations. Append-only triggers make the rows immutable; UNIQUE
   constraints make an epoch writable exactly once.
5. The epoch manifest is committed under `manifests/` and bound by the next gate
   regeneration. Acceptance of that gate freezes the epoch.

## 4. Active epoch

The **active epoch** is the highest epoch present in `catalog.split_snapshots` whose
manifest is committed and whose gate is accepted. Consumers (training, evaluation,
L2-D profile snapshots) must bind to an explicit epoch + `manifest_hash` +
`registry_snapshot_hash` — never "latest" implicitly.

## 5. Registry growth

`catalog.dataset_registry` is append-only. New identities are added for new rounds;
existing rows are never modified or deleted. Each epoch's `registry_snapshot_hash`
captures exactly the identity set visible to that epoch, so the frozen v1
`dataset_registry_hash` remains valid as the epoch-1 ancestor while later epochs carry
their own snapshot identity.

## 6. Model binding

Any trained model must record the epoch (and its `manifest_hash`) whose training
partition it consumed. Evaluation results are only comparable within the same epoch
lineage; a model trained on epoch N may be evaluated on the validation cohort of any
epoch ≥ N (the cohort can only have grown).

## 7. Sample-removal prohibition

Samples are never removed from an epoch lineage. If a sample is discovered to be
defective, it is handled by an explicit design decision recorded here (e.g. an exclusion list in a
future policy version) — the historical epochs remain byte-identical. Silent removal or
identity replacement fails verification (`no_parent_removed`,
`no_round_id_replacement`) and persistence.

## 8. Test exposure / consumption state machine

The test cohort is **SEALED** by default:

| State | Meaning | Database posture |
|---|---|---|
| `SEALED` (current) | No consumer may read any test allocation. | `evaluation.sealed_test_epoch_allocations` exists with **no grant**; no application role can select from it or the base tables. |
| `FINAL_EVAL_AUTHORIZED` | The controller is frozen and a final evaluation is explicitly authorized on a named epoch. | A separate, explicitly-authorized migration grants `SELECT` on the sealed view to `minos_evaluator` for that decision; the authorization commit records the epoch, the model identity, and the authorizing protocol decision. |

Transitioning to `FINAL_EVAL_AUTHORIZED` is out of scope for SPLIT-FROZEN-V2 and
requires its own explicitly authorized protocol change; nothing in this stage grants test access.
Consuming the test cohort **burns** it for the models evaluated; a new sealed cohort can
only come from newly-added samples in later epochs (which enter `test` per the ratio
policy and are sealed from birth).

## 9. Validation access

`evaluation.validation_epoch_allocations` (validation partition only) is readable by
`minos_evaluator` for model selection during development. The trainer never reads it.

## 10. Trainer view minimization

`catalog.training_epoch_allocations` (train partition only, `minos_trainer` only)
exposes **join/integrity identity fields only**: dataset id, chromosome, region
coordinates/hash, artifact sha256 digests, parameter-space/feature-registry hashes,
epoch, manifest hash, registry snapshot hash, partition, origin epoch, assignment
source. These are integrity/join fields — **not model features**. The ELIGIBLE-only
feature boundary is owned by L2-E; no profile values, scores, truth, or feature columns
ever appear in these views.

## 11. New sealed cohorts

New samples assigned to `test` in later epochs join the sealed cohort automatically:
the sealed view filters on partition, and no grant exists. No per-epoch action can
expose them.
