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

## 9. Round and profile ownership (gap #20 — CLOSED, corrected)

**The first attempt breached the seal it claimed to honour.** v1 of this authority read the whole
75-member profile snapshot, parsed it, and iterated every record before skipping non-TRAIN members.
TEST is sealed until L2-I *including identity enumeration*, so reading all 75 and filtering was
itself the breach — and the v1 report's `skipped_partition_counts` were derived by traversing the
very records they claimed were untouched. A count of skipped members proves nothing about
non-access. `l2h-safe-controller-qualification-v1.json` is preserved as history and is not valid
for qualification.

`l2h-round-profile-ownership-v2` never opens a document carrying a sealed identity. The TRAIN round
list comes from `manifests/l2f2_train_schedule_v1.json` — the frozen TRAIN-ONLY projection the
accepted L2-F2 campaign already ran on: 50 entries of `{chromosome, dataset_id, round_id}`, no
sealed row.

**The schedule is not trusted for being present, and neither is the authority that names it.**
The Phase-A execution authority's identity is *derived* from authority that is already accepted,
never copied out of the document being checked:

1. `BASELINE_QUALIFIED_GATE_HASH` is an accepted source constant;
2. `gates/baseline-qualified.json` must recompute to exactly that hash, and `compute_hash` covers
   `qualified_source_git_sha` / `qualified_source_tree_sha` — so the accepted gate cryptographically
   pins commit `9395c116e22c52777441d76200acd96a738417bf` (tree `fe831428…`);
3. at that commit the git blob for `manifests/l2f2_phase_a_execution_authority_v1.json` is
   immutable and already part of the accepted L2-F2 chain; its `content` re-hashed under
   `minos:l2f2-phase-a-execution-authority:v1\n` gives the accepted identity
   **`9ad0ba48c80e7b305505fea201e93185deb15ae735338086d05b38afcf4deb3f`**;
4. the working Phase-A document must re-hash to its own declared `authority_hash` **and** that
   value must equal the accepted identity;
5. only then are its `train_schedule_manifest_sha256 = 694a8993…` and
   `split_manifest_sha256 = ffdd3195…` believed, and the schedule's bytes must hash to the former;
6. the schedule's declared shape must equal the **source constants** in `baseline/schedule.py`.

Step 4 is the correction. v2 recorded the Phase-A authority hash rather than verifying it, so a
coherent rewrite of the authority *and* the schedule together — recomputing the schedule SHA, the
authority's reference to it, and the authority's own self hash — survived the authority layer, and
rejection fell to whichever per-member attestation happened to be reached later. That is not
authentication of the schedule. A test now performs exactly that forgery, reordering real admitted
bindings so nothing downstream can catch it, and requires refusal at the anchor before any corpus
member is opened.

`build_phase_a_authority` is deliberately not called: it reaches the split manifest.

Recomputing the protocol via `build_baseline_protocol` would have hashed the split manifest, which
carries sealed rows. The seal guard caught exactly that during development, which is why the anchor
runs through `baseline_selected_content()` instead.

Per-member identity then comes from the member's own three documents, opened **by name** from the
50 scheduled round ids. The corpus directory is never listed, so a sealed directory is never even
observed to exist. Byte integrity needs no artifact inventory: the accepted L2-D admission
authority binds the manifest to the profile and windows bytes, the attestation re-hashes to its own
`attestation_hash`, its identity tuple recomputes, and its `registry_snapshot_hash` must equal the
accepted `PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH` constant.

`validate_admission` is called unchanged. What it cannot supply on its own is the identity it
validates against — it takes `registry_identity` as a caller dict — so that identity is
reconstructed from the member's own verified attestation and cross-checked against the anchored
schedule row.

**Isolation is enforced and observed, not asserted.** `layer2/sealed_access_guard.py` patches
`io.open` for the duration of a qualification and refuses any open of a sealed-identity-bearing
authority, counting attempts. The qualification derives its isolation checks from that counter;
`test_accessed: false` as a constant would prove nothing. Tests confirm the guard actually fires,
that ownership loads with every sealed authority deleted, and that it loads with the 25
unscheduled corpus directories removed entirely.

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
its other evidence: canonical bytes under a content-addressed name (`<identity>.json`, mode
`0640`), staged in-directory, fsynced, linked into place, the directory fsynced, then **read back
from the final path** before the call returns. `decide_and_publish` performs that publication
before returning, so no caller can observe a decision that is not already durable.

Two corrections were made here. The identity is now **derived, not accepted**: a manifest must hash
to the name it is filed under, so a caller cannot publish arbitrary canonical bytes under an
unrelated 64-hex name. And creation is now **no-clobber** — `os.link` rather than check-then-
`os.replace`, which was overwrite-capable: two writers could both see the target absent and both
rename onto it, losing one writer's bytes silently. `link()` is atomic across processes, so exactly
one writer creates the record and the rest converge on it; a process-local lock would not do,
because nothing says one process publishes. Same identity and same bytes converge (`reused=True`);
same identity and different bytes fail closed. A 24-thread test confirms exactly one creation, one
file, and no staged remnant.

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

`reports/layer2/l2h-safe-controller-qualification-v2.json` — schema
`l2h-safe-controller-qualification-v2`, domain `minos:l2h-safe-controller-qualification:v2\n`,
identity **`8408630ffb130afeb22bf08dc47b78f3be5bbeef3dc102c2c3b5265f3431d286`**, file SHA
`483f77c63938331b4930439a244e8db8992049d7a4fab75faf76629ed5975e9d`, 7544 bytes. Produced from
source commit `23b360562b317467a542ee722341fc2d0931bfed`. Ownership corpus identity
`dd1bd330b859f28f6617b05702ef80b159e863d4cd67c5610210d897bd82f307`.

**Status PASS.** 50 anchored TRAIN profiles × 4 requested modes = **200 decisions**, every one
selecting `157d88d1…`; 50 `NONE` and 150 `SAFE_BASELINE_FORCED`; 0 invalid configs, 0 model loads,
0 candidate generations, 0 parameter mutations. All 21 mandatory checks and all 8 additional
checks true, including `sealed_authorities_never_opened`,
`train_ownership_anchored_to_accepted_authority` and
`publication_identity_is_derived_not_declared`.

`forbidden_sealed_path_open_attempts = 0` and `attempted_sealed_authorities = []` are **observed
by the guard**, not written as constants, and the verifier refuses a report whose isolation flags
contradict them. The report deliberately no longer carries `skipped_partition_counts`: counts
derived by traversing sealed records are precisely what v1 got wrong.

**`l2h-safe-controller-qualification-v1.json` (`7d305bcd…`) is preserved as history and is not
valid for qualification.** The v2 verifier refuses it on schema, and the v2 report records the
reason in its own `supersedes` block. A future SAFE-CONTROLLER-FROZEN gate must be issued against
v2 evidence.

**This is not MODELS-QUALIFIED and cannot become it.** Four of its own checks assert the absence of
contextual capability. No gate artifact was issued, and `Layer2Service.select_config` still raises
`StageNotReadyError`.
