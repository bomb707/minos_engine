# L2-H — the capability trust model, stated honestly

This engine's Layer 2 authority chain is built from *sealed capabilities*: objects that only a
verifier can mint, checked downstream by exact concrete type plus a module-private token. Several
correctives have hardened that chain. This document says what the construction actually claims,
because an overstated claim is worse than a modest one — it stops people looking for the attacks
it does not in fact stop.

## 1. What is NOT claimed

**Python underscore names and `object()` tokens are not a cryptographic sandbox.** They are not a
security boundary against hostile code executing inside this interpreter. Code running in this
process can trivially defeat every mechanism described here. It can:

* import an underscore-prefixed module global — `from ...live_round_intake import
  _PRODUCTION_INTAKE_TOKEN` is ordinary Python, and nothing prevents it;
* call `object.__new__(SomeCapability)` and populate the instance by hand;
* write straight past any `__setattr__` guard with `object.__setattr__`;
* replace a `MappingProxyType`'s underlying dict through the reference it was built from, if that
  reference is still reachable;
* monkeypatch a verifier, a helper, or an entire module;
* read and rewrite process memory through `ctypes`.

No claim in this repository should be read as denying any of that. A test asserting that "a
hostile actor cannot obtain the token" would be false, and is not written.

The engine also does not claim to freeze objects it does not own. `MinerPlatformClient` and
`Miner` come from `minos_subnet`; they are live, mutable, externally-maintained objects, and the
engine has no authority to make them immutable. What it does instead is stated in §4.

## 2. What IS claimed

The supported integrity claim covers three populations:

1. **normal production code using supported APIs** — the service, the controller, the loaders;
2. **accidental or misrouted internal code** — a helper that reaches for the wrong object, a
   refactor that hands a fixture where production was meant, a caller that keeps a reference and
   writes through it later;
3. **subclass and mutable-state misuse** — subclassing a capability, skipping its constructor,
   copying its fields, or mutating a legitimately minted one.

For those three, none of the following is possible:

| | closed by |
|---|---|
| mint authority without its verifier | private mint token, demanded by the constructor |
| pass a subclass or lookalike as a capability | exact concrete type + seal at every boundary |
| relabel fixture authority into production | separate capability types, separate private tokens |
| **mutate verified state after minting** | **this task: post-mint freeze, nested containers included** |
| **read a mint token off an exported class** | **this task: `_expected_token` removed** |

The fourth and fifth rows are what this task closes. The first three were closed earlier and are
unchanged.

## 3. Why post-mint mutability mattered

Before this task, every one of these held on a *legitimately loaded, correctly sealed* authority:

```
authority = load_verified_round_profile_corpus(root=REPO_ROOT)
authority.partition = "live"                  # accepted
authority.corpus_identity = "forged..."       # accepted
authority._by_round["fake"] = ...             # accepted
authority.owned(round).bam_sha256 = "0" * 64  # accepted
is_verified_round_profile_authority(authority) # -> True
```

The seal was still valid, and it now attested to a corpus that had been edited after verification.
The same held for the live profile binding (`binding.owned`, `binding.identity`,
`binding.anchors[...]`) and for the safe-baseline authority (`baseline_config_hash`,
`baseline_uri`, `parameter_space_hash`, `source_commit`, `_policy[...]`, `entry_gate_checks[...]`).

That is squarely inside population (2) and (3): no hostility is required, only a later line of
code holding a reference. A verification result that any subsequent statement can edit is not a
verification result.

## 4. Externally-owned objects

`VerifiedProductionPlatformClient` and `VerifiedOfficialMiner` wrap objects from `minos_subnet`.
The wrapper is frozen; the wrapped object is not, and cannot be. The engine's honest position:

* the **wrapper** is sealed and immutable, so the binding between "this was verified" and "this
  object" cannot be re-pointed;
* the security-relevant **configuration** that verification actually checked is *snapshotted at
  verification time* into the frozen wrapper — base URL, hotkey SS58, round-status path, and the
  demo flag — as engine-owned `str`/`bool` primitives, never as references to the external
  object's own values;
* that snapshot is **re-checked against the live object immediately before and immediately after
  every production request** (§4a);
