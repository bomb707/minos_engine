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

The policy is *derived*, not authored: `safe_controller_policy_content(root)` verifies both
campaign freezes and reads every identity from its owning module. No environment variable or
caller argument participates.

**Exactly one policy document is valid for an authority domain.** `verify_safe_controller_policy`
re-derives the whole document from the same root and requires equality field for field and nested
value for nested value; `load_committed_safe_controller_policy` runs that against the committed
bytes on every load. This corrects a real defect: the first implementation asserted a handful of
fields and required only that the two campaign-freeze identities be *non-empty*, and never looked
at `accepted_prerequisites`, `contextual_disabled_reason`, `refinement_disabled_scope`,
`parameter_space_schema` or `config_schema` at all. A document with a swapped freeze identity, an
edited nested prerequisite, a rewritten reason string, an unknown key or a missing key would have
verified. A unit test comparing committed bytes to the deriver does not substitute for this: it
proves something was true when the test last ran, not when the artifact is loaded.

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

## 4a. One authority domain

The entry gate, the committed policy, the baseline-selected authority, both L2-G freezes, the
accepted prerequisites and the git source provenance are all resolved against the **same**
explicitly passed `repo_root`. Mixing a caller-supplied root with an ambient `repository_root()`
would let a tampered copy borrow the real repository's authority for whichever checks it could not
satisfy itself, so a qualification copy is independently verifiable as a coherent domain.

`live_gatk_parameter_space()` is the one deliberate exception: it reads two fixed committed paths
from the installed source package and refuses caller-supplied documents by design. Making it
root-scoped would mean weakening that refusal, so it stays package-scoped and a test pins the
boundary rather than leaving it for a reader to discover.

The verified execution source commit and tree are minted **into** `VerifiedSafeBaselineAuthority`
from the root that was actually verified, and the decision manifest uses those values. Nothing
resolves a repository again after minting — a later global lookup could name a different checkout
than the one the capability attests to. No filesystem path enters the scientific identity.

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
that gap needed the ingest/admission surface (`layer2.ingest.validation.validate_admission`) and
the owning profile documents. **It is now closed — see §9.** The controller no longer accepts a
request whose round and profile it cannot trace to a frozen snapshot member.

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

## 9. Round and profile ownership (gap #20 — CLOSED)

Proving a `Layer1ProfileReference` internally consistent shows only that a caller can compute a
hash of its own four digests. Ownership is now established from committed, frozen, gate-backed
documents by `layer2/round_profile_authority.py` (`l2h-round-profile-ownership-v1`):

* `manifests/profile_snapshot_epoch1_members.json` (PROFILE-SNAPSHOT-FROZEN-1, snapshot
  `cf717ebb…`, registry snapshot `3e60aa65…`) owns each member's `round_id` / `dataset_id` /
  `profile_id` / `identity_tuple_hash` / `profile_manifest_sha256`;
* `manifests/profile_snapshot_epoch1_artifact_inventory.json` owns the byte SHA-256 and size of
  each member's four artifacts;
* each member's `bam-profile-v1`, `profile-manifest-v1` and `input-integrity-attestation-v1`
  documents are read from disk and required to hash to those recorded bytes **before** being
  parsed as anything.

**The accepted L2-D admission authority is reused, not reimplemented.**
`layer2.ingest.validation.validate_admission` is called unchanged for every member. What it cannot
supply on its own is the identity it validates against — it takes `registry_identity` as a plain
dict from its caller — so the registry identity is *reconstructed* from the member's own verified
attestation bytes and cross-checked against the frozen membership row. There is no caller-supplied
identity anywhere in the chain.

`require_owned_request` then proves nine fields against the owning record: `profile_id`,
`region_hash`, all four file digests, `identity_tuple_hash`, `fingerprint_hash` and
`profile_manifest_sha256`. Every mismatch is an authority failure.

**Partition sealing.** The frozen snapshot's 75 members span train (50), TEST (15) and VALIDATION
(10). TEST is sealed until L2-I *including identity enumeration*, and VALIDATION is not authorised
for v2, so a member outside `train` is skipped on its partition label before any artifact is
opened. Only a count survives, so the seal is auditable without enumerating what it covers. A test
deletes every sealed member's directory and confirms the TRAIN corpus still loads — a stronger
proof than asserting the loader does not touch them.

Layer 2 still does not open the BAM.

### A contract defect, recorded rather than reinterpreted

`Layer1ProfileReference.profile_manifest_hash` **has no canonical definition anywhere in this
engine.** Nothing computes it, no manifest carries a field of that name, and its validator checks
only that it is 64 lowercase hex characters. The nearest real quantity is a *different* one:
`profile_manifest_sha256`, the SHA-256 of the owning manifest document's exact bytes, computed by
`storage/profile_ingest.py` and recorded per member in the frozen snapshot.

Silently deciding that the old field "means" the new one would be inventing a preimage nobody ever
defined. So the smallest explicit typed correction was made instead: `profile_manifest_sha256` is
added to the contract under its own name with an exact definition, the ownership authority requires
it, and `profile_manifest_hash` is documented in the contract as unauthenticated and **removed from
the decision manifest** — an opaque value with no definition has no place in a scientific identity.

## 9a. Decision persistence (gap #21 — CLOSED as an explicit deferral)

**Determination: OUTCOME B, scoped to the pre-activation stage.**

The v2 build specification's §15 live decision algorithm does require persistence before the
return — `return persist(SAFE_BASELINE)` on the degraded path, and
`return exact canonical CONFIG bytes referenced by persisted hash` on the main one. That binds an
*activated* controller. This one is not activated: `select_config` still raises, so there is no
live round and no caller receiving a config.

