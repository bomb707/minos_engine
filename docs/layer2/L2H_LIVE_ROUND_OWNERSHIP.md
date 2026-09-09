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

### 2a. Production and fixture are two authority DOMAINS, not one class with a label

An earlier design had a single `VerifiedLiveRoundIntake`, a single private token, and an instance
attribute `scope` set to `"production"` or `"fixture"`. That is not a capability boundary. The
scope was mutable, the downstream binding verifier required only `isinstance`, and so a fixture
observation could be relabelled — or simply passed through unchanged — and end as a genuine
`VerifiedRoundProfileAuthority(partition="live")`. A test fixture could mint live authority.

The two domains are now two capabilities with two module-private tokens, at every stage:

| stage | production | fixture |
|---|---|---|
| intake | `VerifiedProductionLiveRoundIntake` | `FixtureLiveRoundIntake` |
| binding | `VerifiedProductionLiveProfileBinding` | `FixtureLiveProfileBinding` |
| ownership | `VerifiedRoundProfileAuthority` | `FixtureRoundProfileAuthority` |
| entry point | `verify_live_round_intake` / `verify_live_profile_binding` / `load_verified_live_round_ownership` | `observe_fixture_*` |

`scope` is now a **class** attribute of each concrete capability, and a minted intake is immutable
— `__setattr__` refuses every reassignment once the seal is set. There is no string to change, and
changing one would not matter, because nothing downstream reads `scope` as authority. Each guard
asks for the exact concrete type and the private token of *its own domain*.

The shared superclass carries the behaviour, and the shared `_build_*` helpers carry the
validation, so the fixture really does exercise the production arithmetic byte for byte. What it
never shares is a token. Inheritance is how the logic is reused; it is never how authority is
obtained.

The intake scope is also anchored *into* the ownership identity as `live_intake_scope`, so a
fixture chain's corpus identity is not, and can never be, the identity a production chain would
have produced for the same inputs.

## 3. The platform receipt is the authority — sealed, not merely typed

Three rounds of correction landed here. The first attempt let a caller supply a round id, region
and four hashes and checked they agreed with each other; **internal consistency is not
provenance**. The second demanded a receipt but let *any* transport mint one, including a fixture
and `/v2/demo/round-status`. The third separated fixture from production — but
`ProductionRoundStatusTransport` was still a **public subclassing point**, so a caller could
subclass it, return any payload, and mint a genuine production receipt. Inheritance is not
authority.

The chain is now sealed end to end, each link token-minted:

```
the REAL utils.platform_client.MinerPlatformClient object
  -> verify_subnet_platform_client   -> VerifiedProductionPlatformClient
  -> production_round_status_transport -> ProductionRoundStatusTransport  (@final, sealed)
  -> verify_production_round_status  -> ProductionRoundStatusReceipt
```

**Type identity, not names — and exact, not `isinstance`.** `verify_subnet_platform_client`
imports the real `utils.platform_client.MinerPlatformClient` and requires
`type(client) is that class`. An earlier version matched a class *named* `MinerPlatformClient` in
a module *named* `platform_client` (a test constructed exactly that and was accepted); the next
used `isinstance`, which a **subclass overriding `get_round_status`** would have satisfied while
returning anything it liked. `verify_official_miner` applies the same exact rule to
`neurons.miner.Miner`, where a subclass could override `_download_bam`. Both are now consistent
with the seal rule used everywhere else.

Tests prove this independently of whether the subnet's dependencies are installed: the resolver
itself is monkeypatched to a stand-in official class, the genuine instance is accepted, and a real
subclass of it is refused — with an assertion that `isinstance` *would* have passed.

**Fail closed, no fallback.** If the subnet package cannot be imported, the production path
**refuses** with a message naming `MINOS_SUBNET_ROOT` and stating that it does not fall back to
matching class names. There is deliberately no structural fallback, because a fallback is exactly
the hole being closed.

