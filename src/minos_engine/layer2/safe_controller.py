"""The SAFE_BASELINE-only Layer 2 controller: source authority and pure decision core.

There is exactly one scientific outcome this module can produce -- the qualified baseline config
`157d88d1…` -- and that is a property of its structure, not of its inputs. There is no candidate
generator, no ranker, no utility model, no exploration and no random source anywhere in the call
path. A caller cannot widen it: `model_bundle_id` is recorded and ignored, and a request for a
contextual mode is deterministically reduced to SAFE_BASELINE rather than executed.

**Two failure classes, deliberately not merged.**

A GLOBAL AUTHORITY FAILURE means the engine cannot prove what it is about to emit: the L1 entry
gate is invalid, the baseline authority does not resolve, the payload bytes do not hash to the
accepted config, the parameter space has moved, or a repository prerequisite has drifted. These
raise :class:`SafeControllerAuthorityError` and emit nothing. Returning an unauthenticated CONFIG
because "the safe path should always work" would make the safe path the least trustworthy one.

A ROUND-LEVEL REDUCED-AUTHORITY CONDITION means the engine knows exactly what to emit and emits
it: a contextual mode was requested but no qualified model exists, or safe mode was asked for
directly. These return the verified baseline with a typed fallback reason. Nothing pretends the
requested contextual mode ran.

Layer 2 still does not open the BAM, and this module reads no truth, no VALIDATION and no TEST.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.frozen_state import FrozenAfterMint, deep_plain, frozen_deep
from minos_engine.common.hashing import sha256_hex
from minos_engine.layer2.contracts import (
    ArtifactIdentity,
    ControlMode,
    DecisionIdentity,
    DecisionRequest,
    DecisionResult,
    FallbackReason,
)
from minos_engine.layer2.round_profile_authority import (
    VerifiedRoundProfileAuthority,
    is_verified_round_profile_authority,
)
from minos_engine.layer2.safe_controller_policy import (
    ALLOWED_MODES,
    SAFE_CONTROLLER_VERSION,
    compute_safe_controller_policy_hash,
    load_committed_safe_controller_policy,
)

#: Where the frozen content-addressed GATK payloads live. An operational handle only: the file is
#: verified against the hash that names it, so a wrong root fails rather than substitutes.
from minos_engine.models.config_table import CONFIG_PAYLOAD_ROOT

__all__ = [
    "SAFE_DECISION_MANIFEST_DOMAIN",
    "SAFE_DECISION_MANIFEST_SCHEMA",
    "SafeBaselineController",
    "SafeControllerAuthorityError",
    "VerifiedSafeBaselineAuthority",
    "is_verified_safe_baseline_authority",
    "load_verified_safe_baseline_authority",
    "safe_decision_manifest_content",
    "safe_decision_manifest_identity",
    "select_safe_baseline",
]

SAFE_DECISION_MANIFEST_SCHEMA: Final = "l2h-safe-decision-manifest-v1"
SAFE_DECISION_MANIFEST_DOMAIN: Final = "minos:l2h-safe-decision-manifest:v1\n"


class SafeControllerAuthorityError(MinosEngineError):
    """A GLOBAL authority the safe controller depends on is invalid. Emit nothing."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeControllerAuthorityError(message)


_AUTHORITY_TOKEN: Final = object()


