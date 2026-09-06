# L2-H — SAFE_BASELINE-only controller: source authority

Companion to [ADR-0005](../decisions/ADR-0005-SAFE-BASELINE-ONLY-CONTROL.md). This document
records the source authority created in L2-H, the audits performed, and the checks a future
controller qualification must pass. **No gate is issued here, and the public
`Layer2Service.select_config` boundary remains blocked.**

## 1. What this is, and what it is not

L2-G v1 and v2 both closed with an empty TRAIN shortlist. The safe controller is **not** a failed
model repackaged as a qualified one. `SAFE_BASELINE` was always a control mode and always the
fail-safe; what changed is that it is now the *only* authorizable one, because the other three
require a qualified model bundle and none exists.

Concretely, this controller returns no learned prediction and changes no GATK parameter relative
to the qualified baseline: the config it emits is the byte-identical, already-qualified
`157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea` payload.

## 2. The policy

`reports/layer2/l2h-safe-controller-policy-v1.json`, schema `l2h-safe-controller-policy-v1`,
domain `minos:l2h-safe-controller-policy:v1\n`, policy hash
**`638d634834c921f5ba00220caaca59c4b317368767c38bd50cafaad232241fa3`**.

`allowed_modes` is exactly `["SAFE_BASELINE"]`; `disabled_modes` is exactly the other three, with
`refinement_disabled_scope = CONFIG_SELECTION`. The policy binds both L2-G freeze identities, the
BASELINE-QUALIFIED gate and qualification hashes, the baseline-selected authority identity, the
safe baseline config hash, the live parameter-space identity and schema, the full accepted
prerequisite identity, `models_qualified_status`, `model_bundle_load_authorized = false` and
`select_config_public_boundary = BLOCKED`.

The policy is *derived*, not authored: `safe_controller_policy_content()` verifies both campaign
freezes and reads every identity from its owning module. No environment variable or caller
argument participates.

## 3. Baseline authority (not copied from anywhere)

The baseline is resolved through `baseline.baseline_selected.load_committed_baseline_selected()`,
whose document must hash to the bound `baseline_selected_identity`
(`b13aef13fecf8e966184d03bad5ee0e6f096fb5649b30e336283e2f50f3eba38`) and whose
`selected_config_hash` must equal the module constant. The payload at
`<config_artifacts>/157d88d1….json` is then required to be a non-symlink whose bytes are canonical
and hash to that same config hash. No new baseline is created and none is derivable here.

## 4. Parameter-space compatibility rule

A frozen old baseline is **not** legal forever. Before any decision, the controller requires:

- the live parameter space `live_gatk_parameter_space()` to still be
  `b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e`, the identity the policy binds;
- `canonicalize_live_gatk_config(payload)` to leave the config hash **unchanged**;
- `effective_config == requested` — every fixed/protocol field preserved;
- the canonical config's parameter-space hash to equal the live one.

If the space moves, or canonicalisation would rewrite any field, the controller raises rather than
emitting. **The baseline is never silently mutated to fit new ranges**; a new compatibility domain
requires explicit requalification.

## 5. Entry gate

`layer2.entry_gate.verify_l2_entry_gate(EntryGateRequest(repo_root=...))` — the repository-owned
verifier, all 34 checks, no duplication and no caller-supplied accepted hashes. Layer 2 still does
not open the BAM.

**A scope limit, stated rather than glossed.** That verifier proves the L1-READY gate identity and
git ancestry. Profile identity is enforced only to the extent the contract enforces it:
`Layer1ProfileReference` recomputes `identity_tuple_hash` from the BAM/BAI/reference/FAI/region
hashes and rejects a mismatch, so a request cannot carry an internally inconsistent profile. It is
**not** cross-checked against an external profile manifest, and round/region identity is not
cross-checked against a round-context document, because `DecisionRequest` carries neither. Closing
that gap needs the ingest/admission surface (`layer2.ingest.validation.validate_admission`) and a
profile document alongside the request. It is listed as a mandatory check below so the next task
cannot inherit it silently.

## 6. Two failure classes, kept apart

| Class | Trigger | Behaviour |
|---|---|---|
| **Global authority failure** | entry gate invalid · baseline authority does not resolve or mis-hashes · payload byte/hash mismatch · non-canonical payload · symlink · parameter-space identity moved · canonicalisation rewrites the config · request names a foreign baseline or parameter space | raise `SafeControllerAuthorityError`, **emit nothing** |
| **Round-level reduced authority** | contextual mode requested but unavailable · safe mode requested directly | return the **verified** baseline with a typed fallback reason |

Emitting an unauthenticated CONFIG because "the safe path should always work" would make the safe
path the least trustworthy one, so authority corruption is never treated as an ordinary fallback.

## 7. Decision semantics

| Requested mode | Actual mode | Fallback reason |
|---|---|---|
| `SAFE_BASELINE` | `SAFE_BASELINE` | `NONE` |
| `BOUNDED` | `SAFE_BASELINE` | `SAFE_BASELINE_FORCED` |
| `FULL_CONTEXTUAL` | `SAFE_BASELINE` | `SAFE_BASELINE_FORCED` |
| `REFINEMENT` | `SAFE_BASELINE` | `SAFE_BASELINE_FORCED` |

`BASELINE_GATE_FAILED` is deliberately **not** used: it would claim a baseline-improvement
comparison happened, and none did — no model was ever loaded to make one.

