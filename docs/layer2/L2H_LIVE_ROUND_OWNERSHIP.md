# L2-H — LIVE round / profile ownership

`l2h-round-profile-ownership-v2` proves that a request names one of the **fifty frozen TRAIN
rounds**. That is correct for a qualification campaign and wrong for production: a live SN107
challenge is not in that schedule, so it could not obtain the capability
`select_safe_baseline` requires, and no live round could reach the controller at all.

This document is the source authority that closes that. It **does not** activate the service,
reissue any gate, or provision the SAFE config row.

---

## 1. The upstream authority is not ours

Traced from repository source and the local subnet integration
(`minos_subnet/utils/platform_client.py`, `neurons/miner.py`):

| | |
|---|---|
| endpoint | `POST /v2/round-status` (authenticated with the miner hotkey signature) |
| creates the round | **the platform**, not MINOS |
| `round_id` | **an ISO-8601 timestamp**, e.g. `2026-01-21T12:00:00.000000+00:00`; the subnet's own `validate_round_id` parses it with `datetime.fromisoformat` and caps it at 40 characters |
| stable / replayable | yes — the platform reissues the same id for the same round |
| region | a string such as `chr20:45000000-50000000` |
| BAM / BAI | **presigned URLs**, not content hashes |
| reference / FAI | **local**, `datasets/reference/<chrom>/<chrom>.fa` |
| also returned | `status`, timing, `num_mutations`, `downsampled_coverage` |

So **no round-id scheme is invented here.** Two upstream facts shape everything below:

* **The platform's round id is a timestamp.** That is a fact about the upstream identifier, not a
  decision to put wall-clock metadata into a scientific identity. What is excluded from the
  identity is anything describing *this run* — retrieval time, host, pid, path, URL — and none of
  it appears. The round id is *included* precisely because two different rounds may present the
  same inputs; without it, a decision for one round could be replayed as a decision for another.
* **The platform states URLs, not bytes.** Nothing upstream says what the BAM's content is, so
  identity is computed locally from the downloaded bytes — which is what the intake producer
  already does — and it is those computed identities that are bound.

**One pre-existing contract had to widen.** `InputIntegrityAttestation.round_id` was
`^[0-9a-f]+$`, written when every round the engine had seen was a research round. A live
attestation was therefore *inexpressible*. The rule now lives once, in
`intake.contracts.validate_round_identifier`, and accepts lowercase hex **or** a strict ISO-8601
timestamp, rejects anything longer than 40 characters, and rejects separators, traversal
fragments and whitespace — the same security reasoning the subnet applies, for the same reason
plus one more: a round id lands in `runtime.decisions.round_id`. Hex is a strict subset of what
was accepted before, so **no existing artifact changes**.

## 2. Three authorities, kept apart

| | scope | admissible because |
|---|---|---|
| `l2h-round-profile-ownership-v2` | TRAIN, 50 frozen rounds | it is in the frozen schedule, anchored to Phase-A/split/registry authority |
| `l2h-live-round-intake-v1` | one live challenge | its inputs authenticate |
| `l2h-live-round-profile-ownership-v1` | one live challenge | its L1 profile provably belongs to that intake |

The TRAIN loader is **untouched** and still anchors to exactly what it anchored to before. Its
corpus identity is still `9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03`.

## 3. The live intake identity

`sha256("minos:l2h-live-round-intake:v1\n" + canonical_json_bytes(content))` over a **closed**
field set — a missing field and an unknown field are both refusals:

```
round_id, chromosome, region_source, region_coordinate_system,
region_start0, region_end0_exclusive, region_length_bp, region_hash,
bam_sha256, bai_sha256, reference_sha256, fai_sha256,
identity_tuple_hash, schema_version
```

The region is parsed once, under an explicit convention, by the same `Region` contract Layer 1
uses. `identity_tuple_hash` is exactly the tuple the L2-D attestation recomputes, so the two
cannot disagree silently. Verification **re-derives the whole document from its own inputs**: a
declared region hash or identity tuple that disagrees with the bytes it describes is not evidence.

`parameter_space_hash` is deliberately **absent**. It defines what the controller may *do*, not
what the round *is*, and the controller already checks it against the accepted live space. Binding
it here would break the profile binding whenever the parameter space moved even though the inputs
had not.

`dataset_id` is derived, not invented: `live-<chromosome>-<identity_tuple_hash[:16]>`, named so it
can never be mistaken for a registry-backed research id like `minos-chr21-0279a3b8042f848b`.

**`registry_snapshot_hash` keeps its meaning.** For a research round it is the identity of the
`catalog.dataset_registry` snapshot the attestation was matched against. A live round has no
registry, and the document registering what its inputs are *is* the intake — so the intake's
identity takes that role. The field still means "the identity of the registered input set this
attestation was checked against"; only which document registers it differs. That is a change of
scope, not of meaning, and it is stated rather than assumed.

## 4. The profile binding

`load_verified_live_round_ownership` takes the four Layer 1 outputs as **bytes** — a caller
handing over a dict has already decided what the bytes mean — and proves, recomputing each link:

```
verified live intake
    == the attestation's declared inputs      (all ten registry-identity fields)
    == the profile manifest's region and artifact byte hashes
    == the profile document's own identity     (via validate_admission, unchanged)
    == the DecisionRequest's profile reference (via require_owned_request)
```

The attestation must hash to its own `attestation_hash` and its identity tuple must recompute.
The accepted L2-D admission authority `validate_admission` is **called unchanged, not
reimplemented**, and the `registry_identity` it validates against is reconstructed from the
verified intake, so no caller is on the trust path. Any mismatch raises; nothing is emitted.

**One stated exception.** The manifest's `fingerprint_hash` cannot be re-derived here: rebuilding
it needs L1's sampling-plan and read-filter-policy identities, and Layer 2 may not import
`layer1.fingerprint` at all (the boundary the leakage suite enforces). What *is* re-derived is
`profile_manifest_sha256`, the exact bytes of the manifest that declared it. The frozen TRAIN
authority carries the field on exactly the same terms.

## 5. Round-scoped, and still unforgeable

The live factory takes one intake and one profile and mints an authority owning **exactly one
round**. There is no history to load and no row to pick — `len(ownership) == 1`.

The controller's `isinstance(ownership, VerifiedRoundProfileAuthority)` check is **unchanged**;
it was not weakened to a `Protocol` or to duck typing. What changed is that there is now a second
way to earn the module-private token, via `mint_verified_ownership`, which is the whole of the
widening and is reachable only from the two verifying factories. A dictionary — or an object that
merely implements `owned`/`require_owned_request` — is still not an authority, and a test proves
the controller refuses a lookalike.

Scope is **derived** from the partition rather than stored, so the TRAIN corpus identity that the
accepted controller qualification binds cannot move because a new scope exists.

## 6. Decision-manifest v1 or v2: **v2 is required**

Three fields of `l2h-safe-decision-manifest-v1` would change meaning without changing name:

* **`profile_ownership_anchors`** — the TRAIN campaign's anchors: Phase-A authority, TRAIN
  schedule SHA, split manifest SHA, registry snapshot, baseline protocol. **None of these exist
  for a live round.**
* **`profile_corpus_identity`** — the identity of the fifty-member corpus a decision was admitted
  against. A live round has no corpus; a round-scoped authority has one member.
* **`dataset_id`** — a registered research dataset. A live round has no registry row.

So v1 is **not** retained. `safe_decision_manifest_content` now refuses a live-scoped ownership
authority with a typed error naming manifest v2, rather than emitting a v1 document with live
values in TRAIN-shaped fields. v2 itself is deliberately **not defined here**: defining it is
controller science and belongs with the requalification that must follow.

**Consequences, stated rather than deferred quietly:**

* `safe_controller.py` and `round_profile_authority.py` changed, so the source underlying
  SAFE-CONTROLLER-FROZEN (`7d064fe8…`) changed. **The existing gate does not authorize the changed
  source.** It remains accepted for the historical S3 source; the new LIVE-capable controller is
  **not frozen** and needs its own qualification and gate reissuance.
* `r0001`'s `ck_decisions_manifest_schema` pins `l2h-safe-decision-manifest-v1`, so **an `r0003`
  will be required** once manifest v2 exists. The accepted persistence v2 is accepted **for
  manifest v1 only**; whether it remains reusable must be demonstrated then, not assumed now.
  Nothing here modifies `r0001`, `r0002`, the persistence v2 report or its acceptance constants.

## 7. Operational storage: a new surface is required

Audited, not assumed:

* `ingest_profile` resolves identity **through the split epoch's allocation membership**
  (`_epoch_member_identity` joins `catalog.split_epoch_allocations`), and fails closed when the
  dataset is not allocated in that epoch. A live round has no allocation. **That path is correct
  and is not weakened.**
* `catalog.dataset_registry` requires `split_algorithm_version`, `split_salt`,
  `allocation_digest`, `manifest_hash`, `feature_registry_hash` and `parameter_space_hash`
  `NOT NULL`. Filling those for a live round would mean inventing split/allocation values, which
  is exactly the overloading that must not happen.

So **option B**: a separate operational live-intake/profile registration surface is required.
Sketched, and deliberately **not built here**, because the shape is not yet unambiguous — it needs
its own tables (a live intake row and a live profile row keyed by the intake identity), and a
decision about what `runtime.decisions.profile_id` references for a live decision, which
interacts with the `r0003` above. Creating an overlay revision on an unsettled shape would be
worse than reporting it.

## 8. What is proved

A fresh platform-shaped round id — `2026-09-08T12:00:00+00:00`, absent from the fifty-row TRAIN
schedule — reaches the ownership capability through live intake + L1, using approved TRAIN
**input-side material only** (BAM/BAI/reference/FAI hashes and the region). Layer 1 mints
`profile_id = canonical_hash({bam_sha256, region, config_hash, profiler_version})[:32]`, which
depends on no schedule, no split and no round id, so the same inputs give the same profile whether
they arrive as research or as a live challenge — which is what makes the replay honest rather than
fabricated. No truth, no mutations, no scores, and the round's TRAIN outcome is never consulted.

A structural test makes every research authority raise on open — the TRAIN schedule, the Phase-A
authority, both split manifests and both snapshot documents — and the live round still becomes
authoritative.