The database route is blocked structurally, not by preference:

* `runtime.decisions` is a strict subset of the table the specification describes — no `mode`, no
  `fallback_reason`, no `controller_version`, no `parameter_space_hash`, no `decision_manifest`,
  no `decided_at`, and `profile_id` nullable — and its `config_id`/`profile_id` foreign keys point
  at `catalog.gatk_configs` and `profiling.profiles`, which hold **0 rows**.
* Alembic is one strictly linear chain with no branch labels anywhere. The operational store is at
  `0005_l2e_feature_view`; the chain runs to `0026`, and every revision from `0006` on is an L2-F2
  scientific-campaign migration. A new controller revision could only be reached by advancing the
  operational database through **twenty-one unrelated scientific migrations**, which is explicitly
  forbidden. Introducing a branch-labelled chain to dodge that is a far larger architectural change
  than this task authorises.

`docs/layer2/L2H_SAFE_CONTROLLER.md` §10 check 21 accepts "an explicit decision that decisions are
not persisted" as a closure. **This is that decision, and it is a deferral, not a waiver:** database
persistence remains a prerequisite for activating the public service, and the single-chain
migration topology is the impediment that must be resolved first. "The table is inconvenient" is
not the reason and would not be a good one.

Meanwhile `layer2/decision_publication.py` publishes decisions the way this engine publishes all
its other evidence: canonical bytes under a content-addressed name
(`<identity>.json`, mode `0640`), staged in-directory, fsynced, atomically renamed, the directory
fsynced, then **read back from the final path** before the call returns. `decide_and_publish`
performs that publication before returning, so no caller can observe a decision that is not already
durable — the property `persist(...)`-before-`return` exists to give.

Idempotency is a property of the naming rather than a protocol: the same semantic decision has the
same identity, hence the same path and the same bytes, so a retry converges and returns
`reused=True`. Two different decisions can never contend for one name, and republishing different
bytes under an existing identity is refused outright — a decision identity may never name two
decisions.

**No migration is proposed or applied. No database was written to.**

## 10. Controller qualification — mandatory checks (§16)

**Run.** `layer2/safe_controller_qualification.py` (`l2h-safe-controller-qualification-v1`) drives
the real decision path over the real owned corpus and derives every verdict from observations it
made itself; `TrustedSafeControllerQualification` can only be minted by running one, so a caller
cannot pass `{"check": true}` and manufacture a PASS. `verify_qualification_report` re-derives
every check from the report's own observations and refuses a status that disagrees with them.

The 21 mandatory checks below are unchanged and all passed. Six further checks were added:
`owned_corpus_admitted_by_accepted_authority`, `ownership_failures_are_authority_failures`,
`model_bundle_id_cannot_influence_the_config`,
`publication_is_content_addressed_and_idempotent`, `select_config_public_boundary_blocked`, and
`sealed_partitions_never_enumerated`.

**That result must not be called MODELS-QUALIFIED.**

## 11. Gate naming and capability scope (§17)

No gate is issued in this task. The naming decision is recorded now so it cannot drift later:

| Gate | Capability implied | Requires |
|---|---|---|
| `CONTROLLER-FROZEN` | full contextual control, including `FULL_CONTEXTUAL` | **MODELS-QUALIFIED PASS** — therefore unavailable |
| `SAFE-CONTROLLER-FROZEN` | `SAFE_BASELINE` only, no learned inference | BASELINE-QUALIFIED PASS **+** L2-G terminal contextual-model HOLD/freeze |

`CONTROLLER-FROZEN` would name a capability this controller does not possess, so it is reserved.
`SAFE-CONTROLLER-FROZEN` is now **registered** in `gates/required_checks.py` with 26 required
checks, every one of which the qualifier produces — and it is deliberately **not issued**. Four of
its own required checks (`allowed_modes_exactly_safe_baseline`, `zero_contextual_model_loads`,
`models_qualified_remains_hold`, `select_config_public_boundary_blocked`) assert the absence of
contextual capability, so the gate cannot be read as implying any.

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

## 13. Qualification result

`reports/layer2/l2h-safe-controller-qualification-v1.json` — schema
`l2h-safe-controller-qualification-v1`, domain `minos:l2h-safe-controller-qualification:v1\n`,
identity **`7d305bcd7c35c82389259ec1d88058ff9202ce454a0364d15e4b864345aaf821`**, file SHA
`b0a3a16d2a411d18e708ef6add63a6832778e51cfb8454874763a0f5c8604453`, 6046 bytes. Produced from
source commit `9a516c1738b862aa78ce211c03891b57f5444deb`.

**Status PASS.** 50 TRAIN profiles × 4 requested modes = **200 decisions**, every one selecting
`157d88d1…`; 50 `NONE` and 150 `SAFE_BASELINE_FORCED`; 0 invalid configs, 0 model loads, 0
candidate generations, 0 parameter mutations. Thirteen authority-failure drills all failed closed.
All 21 mandatory checks and all 6 additional checks true.

The report records no timestamp, host, PID, path or credential — an identity that changed every
run would not be an identity. It records `gate_issued: false` and `service_activated: false`, and
its verifier refuses any report whose recorded checks disagree with its own observations, whose
status disagrees with its checks, or which claims a contextual model qualification.

**This is not MODELS-QUALIFIED and cannot become it.** Four of its own checks assert the absence of
contextual capability. No gate artifact was issued, and `Layer2Service.select_config` still raises
`StageNotReadyError`.