class VerifiedSafeBaselineAuthority(FrozenAfterMint):
    """Proof that everything the controller needs was checked before any decision was made.

    Minted only by :func:`load_verified_safe_baseline_authority`. The pure core takes this rather
    than paths or hashes, so there is no way to reach a decision without having passed the entry
    gate, resolved the baseline from its own authority, verified the payload bytes and confirmed
    the config is still legal under the CURRENT parameter space.
    """

    _frozen_error = SafeControllerAuthorityError
    _frozen_noun = "safe-baseline authority"

    __slots__ = (
        "_entry_gate_checks",
        "_frozen",
        "_policy",
        "_seal",
        "baseline_config_hash",
        "baseline_payload_sha256",
        "baseline_uri",
        "parameter_space_hash",
        "policy_hash",
        "source_commit",
        "source_tree",
    )

    def __init__(
        self,
        token: object,
        *,
        policy: dict[str, Any],
        policy_hash: str,
        baseline_config_hash: str,
        baseline_payload_sha256: str,
        baseline_uri: str,
        parameter_space_hash: str,
        entry_gate_checks: dict[str, bool],
        source_commit: str,
        source_tree: str,
    ) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise SafeControllerAuthorityError(
                "a safe-baseline authority may only be minted by the verifying loader; a "
                "dictionary has not been verified against anything"
            )
        # deep-frozen: refusing ``authority.baseline_config_hash = ...`` while
        # ``authority._policy["baseline_selected_identity"] = ...`` still worked would have been a
        # longer route to the same forged decision manifest.
        self._policy = frozen_deep(policy)
        self._entry_gate_checks = frozen_deep(entry_gate_checks)
        #: Set only here, by the constructor that demanded the private token. The controller
        #: checks it: a subclass that skips ``__init__`` and copies the fields satisfies
        #: ``isinstance`` and must not reach the decision core.
        self._seal = _AUTHORITY_TOKEN
        self.policy_hash = policy_hash
        self.baseline_config_hash = baseline_config_hash
        self.baseline_payload_sha256 = baseline_payload_sha256
        self.baseline_uri = baseline_uri
        self.parameter_space_hash = parameter_space_hash
        # minted from the SAME root that was verified, so nothing downstream needs to look a
        # repository up again -- a later global lookup could name a different checkout entirely
        self.source_commit = source_commit
        self.source_tree = source_tree
        # LAST: every field above is now final.
        self._freeze()

    @property
    def policy(self) -> dict[str, Any]:
        """An ordinary deep ``dict``/``list`` copy, detached from the authority.

        Deep, not shallow: a shallow copy still shared the nested containers, so a caller could
        edit the verified policy through the dict it was handed. Plain rather than the frozen
        representation, because the values are canonicalized into decision identities and a
        ``MappingProxyType``/``tuple`` would not serialize the way a ``dict``/``list`` does.
        """
        result: dict[str, Any] = deep_plain(self._policy)
        return result

    @property
    def entry_gate_checks(self) -> dict[str, bool]:
        """The entry-gate result this authority was minted against. A deep copy, as above."""
        result: dict[str, bool] = deep_plain(self._entry_gate_checks)
        return result

    @property
    def allowed_modes(self) -> tuple[str, ...]:
        return tuple(self._policy["allowed_modes"])

    def selected_config(self) -> ArtifactIdentity:
        """The one and only config this controller can select."""
        return ArtifactIdentity(uri=self.baseline_uri, sha256=self.baseline_config_hash)


def is_verified_safe_baseline_authority(candidate: Any) -> bool:
    """Exact concrete type and private seal -- the rule every capability here crosses under."""
    return (
        type(candidate) is VerifiedSafeBaselineAuthority
        and getattr(candidate, "_seal", None) is _AUTHORITY_TOKEN
    )