> **Deployment assumption, stated:** the process making live decisions must be able to import the
> subnet package — installed, on `PYTHONPATH`, or located by `MINOS_SUBNET_ROOT`. A miner already
> satisfies this, because it *is* the subnet process. In this repository's own environment the
> package is **not** importable (it needs `bittensor_wallet`), so the production path fails closed
> here and the test suite exercises that branch rather than spoofing the class.

**Subclassing grants nothing.** `ProductionRoundStatusTransport` is `@final`, its constructor
demands a module-private token, it carries a seal only that constructor sets, and
`verify_production_round_status` checks **exact type identity plus the seal** rather than
`isinstance`. A test dynamically synthesizes a subclass that skips `__init__`, asserts
`isinstance(...)` *would* have passed, and requires the refusal.

**Endpoint.** The sealed transport hard-codes `/v2/round-status`; it is not a parameter, and the
class contains no demo route at all. The endpoint remains part of the receipt identity, so a
response from any other route is a different round status.

**What a receipt asserts, unchanged and not overstated.** The miner signs the **request** (hotkey
signature over method, path, body and timestamp, plus a nonce, with `X-Minos-Auth-Version: 2`);
HTTPS authenticates and protects the configured transport; the response body carries **no digital
signature** — nothing in the subnet verifies one.

## 4. Downloads come from the official miner, not from the caller

The previous API took `bam_source_url` and `bam_path`, checked the URL against the round's offered
URLs, and hashed whatever file it was pointed at. **Those two facts never met**: nothing proved
the file came from that URL. A caller could copy an offered URL and hand over any file.

The production API now takes **neither**:

```python
download_production_round_inputs(receipt, miner)  # exactly these two parameters
```

It hands the round's own operational data to the maintained
`neurons.miner.Miner._download_bam`, which selects primary or backup by
`STORAGE_PRIMARY_BACKEND`, falls back, passes the platform's `bam_sha256` to the verified
downloader, fetches the index when one is offered and builds it with samtools when it is not — and
the engine hashes exactly the files that operation produced, at `bam_path` and `bam_path + ".bai"`.
None of those rules are reimplemented, and the caller chooses none of them. A test asserts the
signature is exactly `{receipt, miner}` and that `accept_production_round_downloads` no longer
exists.

Refused: a miner that is not the real type, a download the miner reports as failed, a returned
path that is not a file, a missing index, and a BAM that does not match the platform's published
SHA-256.

**Primary and backup URLs** are retained privately on the receipt and passed only to the official
downloader through `_operational_round_data()`. Which slot it chose is its own decision; the
provenance recorded is `official-miner-download`.

**URLs never reach an identity (§H).** A presigned URL expires, carries a signature and varies
between equivalent fetches. A test asserts no URL value, scheme, `sig=` or `?` appears in the
receipt content, the receipt observation, the download observation, the intake content or the
ownership anchors. Slot **names** are published deliberately; values never are.

## 4a. One capability rule, applied everywhere

`isinstance` is not a capability check anywhere in this chain. A subclass can skip `__init__`,
populate the slots by hand, and satisfy it without the private token ever having minted anything.
So every production authority crosses its boundary under one rule:

```python
type(candidate) is ExactProductionClass and candidate._seal is PRIVATE_TOKEN
```

applied to `VerifiedProductionPlatformClient`, `VerifiedOfficialMiner`,
`ProductionRoundStatusTransport`, `ProductionRoundStatusReceipt`, `ProductionRoundDownloads`,
`VerifiedProductionLiveRoundIntake`, `VerifiedProductionLiveProfileBinding`,
`VerifiedRoundProfileAuthority` and `VerifiedSafeBaselineAuthority` — the whole chain, from the
platform response to the controller boundary, with no stage left on `isinstance`. The helpers are
`is_verified_production_receipt`, `is_verified_production_downloads`, `is_verified_official_miner`,
`require_production_scope` / `is_verified_production_intake`,
`is_verified_production_live_binding`, `is_verified_round_profile_authority` and
`is_verified_safe_baseline_authority`. The tokens are module-private and never exported.

