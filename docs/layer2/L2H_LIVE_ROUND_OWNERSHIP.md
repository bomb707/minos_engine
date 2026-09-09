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
`common.genomic_region`, and the live branch is `is_upstream_round_id` — **upstream's own rule,
transcribed literally**: `fromisoformat`, a one-character `Z` strip and retry for the legacy
`+00:00Z` shape, and then `tzinfo is not None`. A **naive** timestamp is refused, which an earlier
version of this work wrongly accepted. A parity table names every branch, and when the subnet
checkout sits beside this repository a second test asks the official implementation directly
rather than only trusting the transcription; it currently agrees on every case.

The engine is deliberately *stricter* on path safety: it also refuses separators, traversal
fragments and whitespace, so `2026-01-21 12:00:00+00:00` — which upstream's `fromisoformat` would
accept — is rejected here, because a round id becomes a persisted decision's join key and part of
a scientific identity as well as reaching a path. The strictness only ever removes values; a test
asserts the engine never accepts anything upstream rejects, the frozen research hex shape aside.
Hex is a strict subset of what was accepted before, so **no existing artifact changes**.

## 2. Three authorities, kept apart

| | scope | admissible because |
|---|---|---|
| `l2h-round-profile-ownership-v2` | TRAIN, 50 frozen rounds | it is in the frozen schedule, anchored to Phase-A/split/registry authority |
| `l2h-live-round-intake-v1` | one live challenge | its inputs authenticate |
| `l2h-live-round-profile-ownership-v1` | one live challenge | its L1 profile provably belongs to that intake |

The TRAIN loader is **untouched** and still anchors to exactly what it anchored to before. Its
corpus identity is still `9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03`.

## 3. The platform receipt is the authority

An earlier version of this work let a caller supply a round id, a region and four content hashes,
checked that they were internally consistent, and minted the capability. **Internal consistency is
not provenance.** Anyone able to invent a plausible ISO timestamp and four hex strings could
invent a round. Canonicalizing something does not make it true.

`protocol/round_status.py` fixes that. `VerifiedPlatformRoundStatus` exists only because a
transport went and asked the platform:

| | |
|---|---|
| seam | `RoundStatusTransport` — the **transport**, never the authority |
| production | `SubnetPlatformRoundStatusTransport`, wrapping the subnet's own `MinerPlatformClient` |
| tests | `FixtureRoundStatusTransport` — deterministic, and still goes through the same verification |
| mint | `verify_platform_round_status` only; the constructor demands a private token |

**What a receipt asserts, precisely.** Read from the subnet source: the miner **signs the
request** (`_auth_body` adds a hotkey signature over method, path, body and timestamp, plus a
nonce, sent with `X-Minos-Auth-Version: 2`), and the transport is **HTTPS-enforced**
(`PlatformClient.__init__` refuses a non-HTTPS base URL outside localhost). The response is
**plain JSON with no digital signature** — nothing in the subnet verifies one. So a receipt
asserts exactly: *this payload was returned by the configured, HTTPS-protected platform transport
in answer to a request this miner signed.* It is a transport-authenticated receipt, not a signed
document, and it is not described as one anywhere.

The engine deliberately does **not** reimplement the HTTP call. Re-deriving the request signing,
nonce and auth headers here would be a second implementation of a security boundary that already
exists and is maintained next door, and it would need the miner's keypair.

A receipt is refused unless the platform reports an **active, open** round with a usable round id
and region. `pending`, `scoring` and `completed` are not rounds to decide for. A client in demo
mode — which routes to the sandboxed `/v2/demo` namespace — is refused for a live decision.

## 4. The live intake identity

`l2h-live-round-intake-v2` = `sha256("minos:l2h-live-round-intake:v2\n" + canonical_json_bytes(content))`
over a **closed** field set — a missing field and an unknown field are both refusals.

The two jobs the specification separates are separated:

* `canonical_live_round_content(...)` still canonicalizes and **mints nothing**. It returns
  ordinary untrusted content.
* `verify_live_round_intake(receipt, downloads)` mints, and **has no content parameter at all**.

so that no field of a verified intake originates in a caller-chosen string:

| field | comes from |
|---|---|
| `round_id`, `region_source`, `platform_receipt_identity` | the **platform receipt** |
| `bam_sha256`, `bai_sha256` | `hash_downloaded_inputs`, which **reads real files** |
| `reference_sha256`, `fai_sha256` | the **accepted per-contig reference table** |
| region bounds, `region_hash`, `identity_tuple_hash` | derived |

`LocalDownloadDigests` is likewise token-minted: a caller cannot *state* a BAM hash, it has to
possess bytes that hash that way. That is what makes "the downloaded BAM hash was substituted" a
refusal rather than a different self-consistent story.

`ACCEPTED_REFERENCE_IDENTITIES` pins one reference FASTA + FAI + M5 per contig, so a live round is
never profiled against the wrong genome build. A unit test cross-checks every entry against the
frozen L2-D corpus attestations, where all fifty members agree on one reference per chromosome.

Presigned URLs, timings, nonces, signatures and `num_mutations` never enter any identity.
`parameter_space_hash` is deliberately absent: it defines what the controller may *do*, not what
the round *is*, and binding it here would break the profile binding whenever the parameter space
moved even though the inputs had not.

`dataset_id` is derived, not invented: `live-<chromosome>-<identity_tuple_hash[:16]>`, prefixed so
it can never be mistaken for a registry-backed research id.

**`registry_snapshot_hash` keeps its meaning.** For a research round it is the identity of the
`catalog.dataset_registry` snapshot the attestation was matched against. A live round has no
registry, and the document registering what its inputs are *is* the intake — so the intake's
identity takes that role. The field still means "the identity of the registered input set this
attestation was checked against"; only which document registers it differs.

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

## 5. Round-scoped, and no door beside the capability

The live factory takes one receipt and one profile and mints an authority owning **exactly one
round** — `len(ownership) == 1`. There is no history to load and no row to pick.

The controller's `isinstance(ownership, VerifiedRoundProfileAuthority)` check is **unchanged**; it
was not weakened to a `Protocol` or to duck typing.

**The escape hatch is gone.** An earlier version exported
`mint_verified_ownership(by_round, anchors, corpus_identity, partition)` — a public wrapper around
the private token. The capability design was intact and the door beside it was open: anyone could
hand-build `OwnedRoundProfile` values, pass them in, and receive an object that satisfies the
controller's check. It is removed, not renamed. What replaces it:

```
verify_live_profile_binding(...)  ->  VerifiedLiveProfileBinding   (token-minted, live module)
own_verified_live_round(binding)  ->  VerifiedRoundProfileAuthority (token-owning module)
```

`own_verified_live_round` accepts exactly one argument, and the only way to produce it is to have
verified a live profile against a live intake. There is no parameter through which a hand-built
map can reach the token, and a test executes the old bypass directly and requires refusal. A
static test asserts no public function anywhere in the module still takes raw ownership data, and
that exactly two call sites reach `_CORPUS_TOKEN`: the frozen TRAIN loader and this one.

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
schedule — reaches the ownership capability through the **whole production chain**, with only the
transport substituted:

```
FixtureRoundStatusTransport  ->  verify_platform_round_status  ->  VerifiedPlatformRoundStatus
real BAM/BAI on disk         ->  hash_downloaded_inputs        ->  LocalDownloadDigests
                                 verify_live_round_intake      ->  VerifiedLiveRoundIntake
real intake.attest_input     ->  verify_live_profile_binding   ->  VerifiedLiveProfileBinding
                                 own_verified_live_round       ->  VerifiedRoundProfileAuthority
```

Everything but the transport is genuine production code: `build_dataset` writes a real BAM, index,
reference and FAI; `Layer1Service.analyze` is the real profiler; `intake.attest_input` is the real
attestation producer, stream-hashing those same files. Layer 1 mints
`profile_id = canonical_hash({bam_sha256, region, config_hash, profiler_version})[:32]`, which
depends on no schedule, no split and no round id. The whole replay runs in well under a second and
needs no network. No truth, no mutations, no scores.

The one other substitution is the accepted reference table: a synthetic genome cannot hash to the
real GRCh38 contig this engine is qualified against, so the test patches that module constant for
its contig. It is *patched*, never parameterized — production has no argument through which a
reference identity could be supplied.

A structural test makes every research authority raise on open — the TRAIN schedule, the Phase-A
authority, both split manifests and both snapshot documents — and the live round still becomes
authoritative.