def load_verified_safe_baseline_authority(
    *,
    repo_root: Any = None,
    config_payload_root: Any = None,
) -> VerifiedSafeBaselineAuthority:
    """Verify every GLOBAL authority, then mint the capability. Fails closed on all of them."""
    from minos_engine.baseline.baseline_selected import (
        SELECTED_CONFIG_HASH,
        load_committed_baseline_selected,
    )
    from minos_engine.experiments.gatk_live_space import (
        canonicalize_live_gatk_config,
        live_gatk_parameter_space,
    )
    from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l2_entry_gate
    from minos_engine.layer2.safe_controller_policy import _resolve_root
    from minos_engine.qualification.provenance import read_provenance

    # ONE authority domain: the entry gate, the policy, the baseline authority, the payload and
    # the source provenance are all resolved against this same root. Mixing a caller-supplied
    # root with an ambient global one would let a tampered copy borrow the real repository's
    # authority for whichever checks it could not satisfy itself.
    root = _resolve_root(repo_root)

    # --- the repository-owned L1 entry gate. Not duplicated, not caller-supplied ----------- #
    gate = verify_l2_entry_gate(EntryGateRequest(repo_root=str(root)))
    _require(
        gate.ok,
        "the Layer 2 entry gate does not pass: " + "; ".join(gate.reasons or ("no reason",)),
    )

    policy = load_committed_safe_controller_policy(root)
    policy_hash = compute_safe_controller_policy_hash(policy)

    # --- the baseline comes from ITS OWN authority, never from a caller or a constant here -- #
    selected = load_committed_baseline_selected(root)
    baseline_hash = str(selected["content"]["selected_config_hash"])
    _require(
        selected["baseline_selected_hash"] == policy["baseline_selected_identity"],
        "the committed baseline authority does not hash to the identity the policy binds",
    )
    _require(
        baseline_hash == SELECTED_CONFIG_HASH == policy["safe_baseline_config_hash"],
        f"the baseline authority resolves to {baseline_hash}, not the policy's baseline",
    )

    # --- the payload bytes, and that they ARE the accepted baseline ------------------------ #
    payload_root = Path(config_payload_root) if config_payload_root else CONFIG_PAYLOAD_ROOT
    path = payload_root / f"{baseline_hash}.json"
    _require(path.is_file(), f"the baseline CONFIG payload is missing: {path}")
    _require(not path.is_symlink(), f"{path} is a symlink")
    raw = path.read_bytes()
    payload_sha = sha256_hex(raw)
    parsed = json.loads(raw)
    _require(
        canonical_json_bytes(parsed) == raw,
        "the baseline payload is not canonical bytes, so its hash is not its identity",
    )
    _require(
        sha256_hex(canonical_json_bytes(parsed)) == baseline_hash,
        f"the baseline payload hashes to {payload_sha}, not the accepted {baseline_hash}",
    )

    # --- §13: legal under the CURRENT parameter space, not the one it was chosen under ----- #
    space = live_gatk_parameter_space()
    _require(
        space.parameter_space_hash == policy["parameter_space_hash"],
        f"the live parameter space is {space.parameter_space_hash}, but the policy binds "
        f"{policy['parameter_space_hash']}; a new compatibility domain requires requalification",
    )
    canonical = canonicalize_live_gatk_config(parsed)
    _require(
        canonical.config_hash == baseline_hash,
        "canonicalising the baseline under the current parameter space changes its identity; "
        "the baseline must never be silently mutated to fit new ranges",
    )
    _require(
        canonical.effective_config == parsed,
        "the current parameter space rewrites a baseline field; this needs requalification",
    )
    _require(
        canonical.parameter_space_hash == space.parameter_space_hash,
        "the canonical config cites a parameter space other than the live one",
    )

    provenance = read_provenance(root)
    _require(
        bool(provenance.head_sha) and bool(provenance.tree_sha),
        f"the execution source provenance could not be read from Git at {root}",
    )

    return VerifiedSafeBaselineAuthority(
        _AUTHORITY_TOKEN,
        policy=policy,
        policy_hash=policy_hash,
        baseline_config_hash=baseline_hash,
        baseline_payload_sha256=payload_sha,
        baseline_uri=f"file://{path.resolve()}",
        parameter_space_hash=space.parameter_space_hash,
        entry_gate_checks=dict(gate.checks),
        source_commit=str(provenance.head_sha),
        source_tree=str(provenance.tree_sha),
    )


def _fallback_reason(requested: ControlMode) -> FallbackReason:
    """SAFE asked for is not a fallback; a contextual mode reduced to SAFE is."""
    if requested.value in ALLOWED_MODES:
        return FallbackReason.NONE
    # deliberately NOT BASELINE_GATE_FAILED: no baseline-improvement comparison ever existed,
    # because no model was ever loaded to make one
    return FallbackReason.SAFE_BASELINE_FORCED