The last two matter most, because they are what the controller itself consumes.
`select_safe_baseline` and `SafeBaselineController.__init__` previously took both capabilities on
`isinstance`, so a subclass of either — skipping `__init__`, copying the fields from a real one —
reached the decision core. Both are now sealed and both entry points check the seal; the source
contains no `isinstance(authority` or `isinstance(ownership` at all, and a test asserts that.

Adding a runtime seal moves no published identity: `identity_content()` hashes the corpus contents
and anchors, never the capability state, so the TRAIN corpus identity is unchanged.

The `VerifiedOfficialMiner` bypass is closed the same way: a **sealed** capability is accepted
as-is, a raw object must earn one through `verify_official_miner`, and a *subclass of the
capability* is neither — it is refused by name, even when its `.miner` returns something with
`_download_bam`.

Tests forge each one — subclass, skip `__init__`, copy the identity, the scope and even the real
operational binding — assert that `isinstance` *would* have passed, and require the refusal. The
sharpest is a `ProductionRoundDownloads` subclass carrying a real receipt identity, a real
operational binding, and attacker-chosen hashes: it is refused on the seal, before any intake
exists.

## 4b. The receipt instance, not just its identity

The scientific receipt identity is `{schema, round_id, region_source, endpoint_path}` — it
excludes the operational URLs and the platform's expected BAM hash. So **two different responses
for the same round, region and endpoint share one identity** even when they offer different
download sources, and matching the identity alone would let downloads obtained under one be
presented for the other.

Each receipt therefore carries a per-instance sentinel — a bare `object()`, so it cannot be
serialized, compared across processes, or leak into evidence. It is **private**: callers ask
`receipt_owns_downloads(receipt, downloads)` rather than fetching it, because exposing it would
hand a forger the one ingredient a fabricated proof is missing. A test asserts neither the receipt
nor the downloads has a public `operational_binding` attribute.

## 4c. Cached bytes must belong to this response

**The finding.** `download_file_verified` returns an existing file untouched when no
`expected_sha256` is supplied — *"Cache hit (no hash check)"* — and `_download_bam` keys its output
directory on `round_id` alone. And the current official LIVE contract does **not** guarantee
`bam_sha256`: `/v2/round-status` does not document it, it is documented only for the practice
endpoint and only *"when configured"*, and both `neurons/miner.py` and `neurons/validator.py` read
it with `.get`. So two responses for the same round with different URLs and no digest can
legitimately return the *first* response's bytes — and the per-instance binding cannot catch it,
because the downloads object is created after the cache hit. A test reads these facts out of the
installed subnet source so the finding cannot go stale silently.

**Not chosen:** forcing a fresh download. That would discard a cache the miner is deliberately
keeping — the logs measure BAMs in gigabytes — and imposing that on every decision is not this
module's call. Nothing here deletes or re-fetches anything. `minos_subnet` is not modified.

**Chosen:** a provenance sidecar written beside the BAM by this integration, recording the
`round_id`, a **digest** of the operational source set, and the resulting hashes. On each handoff
the bytes are accepted when

* this call demonstrably wrote the file; or
* the platform published a `bam_sha256` and the bytes match it — the content is authoritative
  however it was obtained; or
* a sidecar names this same source set and these same bytes — provenance carries over;

and otherwise the handoff **fails closed** with an actionable message. The sidecar stores a
digest, never a URL — asserted by test.

**Freshness is inode state, not a clock.** An earlier version compared the BAM's mtime against a
marker written just before the call and treated `mtime >= marker` as proof of a write. Equality is
ambiguous: a coarse filesystem clock can stamp a file written moments earlier with exactly the
marker's value, so a cache hit could be declared fresh and skip sidecar validation — a fail-open
boundary, and one no tolerance window fixes, since a window only widens the ambiguity.

