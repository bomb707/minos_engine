<title>Live Decision Manifest v2</title>

# `l2h-safe-decision-manifest-v2` — a SAFE decision against one authenticated LIVE round

**Status: SOURCE ONLY. Not qualified, not frozen, not persistable.** The historical
`SAFE-CONTROLLER-FROZEN` gate (`504e701f…`) covers the historical qualified controller source
`7d064fe8…` and nothing here. Persistence is still accepted for v1 only. `Layer2Service` is still
blocked.

## 1. Why a new schema rather than v1 with different values

`l2h-safe-decision-manifest-v1` describes a decision admitted against the frozen 50-round TRAIN
corpus. Four of its fields describe that research campaign and have no live counterpart:

| v1 field | what it means in v1 | why a live round has no such thing |
|---|---|---|
| `dataset_id` | a registered research dataset in `catalog.dataset_registry` | a live round is never registered; nothing allocated it to a split |
| `registry_snapshot_hash` | the frozen registry snapshot the attestation was matched against | there is no registry; the authenticated intake plays that role |
| `profile_corpus_identity` | the fifty-member corpus the decision was admitted against | a live round is its own single-member ownership; there is no corpus |
| `profile_ownership_anchors` | Phase-A authority, TRAIN schedule, split manifest, baseline protocol | none of those exist for a round the platform issued this morning |

Emitting live values under those names would be a reinterpretation without a rename — the one
thing this engine refuses everywhere else. So **v2 drops all four** and publishes the live
authorities under names chosen for their semantics.

v1 is unchanged, remains TRAIN-only, and still refuses a live authority when called directly.

## 2. Identity

```
SAFE_DECISION_MANIFEST_V2_SCHEMA = "l2h-safe-decision-manifest-v2"
SAFE_DECISION_MANIFEST_V2_DOMAIN = "minos:l2h-safe-decision-manifest:v2\n"

identity = sha256( DOMAIN.encode("utf-8") + canonical_json_bytes(manifest) )
```

A separate hashing domain, so a v1 identity and a v2 identity cannot collide even if the two
documents were somehow byte-equal.

## 3. The LIVE authority block

Every value comes from the sealed `VerifiedRoundProfileAuthority`, never from the request.

| v2 field | source | meaning |
|---|---|---|
| `ownership_scope` | `ownership.scope` | always `"live"`. Stated, so no reader has to infer it from the schema name |
| `live_input_set_id` | `owned.dataset_id` | content-addressed `live-<chrom>-<identity_tuple[:16]>` from the authenticated input set. **Not** a research split allocation and **not** a `catalog.dataset_registry` identity — which is exactly why it is not called `dataset_id` |
| `live_intake_identity` | `anchors["live_intake_identity"]` | the `l2h-live-round-intake-v2` document that authenticated this round's inputs |
| `live_intake_schema` | `anchors["live_intake_schema"]` | which intake contract produced it |
| `platform_receipt_identity` | `anchors["platform_receipt_identity"]` | the platform round-status response this round came from |
| `live_profile_ownership_identity` | `ownership.corpus_identity` | the `l2h-live-round-profile-ownership-v1` proof that this profile belongs to this round |
| `live_profile_ownership_anchors` | `ownership.anchors` | the live chain's own anchors, including `live_intake_scope` |

### Why `registry_snapshot_hash` is not published

`OwnedRoundProfile.registry_snapshot_hash` exists because the accepted L2-D admission interface
(`validate_admission`) expects that shape, and for a live round it holds the **intake identity**,
not any registry snapshot. It is an internal compatibility field. Publishing it under that name
would leak a misleading semantic into a scientific contract, so v2 publishes the underlying
authority as `live_intake_identity` instead — and the builder requires the two to be equal, so the
compatibility shim and the published authority can never describe different things.

## 4. The common scientific block

These fields mean exactly the same thing in both domains and come from one implementation,
`_common_safe_decision_science`. Each was audited for this task rather than copied: every one
describes the round's own genomic input, the controller's own accepted authority, or the decision
that was made — none describes a campaign, split, schedule or registry.

* **round input** — `round_id`, `chromosome`, `profile_id`, `profile_sha256`,
  `profile_manifest_sha256`, `profile_fingerprint_hash`, `profile_identity_tuple_hash`,
  `attestation_hash`, `region_hash`
* **controller authority** — `parameter_space_hash`, `caller`, `baseline_authority_identity`,
  `baseline_qualified_gate_hash`, `baseline_config_hash`, `baseline_payload_sha256`,
  `selected_config_hash`, `controller_policy_hash`, `controller_version`,
  `request_controller_version`
* **the decision** — `requested_mode`, `actual_mode`, `fallback_reason`,
  `model_bundle_id_present`, `model_bundle_loaded` (always `false`), `models_qualified_status`,
  `l2g_v2_campaign_freeze_identity`, `contextual_research_closed` (always `true`), `guards`,
  `accepted_prerequisite_identity`, `execution_source_commit`, `execution_source_tree`

`profile_manifest_hash` stays deliberately absent from both: it has no canonical definition in
this engine, and an unauthenticated opaque value has no place in a scientific identity.

## 5. Dispatch