def safe_decision_manifest_content(
    *,
    request: DecisionRequest,
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    owned: Any = None,
) -> dict[str, Any]:
    """The canonical scientific manifest of one safe decision.

    Deterministic for identical semantic input: no timestamps, no PIDs, no durations. Operational
    facts belong in the persistence layer's own columns, not in a scientific identity -- two
    identical requests must produce the same decision identity or the identity means nothing.

    Every authority value here comes from the minted capability. Nothing looks a repository up
    again: a global lookup after minting could name a different checkout than the one that was
    actually verified, which is exactly the kind of seam this controller exists to close.
    """
    # A LIVE round cannot be represented in THIS manifest version, and the failure is loud.
    #
    # Three fields below would silently change meaning: `profile_corpus_identity` names the fifty
    # member corpus a decision was admitted against and a live round has no corpus at all;
    # `profile_ownership_anchors` are the TRAIN campaign's anchors -- Phase-A authority, TRAIN
    # schedule, split manifest, registry snapshot, baseline protocol -- none of which exist for a
    # live round; and `dataset_id` names a registered research dataset that a live round does not
    # have. Emitting a v1 manifest with live values in those fields would be exactly the quiet
    # reinterpretation this engine refuses elsewhere, so a decision-manifest v2 is required and is
    # deliberately not defined here.
    _require(
        getattr(ownership, "scope", "train") == "train",
        f"{SAFE_DECISION_MANIFEST_SCHEMA} describes a decision admitted against the frozen TRAIN "
        "corpus; a live-scoped ownership authority needs a decision-manifest v2 because "
        "profile_corpus_identity, profile_ownership_anchors and dataset_id would otherwise change "
        "meaning without changing name",
    )
    proven = owned if owned is not None else ownership.require_owned_request(request)
    policy = authority.policy
    requested = request.requested_mode
    return {
        "schema_version": SAFE_DECISION_MANIFEST_SCHEMA,
        # every identity here is the OWNED one, proven against the frozen snapshot. The request's
        # own values had to equal these to get this far, so binding the proven side means the
        # scientific identity never depends on a value a caller merely asserted. Note that
        # `profile_manifest_hash` is deliberately absent: it has no canonical definition in this
        # engine (see Layer1ProfileReference), and an unauthenticated opaque value has no place
        # in a scientific identity.
        "round_id": proven.round_id,
        "dataset_id": proven.dataset_id,
        "chromosome": proven.chromosome,
        "profile_id": proven.profile_id,
        "profile_sha256": proven.profile_sha256,
        "profile_manifest_sha256": proven.profile_manifest_sha256,
        "profile_fingerprint_hash": proven.fingerprint_hash,
        "profile_identity_tuple_hash": proven.identity_tuple_hash,
        "attestation_hash": proven.attestation_hash,
        "registry_snapshot_hash": proven.registry_snapshot_hash,
        "profile_corpus_identity": ownership.corpus_identity,
        "profile_ownership_anchors": dict(sorted(ownership.anchors.items())),
        "region_hash": proven.region_hash,
        "parameter_space_hash": authority.parameter_space_hash,
        "caller": request.parameter_space.caller,
        "baseline_authority_identity": policy["baseline_selected_identity"],
        "baseline_qualified_gate_hash": policy["baseline_qualified_gate_hash"],
        "baseline_config_hash": authority.baseline_config_hash,
        "baseline_payload_sha256": authority.baseline_payload_sha256,
        "selected_config_hash": authority.baseline_config_hash,
        "controller_policy_hash": authority.policy_hash,
        "controller_version": SAFE_CONTROLLER_VERSION,
        "request_controller_version": request.controller_version,
        "requested_mode": requested.value,
        "actual_mode": ControlMode.SAFE_BASELINE.value,
        "fallback_reason": _fallback_reason(requested).value,
        "model_bundle_id_present": request.model_bundle_id is not None,
        "model_bundle_loaded": False,
        "models_qualified_status": policy["models_qualified_status"],
        "l2g_v2_campaign_freeze_identity": policy["l2g_v2_campaign_freeze_identity"],
        "contextual_research_closed": True,
        "guards": {
            "entry_gate_ok": True,
            "entry_gate_check_count": len(authority.entry_gate_checks),
            "baseline_payload_verified": True,
            "parameter_space_compatible": True,
            "round_profile_ownership_proven": True,
            "candidate_generation": False,
            "parameter_mutation": False,
        },
        # from the VERIFIED policy, not from a fresh global lookup: both come from the same
        # authority domain the capability was minted in
        "accepted_prerequisite_identity": policy["accepted_prerequisites"],
        "execution_source_commit": authority.source_commit,
        "execution_source_tree": authority.source_tree,
    }