The file is now *observed* instead. Its path is derived from the official miner's own `BASE_DIR`
and `utils.path_utils.safe_round_dir_name` — nothing about backend selection, fallback,
downloading or indexing is reimplemented, only *where the file lands* — and its inode state
(device, inode, size, mtime, ctime) is snapshotted before the call and compared after:

| before → after | verdict |
|---|---|
| absent → present | this call created it — **fresh** |
| present, state **changed** | this call rewrote it — **fresh** |
| present, state **unchanged** | the downloader cached — **not fresh** |
| path underivable, or a path the layout does not predict | unknown — **not fresh** |

Timestamps decide nothing on their own, so an **equal** mtime and a **future** mtime both land in
"unchanged", which is the safe answer; filesystem metadata never becomes caller-controlled
authority. A false negative costs a refusal and a retry, and there is no false positive to trade
it against. Tests cover both, plus a genuine re-fetch that *restores* the old mtime — still
correctly fresh, because `ctime` moves.

The BAI needs no such protection: `_download_bam` unlinks the old index before obtaining or
rebuilding it, so it is always fresh. A test pins that upstream behaviour too.

## 4d. The live intake identity

`l2h-live-round-intake-v2` = `sha256("minos:l2h-live-round-intake:v2\n" + canonical_json_bytes(content))`
over a **closed** field set — a missing field and an unknown field are both refusals.

The builder is split from the authority: `canonical_live_round_content(...)` canonicalizes and
**mints nothing**; `verify_live_round_intake(receipt, downloads)` mints and has **no `content`
parameter at all**. No field of a verified intake originates in a caller-chosen string:

| field | comes from |
|---|---|
| `round_id`, `region_source`, `platform_receipt_identity` | the **production platform receipt** |
| `bam_sha256`, `bai_sha256` | **round-bound downloads**, hashed from files tied to that receipt's own URLs |
| `reference_sha256`, `fai_sha256` | the **accepted per-contig reference table** |
| region bounds, `region_hash`, `identity_tuple_hash` | derived |

`ACCEPTED_REFERENCE_IDENTITIES` pins one reference FASTA + FAI + M5 per contig, so a live round is
never profiled against the wrong genome build. A unit test cross-checks every entry against the
frozen L2-D corpus attestations, where all fifty members agree on one reference per chromosome.

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
schedule — reaches the ownership capability through the **whole chain**, with only the transport
substituted and only the authority token scope-separated:

```
FixtureRoundStatusTransport      ->  observe_fixture_round_status
real BAM/BAI + this round's URLs ->  accept_fixture_round_downloads
                                 ->  observe_fixture_live_round_intake      (FixtureLiveRoundIntake)
real intake.attest_input         ->  observe_fixture_live_profile_binding   (FixtureLiveProfileBinding)
                                 ->  observe_fixture_live_round_ownership   (FixtureRoundProfileAuthority)
```

That last line is where the offline chain **ends**. `FixtureRoundProfileAuthority` carries the
same lookups, the same anchors and the same identity arithmetic as production ownership — so the
replay genuinely tests them — and it is a different type, so `is_verified_round_profile_authority`
rejects it and neither controller entry point will take it. The fixture proves the logic without
ever becoming the authority.

The fixture domain differs from production only in which token is minted — the parsing,
canonicalization, URL binding, hashing and every refusal are the production implementation, and a
test asserts the two produce identical parsed content. Every production entry point refuses a
fixture capability **by type and seal**: `verify_live_round_intake` refuses a fixture receipt,
`verify_live_profile_binding` refuses a fixture intake, and `own_verified_live_round` refuses a
fixture binding. A negative matrix runs each attack — mutate the scope, copy the fields into the
production type, subclass the production type and skip `__init__`, hand-build the exact type,
subclass either controller capability — asserts that `isinstance` *would* have passed, and
requires the refusal.

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