```
select_safe_baseline(request, authority, ownership)
    require is_verified_round_profile_authority(ownership)   # exact type + private seal
    ...
    safe_decision_manifest_for(...)
        ownership.scope == "train"  ->  v1
        ownership.scope == "live"   ->  v2
        anything else               ->  refuse
```

The schema is chosen by the **verified ownership domain**, after the seal check — never by the
caller. No request field selects it: a caller that could pick the schema could pick which meanings
its values are read with. `DecisionRequest` carries no `partition`, `scope` or `schema_version`,
and a test asserts that.

Separation is strict in both directions:

| | TRAIN ownership | LIVE ownership |
|---|---|---|
| `safe_decision_manifest_content` (v1) | PASS | REFUSE |
| `live_safe_decision_manifest_content` (v2) | REFUSE | PASS |

## 5a. Two preconditions every public builder enforces

The manifest builders are exported and may be called directly -- by the persistence writer, by
the qualification harness, by a future service. Relying on `select_safe_baseline` having checked
first is relying on a caller, so both preconditions live in the builders themselves.

### Both sealed authorities, before either is read

`_require_manifest_authorities` demands `is_verified_safe_baseline_authority(authority)` **and**
`is_verified_round_profile_authority(ownership)` at every public entry -- v1, v2 and the
dispatcher. Without it, the manifest API read `authority.policy`,
`authority.baseline_config_hash`, `authority.parameter_space_hash`, `authority.source_commit` and
the rest straight into a scientific document, so an object that merely carried those attribute
names had its values published as though an accepted authority had produced them -- including
`selected_config_hash`. A test drives a lookalike whose every attribute access is recorded and
asserts that **nothing** was read before the refusal.

### The owned member is always proved

`OwnedRoundProfile` is deliberately a public, constructible value object. It is immutable once
built, and immutability is not provenance. The builders used to read:

```python
proven = owned if owned is not None else ownership.require_owned_request(request)
```

so supplying `owned` skipped `require_owned_request` outright. A fabricated member could then be
handed in beside a **genuine sealed live authority**, and the manifest combined real ownership
anchors and a real intake identity with attacker-chosen `profile_id`, `profile_sha256` and
`round_id`.

`_proven_member` now proves the request unconditionally and accepts a supplied `owned` only when
it **is** the object the sealed authority holds:

```python
proven = ownership.require_owned_request(request)
if owned is not None and owned is not proven:
    raise ...
return proven
```

Object identity rather than field equality: `ownership.owned(...)` returns the frozen member
itself, so identity is available and is the strongest available statement of provenance -- an
equal-looking copy is not the corpus member, and a test proves that a byte-identical twin is
refused. The parameter is kept rather than removed because `storage/decision_persistence.py`
passes it and that module is byte-locked; it is now a redundancy check rather than a shortcut,
and valid calls produce identical bytes with or without it.

The identity hashers stay pure. `safe_decision_manifest_identity`,
`live_safe_decision_manifest_identity` and `safe_decision_identity_for` hash an already-built
canonical document and perform no authority or repository lookup; the obligation belongs at
document construction. A test asserts their bodies contain no loader.

## 6. What v2 does **not** change

The decision is identical. Every valid live request selects exactly
`157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea`, with no parameter mutation, no
candidates, no models, no ranker, no HPO, no RNG and no score estimator. Requested modes collapse
exactly as the accepted SAFE policy defines: `SAFE_BASELINE → SAFE_BASELINE/NONE`, and
`BOUNDED`/`FULL_CONTEXTUAL`/`REFINEMENT` → `SAFE_BASELINE/SAFE_BASELINE_FORCED`.

## 7. What may never enter a v2 identity

No timestamp, duration, PID, hostname, temp path, presigned URL, hotkey, base URL, cache path,
provenance-sidecar path, or the receipt's per-instance operational sentinel. Those describe *how*
this engine reached the platform, not *what* the round is; two identical rounds must produce one
identity or the identity means nothing. A test greps the serialized manifest for each of them.

## 8. What is required next

**`r0003` is required before any live decision can be recorded.** The runtime overlay's
`ck_decisions_manifest_schema` CHECK pins `decision_manifest ->> 'schema_version' =
'l2h-safe-decision-manifest-v1'`, so a v2 document cannot be stored today. v2 deliberately keeps
`round_id`, `actual_mode`, `requested_mode` and `fallback_reason` under their v1 names, so the
overlay's other four manifest CHECKs remain satisfiable and `r0003` only has to widen the schema
constraint.

Until then, both existing recording paths **fail closed** for a live decision rather than writing
one under the wrong contract:

* `decide_and_publish` still builds v1, so a live authority is refused;
* `publish_safe_decision` derives the identity itself in the v1 domain and refuses a v2 identity;
* `persist_safe_decision` is unchanged and v1-only.

The order of the remaining work is: review and qualify this controller source → reissue the frozen
controller gate → `r0003` + persistence requalification → service activation. None of it is done
here.

## 9. Consumers that will need requalification

`safe_decision_manifest_content` / `safe_decision_manifest_identity` are consumed by
`safe_controller_qualification.py`, `storage/decision_persistence.py`,
`storage/decision_persistence_qualification.py` and `layer2/decision_publication.py`. All four are
still v1-only and unchanged by this task; all four are in scope for the r0003-era requalification.