* the live object is still used for its behaviour (`get_round_status`, `_download_bam`), and its
  behaviour is not frozen. What that behaviour *produced* is then verified on its own merits —
  the response is parsed and bound, the downloaded bytes are hashed, the provenance sidecar is
  checked — so nothing downstream trusts the external object's word about anything.

### 4a. Snapshotting alone was not enough: the check must happen at use

Recording what was true at verification time closes nothing on its own, because the official
client reads its own mutable state **at request time**:

| upstream field | read when | decides |
|---|---|---|
| `self.demo` | each call, via the `_round_status_path` property | `/v2/round-status` vs `/v2/demo/round-status` |
| `self.config.base_url` | each call, in `_get_client()` | which host the request goes to |
| `self.keypair.ss58_address` | **each retry attempt**, in `_auth_body` | which hotkey signs the request |

`PlatformConfig` is a plain mutable dataclass, and upstream enforces HTTPS only in
`PlatformClient.__init__` — so a later write to `config.base_url` is not re-checked by anyone.

So a client verified as live and HTTPS could afterwards be flipped to demo and still mint a
**sealed `ProductionRoundStatusReceipt`**, with the engine's `endpoint_path()` continuing to
report `/v2/round-status`. That is a time-of-check/time-of-use defect, and a snapshot that is
never compared is just a comment.

`VerifiedProductionPlatformClient.require_binding_unchanged()` re-resolves the official type,
re-reads the live configuration, requires it to be independently valid for a live round, and
requires it to equal the snapshot field for field. `ProductionRoundStatusTransport.fetch_round_status`
calls it **twice**: before the request, so there is no network authority under changed
configuration; and after it, because the object stays mutable while the request is in flight and
upstream re-reads the hotkey on every retry — if anything moved, the payload is discarded rather
than returned.

Both conditions are required, and the second matters on its own: swapping one accepted HTTPS
platform for another is still not the client that was verified.

The engine's own `endpoint_path()` constant stays as defense in depth. It is what MINOS_ENGINE
*expects*, and by itself it proves nothing about where the external client actually sent the
request — the client's own `_round_status_path` is what establishes that.

As everywhere else here: this closes ordinary persistent and concurrent mutation. It does not
claim to detect a concurrent mutation that is reverted before the next read, which is outside the
model described in §1.

That is the boundary. It is documented rather than papered over.

## 4b. Accepted authorities that live in source

`ACCEPTED_REFERENCE_IDENTITIES` decides which genome this engine accepts as GRCh38 chr18–chr22,
and the chosen `reference_sha256`/`fai_sha256` enter the live intake's scientific identity. It was
an ordinary `dict` of writable objects, so ordinary code could rewrite the accepted reference
authority without touching source:

```python
ACCEPTED_REFERENCE_IDENTITIES["chr20"].reference_sha256 = ...  # accepted
ACCEPTED_REFERENCE_IDENTITIES["chr20"] = another_reference  # accepted
del ACCEPTED_REFERENCE_IDENTITIES["chr22"]  # accepted
ACCEPTED_REFERENCE_IDENTITIES["chr23"] = ...  # accepted
```

`typing.Final` is a type-checker annotation and enforces nothing at runtime. `ReferenceIdentity`
is now a frozen slotted dataclass and the table is a read-only mapping, so the accepted set can be
changed only by changing this source. `accepted_reference_set_identity()` gives the set a
deterministic name for audit and regression — deliberately **not** a field of `LIVE_INTAKE_SCHEMA`,
since the intake already records the per-round reference it was actually profiled against.

The test seam substitutes the module attribute with a *different read-only table*; it does not
edit the accepted one. That distinction is the point.

## 5. White-box test seams

`tests/unit/layer2/test_l2h_live_round_ownership.py` contains one helper that imports the private
`_CORPUS_TOKEN` to fabricate a live-scoped authority. It is named and documented as a **white-box
forged authority**, and it exists for exactly one purpose: proving that decision-manifest v1
refuses a live-scoped ownership.

It is **not** evidence that the production LIVE authority chain has been qualified. It never can
be. A future LIVE qualification must drive an approved production-equivalent authority seam end to
end; a test that reaches past the mint gate proves nothing about the mint gate.