def safe_decision_manifest_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity of a safe-controller decision manifest."""
    return sha256_hex(SAFE_DECISION_MANIFEST_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def select_safe_baseline(
    *,
    request: DecisionRequest,
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
) -> DecisionResult:
    """THE pure safe-controller core. One possible scientific result, by construction.

    Takes verified capabilities rather than paths: by the time this runs, every global authority
    has already passed, so anything left is either a straight safe selection or a contextual
    request being reduced to one.

    ``ownership`` is not optional. Proving a profile reference internally consistent only shows
    that a caller can do arithmetic; it says nothing about whether the round or the profile
    exists. A request that cannot be traced to an owning frozen snapshot member is refused, not
    degraded.
    """
    # exact concrete type + private seal at BOTH capability boundaries. `isinstance` would admit a
    # subclass that skipped the token-bearing constructor, and -- since the fixture live chain ends
    # in its own authority type -- would also admit an offline observation as live authority.
    _require(
        is_verified_safe_baseline_authority(authority),
        "a decision may only be made from a SEALED verified safe-baseline authority; a subclass "
        "that copies its fields is not one",
    )
    _require(
        is_verified_round_profile_authority(ownership),
        "a decision may only be made against a SEALED verified round/profile corpus; a fixture "
        "authority, a subclass, or an object that merely answers the same questions is not one",
    )
    _require(
        tuple(authority.allowed_modes) == tuple(ALLOWED_MODES),
        "the authority allows a mode set this controller cannot honour",
    )
    # the caller's declared baseline and parameter space must be the accepted ones. A request
    # naming a different baseline is not a fallback case; it is a caller disagreeing with the
    # authority, and emitting the accepted config anyway would answer a question nobody asked.
    _require(
        request.safe_baseline.sha256 == authority.baseline_config_hash,
        f"the request names safe baseline {request.safe_baseline.sha256}, not the accepted "
        f"{authority.baseline_config_hash}",
    )
    _require(
        request.parameter_space.parameter_space_hash == authority.parameter_space_hash,
        "the request names a parameter space other than the accepted live one",
    )

    owned = ownership.require_owned_request(request)
    manifest = safe_decision_manifest_content(
        request=request, authority=authority, ownership=ownership, owned=owned
    )
    manifest_hash = safe_decision_manifest_identity(manifest)
    return DecisionResult(
        decision=DecisionIdentity(
            decision_id=manifest_hash,
            config_hash=authority.baseline_config_hash,
            decision_manifest_hash=manifest_hash,
        ),
        mode=ControlMode.SAFE_BASELINE,
        selected_config=authority.selected_config(),
        fallback_reason=_fallback_reason(request.requested_mode),
    )


class SafeBaselineController:
    """A thin binding of one verified authority to the pure core.

    Not wired into :class:`~minos_engine.layer2.service.Layer2Service`: the public
    ``select_config`` boundary stays blocked until controller qualification and the decision
    persistence path are both closed. This class exists so that qualification drills can exercise
    the real decision path without exposing it.
    """

    __slots__ = ("_authority", "_ownership")

    def __init__(
        self,
        authority: VerifiedSafeBaselineAuthority,
        ownership: VerifiedRoundProfileAuthority,
    ) -> None:
        _require(
            is_verified_safe_baseline_authority(authority),
            "a safe controller may only be constructed from a SEALED verified authority",
        )
        _require(
            is_verified_round_profile_authority(ownership),
            "a safe controller may only be constructed against a SEALED verified profile corpus; "
            "a fixture authority is not one",
        )
        self._authority = authority
        self._ownership = ownership

    @property
    def authority(self) -> VerifiedSafeBaselineAuthority:
        return self._authority

    @property
    def ownership(self) -> VerifiedRoundProfileAuthority:
        return self._ownership

    def decide(self, request: DecisionRequest) -> DecisionResult:
        return select_safe_baseline(
            request=request, authority=self._authority, ownership=self._ownership
        )

    def manifest(self, request: DecisionRequest) -> dict[str, Any]:
        return safe_decision_manifest_content(
            request=request, authority=self._authority, ownership=self._ownership
        )