`model_bundle_id` is optional and never required. If a caller supplies one it is **not loaded**;
the manifest records only `model_bundle_id_present` (a boolean), never the identifier itself, so
no model handle can influence either the selected CONFIG or the scientific decision identity. Two
requests differing only in `model_bundle_id` produce the same decision identity — deliberately.

## 8. Decision manifest

Schema `l2h-safe-decision-manifest-v1`, domain `minos:l2h-safe-decision-manifest:v1\n`. It binds
round identity; profile id, manifest, fingerprint, identity-tuple and region hashes;
parameter-space hash and caller; baseline authority identity, BASELINE-QUALIFIED gate hash,
baseline config hash and payload SHA; selected config hash; controller policy hash and controller
version (plus the caller's declared version); requested vs actual mode; fallback reason;
`model_bundle_id_present` and `model_bundle_loaded = false`; MODELS-QUALIFIED status; the L2-G v2
freeze identity; `contextual_research_closed`; the guard results; the accepted prerequisite
identity; and the execution source commit/tree.

It contains **no** truth, mutation identity, hap.py output, score, VALIDATION outcome or TEST
outcome, and **no operational timestamps** — identical semantic input yields an identical decision
identity, which is the only thing that makes the identity meaningful.

## 9. Persistence audit (§14) — surface exists, schema is insufficient

`runtime.decisions` already exists (model `storage/models/runtime.py`, migration
`0001_l2b_initial`), is append-only via the `audit.minos_reject_mutation` trigger, is granted
`SELECT, INSERT` to `minos_live`, and currently holds **0 rows**. Its columns are:

`id`, `round_id`, `decision_hash`, `decision_manifest_hash`, `config_id` (FK
`catalog.gatk_configs`), `model_bundle_id` (FK `models.model_bundles`), `profile_id` (FK
`profiling.profiles`), `created_at`.

**It can already store** round, decision identity, manifest hash, and — once the referenced rows
exist — config and profile references.

**It cannot store**, and a controller write path would need: `mode` (`ControlMode`),
`fallback_reason` (`FallbackReason`), `controller_version`, `controller_policy_hash`,
`parameter_space_hash`, and the baseline authority identity. Two further operational facts: the
operational store is at `0005_l2e_feature_view`, and `catalog.gatk_configs` and
`profiling.profiles` are both empty, so the `config_id`/`profile_id` foreign keys have no rows to
point at yet.

**No migration is proposed or applied in this task.** The roadmap does not authorize one here, and
inventing a schema for a controller that has not been qualified would fix the wrong shape first.
This is the exact gap the next task must decide on.

## 10. Future controller qualification — mandatory checks (§16)

Not run here; this is the inventory the next task must satisfy.

1. Exact L1 entry authority — all 34 entry-gate checks true.
2. Exact BASELINE-QUALIFIED authority — gate hash, qualification hash, baseline-selected identity.
3. Exact L2-G v2 closure identity `42310a97…` and v1 `1c2039de…`.
4. MODELS-QUALIFIED remains HOLD; no `gates/models-qualified.json` exists.
5. Allowed mode set is exactly `{SAFE_BASELINE}`.
6. 100% of decisions select the accepted safe baseline.
7. 0 invalid CONFIGs emitted.
8. 0 contextual model loads.
9. 0 candidate generation events.
10. 0 parameter mutations — emitted config byte-identical to the qualified payload.
11. Decision manifest is canonical and deterministic.
12. Same semantic request → same decision identity.
13. Every contextual request → typed `SAFE_BASELINE_FORCED` fallback.
14. Corrupted global authority → fail closed, nothing emitted.
15. Baseline payload tamper → fail closed.
16. Parameter-space mismatch → fail closed.
17. Low remaining time → still a deterministic safe result while authority is valid.
18. No truth, VALIDATION or TEST dependency anywhere in the path.
19. Fallback success = 100%.
20. **Profile and round/region identity cross-checked against their owning documents**, not only
    against the request's own internal consistency — the scope limit recorded in §5.
21. Decision persistence path closed (§9) or an explicit decision that decisions are not persisted.

**That result must not be called MODELS-QUALIFIED.**

## 11. Gate naming and capability scope (§17)

No gate is issued in this task. The naming decision is recorded now so it cannot drift later:

| Gate | Capability implied | Requires |
|---|---|---|
| `CONTROLLER-FROZEN` | full contextual control, including `FULL_CONTEXTUAL` | **MODELS-QUALIFIED PASS** — therefore unavailable |
| `SAFE-CONTROLLER-FROZEN` | `SAFE_BASELINE` only, no learned inference | BASELINE-QUALIFIED PASS **+** L2-G terminal contextual-model HOLD/freeze |

`CONTROLLER-FROZEN` would name a capability this controller does not possess, so it is reserved.
The mode-scoped name is designed but deliberately **not issued**.

The existing gate schema (`schemas/gate-artifact-v1.schema.json`, `additionalProperties: false`)
has **no capability-scope field**, and adding one would change `gate_hash` for every gate already
issued. Scope is therefore expressed the way the repository already expresses it — through the
gate name plus its registered required-check set in `gates/required_checks.py`. Reusing that
convention is preferable to a schema change that would perturb frozen artifacts.

## 12. Locks

VALIDATION is not read here and stays at `0026`; the v2 freeze records
`validation_authorized_for_v2 = false`, and no controller threshold is drawn from VALIDATION. TEST
stays sealed for L2-I. MODELS-QUALIFIED is absent. `Layer2Service.select_config` still raises
`StageNotReadyError` — a required check in three registered gates and asserted by seven runners —
and this task does not touch it.
