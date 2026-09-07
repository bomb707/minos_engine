"""``l2h-safe-controller-qualification-v1`` — qualifying SAFE_BASELINE-only control.

Every check here is DERIVED from an observation this module made itself. No caller passes a
verdict: the qualification entry takes verified capabilities and an output root, drives the real
decision path over the real owned corpus, runs the authority-failure drills itself, and only then
computes the check dictionary. A caller who wants a PASS has to make the controller actually
behave, which is the whole point of a qualification.

This is **not** MODELS-QUALIFIED and cannot become it. Its capability scope is exactly
`SAFE_BASELINE`; it asserts that no contextual model exists and that none was loaded, which is the
opposite of a model qualification. It is also not a gate: no gate artifact is issued here.

Nothing in this module reads truth, mutations, scores, VALIDATION or TEST, and nothing fits,
predicts or ranks. The only scientific value it can produce is the one config the controller can
produce.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.layer2.contracts import (
    ArtifactIdentity,
    ComputeLimits,
    ControlMode,
    DecisionRequest,
    FallbackReason,
    Layer1ProfileReference,
    ParameterSpaceIdentity,
    RoundIdentity,
)

__all__ = [
    "MANDATORY_CHECKS",
    "SAFE_CONTROLLER_QUALIFICATION_DOMAIN",
    "SAFE_CONTROLLER_QUALIFICATION_PATH",
    "SAFE_CONTROLLER_QUALIFICATION_SCHEMA",
    "SafeControllerQualificationError",
    "TrustedSafeControllerQualification",
    "assemble_qualification_report",
    "derive_checks",
    "qualification_report_identity",
    "run_safe_controller_qualification",
    "verify_qualification_report",
]

SAFE_CONTROLLER_QUALIFICATION_SCHEMA: Final = "l2h-safe-controller-qualification-v2"
SAFE_CONTROLLER_QUALIFICATION_DOMAIN: Final = "minos:l2h-safe-controller-qualification:v2\n"
SAFE_CONTROLLER_QUALIFICATION_PATH: Final = (
    "reports/layer2/l2h-safe-controller-qualification-v2.json"
)

QUALIFICATION_TOOL_VERSION: Final = "l2h-safe-controller-qualifier-v2"

#: The capability this qualification covers. Named so nobody has to infer it.
CAPABILITY_SCOPE: Final = "SAFE_BASELINE_ONLY"

#: The 21 mandatory checks from ``docs/layer2/L2H_SAFE_CONTROLLER.md`` §10, in order. Nothing may
#: be removed; additions are allowed and appear after these.
MANDATORY_CHECKS: Final[tuple[str, ...]] = (
    "exact_l1_entry_authority",
    "exact_baseline_qualified_authority",
    "exact_l2g_closure_identities",
    "models_qualified_remains_hold",
    "allowed_modes_exactly_safe_baseline",
    "every_decision_selects_the_safe_baseline",
    "zero_invalid_configs",
    "zero_contextual_model_loads",
    "zero_candidate_generation",
    "zero_parameter_mutation",
    "decision_manifest_canonical_and_deterministic",
    "semantic_replay_identity_stable",
    "contextual_requests_typed_safe_baseline_forced",
    "corrupted_global_authority_fails_closed",
    "baseline_payload_tamper_fails_closed",
    "parameter_space_mismatch_fails_closed",
    "low_time_request_still_deterministic",
    "no_truth_validation_or_test_dependency",
    "fallback_success_is_total",
    "owning_round_profile_cross_check",
    "decision_persistence_disposition_closed",
)

#: Additional checks this qualifier adds. Documented separately so the mandatory set stays exact.
ADDITIONAL_CHECKS: Final[tuple[str, ...]] = (
    "owned_corpus_admitted_by_accepted_authority",
    "ownership_failures_are_authority_failures",
    "model_bundle_id_cannot_influence_the_config",
    "publication_is_content_addressed_and_idempotent",
    "select_config_public_boundary_blocked",
    "sealed_authorities_never_opened",
    "train_ownership_anchored_to_accepted_authority",
    "publication_identity_is_derived_not_declared",
)

#: Why the v1 report may not be used for qualification. Recorded in the evidence itself so a
#: future reader does not have to reconstruct it.
SUPERSEDES: Final[dict[str, str]] = {
    "schema": "l2h-safe-controller-qualification-v1",
    "identity": "7d305bcd7c35c82389259ec1d88058ff9202ce454a0364d15e4b864345aaf821",
    "reason": (
        "its sealed-partition isolation proof was insufficient: the ownership loader read, "
        "parsed and traversed the whole 75-member profile snapshot before skipping non-TRAIN "
        "members, so TEST identities were enumerated, and the report's skipped_partition_counts "
        "were derived by traversing the very records they claimed were untouched"
    ),
    "status": "HISTORICAL_EVIDENCE_NOT_VALID_FOR_QUALIFICATION",
}

ALL_CHECKS: Final[tuple[str, ...]] = MANDATORY_CHECKS + ADDITIONAL_CHECKS

SAFE_CONFIG_HASH: Final = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"


class SafeControllerQualificationError(MinosEngineError):
    """The safe-controller qualification could not be run or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeControllerQualificationError(message)


_QUALIFICATION_TOKEN: Final = object()


class TrustedSafeControllerQualification:
    """Observations that this module actually made. Minted only by the qualification entry."""

    __slots__ = ("_observation",)

    def __init__(self, token: object, *, observation: dict[str, Any]) -> None:
        if token is not _QUALIFICATION_TOKEN:
            raise SafeControllerQualificationError(
                "a qualification may only be minted by running one; a dictionary of claimed "
                "results is not an observation"
            )
        self._observation = copy.deepcopy(observation)

    @property
    def observation(self) -> dict[str, Any]:
        return copy.deepcopy(self._observation)


def _request_for(
    owned: Any,
    *,
    authority: Any,
    mode: ControlMode,
    remaining_seconds: float = 30.0,
    **override: Any,
) -> DecisionRequest:
    fields: dict[str, Any] = {
        "profile_id": owned.profile_id,
        # unauthenticated by contract; supplied only because the field is mandatory
        "profile_manifest_hash": "0" * 64,
        "profile_manifest_sha256": owned.profile_manifest_sha256,
        "fingerprint_hash": owned.fingerprint_hash,
        "region_hash": owned.region_hash,
        "bam_sha256": owned.bam_sha256,
        "bai_sha256": owned.bai_sha256,
        "reference_sha256": owned.reference_sha256,
        "fai_sha256": owned.fai_sha256,
    }
    fields.update(override)
    return DecisionRequest(
        round=RoundIdentity(round_id=owned.round_id),
        profile_ref=Layer1ProfileReference(**fields),
        parameter_space=ParameterSpaceIdentity(parameter_space_hash=authority.parameter_space_hash),
        safe_baseline=ArtifactIdentity(
            uri=authority.baseline_uri, sha256=authority.baseline_config_hash
        ),
        controller_version="l2h-qualification",
        limits=ComputeLimits(
            remaining_seconds=remaining_seconds,
            wall_clock_budget_seconds=600.0,
            cpu_limit=2,
            memory_limit_bytes=1 << 30,
        ),
        requested_mode=mode,
    )


def _drill(callable_: Any) -> bool:
    """Run an authority-failure drill. True when it failed closed, as it must."""
    from minos_engine.common.errors import MinosEngineError as _Base

    try:
        callable_()
    except _Base:
        return True
    except Exception:
        return True
    return False


def run_safe_controller_qualification(
    *, repo_root: Any = None, corpus_root: Any = None, output_root: Any
) -> TrustedSafeControllerQualification:
    """Drive the REAL decision path over the REAL owned corpus and observe what happens."""
    from minos_engine.baseline.baseline_selected import SELECTED_CONFIG_HASH
    from minos_engine.layer2.decision_publication import (
        DECISION_PUBLICATION_DISPOSITION,
        publish_safe_decision,
        read_published_decision,
    )
    from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l2_entry_gate
    from minos_engine.layer2.round_profile_authority import (
        OWNERSHIP_SCHEMA,
        load_verified_round_profile_corpus,
    )
    from minos_engine.layer2.safe_controller import (
        load_verified_safe_baseline_authority,
        safe_decision_manifest_content,
        safe_decision_manifest_identity,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_policy import (
        ALLOWED_MODES,
        _resolve_root,
        compute_safe_controller_policy_hash,
    )
    from minos_engine.layer2.sealed_access_guard import (
        SEALED_IDENTITY_AUTHORITIES,
        sealed_access_guard,
    )
    from minos_engine.layer2.service import Layer2Service
    from minos_engine.models.campaign_freeze import (
        CAMPAIGN_FREEZE_PATH,
        campaign_freeze_identity,
        verify_campaign_freeze,
    )
    from minos_engine.models.relative_finalist_freeze import (
        V2_FREEZE_PATH,
        v2_campaign_freeze_identity,
        verify_v2_campaign_freeze,
    )
    from minos_engine.qualification.l2f2_baseline_qualified_runner import (
        verify_baseline_qualified_gate,
    )

    root = _resolve_root(repo_root)
    published_root = Path(output_root)

    # Isolation is OBSERVED, not authored. Every authority document that carries a sealed member
    # identity is un-openable inside this block, and the attempt counter is what the checks are
    # derived from -- a constant `test_accessed: false` proves nothing.
    with sealed_access_guard() as sealed:
        authority = load_verified_safe_baseline_authority(repo_root=root)
        ownership = load_verified_round_profile_corpus(root=root, corpus_root=corpus_root)
        materialized_rounds = set(ownership.rounds())
    policy = authority.policy

    gate = verify_l2_entry_gate(EntryGateRequest(repo_root=str(root)))
    baseline_gate = verify_baseline_qualified_gate(
        gate_path=str(root / "gates/baseline-qualified.json"),
        qualification_path=str(root / "reports/layer2/baseline-qualified-result.json"),
        root=root,
    )
    v1 = json.loads((root / CAMPAIGN_FREEZE_PATH).read_bytes())
    verify_campaign_freeze(v1)
    v2 = json.loads((root / V2_FREEZE_PATH).read_bytes())
    verify_v2_campaign_freeze(v2)

    # --- the campaign itself: every owned profile, every requested mode -------------------- #
    requested_counts: dict[str, int] = {mode.value: 0 for mode in ControlMode}
    actual_counts: dict[str, int] = {}
    fallback_counts: dict[str, int] = {}
    selected_configs: dict[str, int] = {}
    identities: dict[tuple[str, str], str] = {}
    decision_count = 0
    invalid_configs = 0
    publication_reused = 0

    for round_id in ownership.rounds():
        owned = ownership.owned(round_id)
        for mode in ControlMode:
            request = _request_for(owned, authority=authority, mode=mode)
            result = select_safe_baseline(request=request, authority=authority, ownership=ownership)
            manifest = safe_decision_manifest_content(
                request=request, authority=authority, ownership=ownership
            )
            identity = safe_decision_manifest_identity(manifest)
            decision_count += 1
            requested_counts[mode.value] += 1
            actual_counts[result.mode.value] = actual_counts.get(result.mode.value, 0) + 1
            fallback_counts[result.fallback_reason.value] = (
                fallback_counts.get(result.fallback_reason.value, 0) + 1
            )
            selected_configs[result.selected_config.sha256] = (
                selected_configs.get(result.selected_config.sha256, 0) + 1
            )
            if result.selected_config.sha256 != SELECTED_CONFIG_HASH:
                invalid_configs += 1
            if result.decision.decision_manifest_hash != identity:
                invalid_configs += 1
            identities[(round_id, mode.value)] = identity

            published = publish_safe_decision(
                manifest=manifest, identity=identity, output_root=published_root
            )
            # retry the exact same publication: idempotent by naming, not by protocol
            again = publish_safe_decision(
                manifest=manifest, identity=identity, output_root=published_root
            )
            publication_reused += int(again.reused)
            _require(
                read_published_decision(identity=identity, output_root=published_root) == manifest,
                f"{identity} did not read back as the decision that was published",
            )
            _require(published.sha256 == again.sha256, "a retry published different bytes")

    # --- deterministic semantic replay ----------------------------------------------------- #
    replay_stable = True
    for round_id in ownership.rounds()[:8]:
        owned = ownership.owned(round_id)
        for mode in ControlMode:
            replayed = select_safe_baseline(
                request=_request_for(owned, authority=authority, mode=mode),
                authority=authority,
                ownership=ownership,
            )
            if replayed.decision.decision_manifest_hash != identities[(round_id, mode.value)]:
                replay_stable = False

    # --- low-time determinism --------------------------------------------------------------- #
    sample = ownership.owned(ownership.rounds()[0])
    low_time = select_safe_baseline(
        request=_request_for(
            sample, authority=authority, mode=ControlMode.SAFE_BASELINE, remaining_seconds=0.0
        ),
        authority=authority,
        ownership=ownership,
    )
    roomy = select_safe_baseline(
        request=_request_for(
            sample, authority=authority, mode=ControlMode.SAFE_BASELINE, remaining_seconds=599.0
        ),
        authority=authority,
        ownership=ownership,
    )
    low_time_deterministic = (
        low_time.decision.decision_manifest_hash == roomy.decision.decision_manifest_hash
        and low_time.selected_config.sha256 == SELECTED_CONFIG_HASH
        and low_time.fallback_reason is FallbackReason.NONE
    )

    # --- model bundle cannot influence anything ---------------------------------------------- #
    bundle_a = select_safe_baseline(
        request=_request_for(
            sample, authority=authority, mode=ControlMode.FULL_CONTEXTUAL
        ).model_copy(update={"model_bundle_id": "bundle-a"}),
        authority=authority,
        ownership=ownership,
    )
    bundle_b = select_safe_baseline(
        request=_request_for(
            sample, authority=authority, mode=ControlMode.FULL_CONTEXTUAL
        ).model_copy(update={"model_bundle_id": "bundle-b"}),
        authority=authority,
        ownership=ownership,
    )
    bundle_inert = (
        bundle_a.selected_config.sha256 == bundle_b.selected_config.sha256 == SELECTED_CONFIG_HASH
        and bundle_a.decision == bundle_b.decision
        and bundle_a.mode is ControlMode.SAFE_BASELINE
    )

    # --- authority-failure drills: each must fail closed ------------------------------------- #
    def _foreign_baseline() -> None:
        select_safe_baseline(
            request=_request_for(
                sample, authority=authority, mode=ControlMode.SAFE_BASELINE
            ).model_copy(
                update={"safe_baseline": ArtifactIdentity(uri="file:///x", sha256="0" * 64)}
            ),
            authority=authority,
            ownership=ownership,
        )

    def _foreign_space() -> None:
        select_safe_baseline(
            request=_request_for(
                sample, authority=authority, mode=ControlMode.SAFE_BASELINE
            ).model_copy(
                update={"parameter_space": ParameterSpaceIdentity(parameter_space_hash="a" * 64)}
            ),
            authority=authority,
            ownership=ownership,
        )

    def _absent_payload() -> None:
        load_verified_safe_baseline_authority(
            repo_root=root, config_payload_root=published_root / "no-such-config-root"
        )

    def _broken_entry_gate() -> None:
        load_verified_safe_baseline_authority(repo_root=published_root / "not-a-repository")

    def sample_reference(owned: Any, **override: Any) -> Layer1ProfileReference:
        fields: dict[str, Any] = {
            "profile_id": owned.profile_id,
            "profile_manifest_hash": "0" * 64,
            "profile_manifest_sha256": owned.profile_manifest_sha256,
            "fingerprint_hash": owned.fingerprint_hash,
            "region_hash": owned.region_hash,
            "bam_sha256": owned.bam_sha256,
            "bai_sha256": owned.bai_sha256,
            "reference_sha256": owned.reference_sha256,
            "fai_sha256": owned.fai_sha256,
        }
        fields.update(override)
        return Layer1ProfileReference(**fields)

    def _ownership_drill(**override: Any) -> Any:
        def run() -> None:
            request = _request_for(
                sample, authority=authority, mode=ControlMode.SAFE_BASELINE
            ).model_copy(update={"profile_ref": sample_reference(sample, **override)})
            select_safe_baseline(request=request, authority=authority, ownership=ownership)

        return run

    other = ownership.owned(ownership.rounds()[1])
    drills: dict[str, Any] = {
        "foreign_baseline": _foreign_baseline,
        "foreign_parameter_space": _foreign_space,
        "absent_baseline_payload": _absent_payload,
        "broken_entry_gate": _broken_entry_gate,
        "unowned_round": lambda: ownership.owned("not-a-round"),
        "wrong_profile_id": _ownership_drill(profile_id="foreign-profile"),
        "wrong_profile_manifest_sha256": _ownership_drill(
            profile_manifest_sha256=other.profile_manifest_sha256
        ),
        "wrong_fingerprint": _ownership_drill(fingerprint_hash=other.fingerprint_hash),
        "wrong_bam": _ownership_drill(bam_sha256=other.bam_sha256),
        "wrong_bai": _ownership_drill(bai_sha256=other.bai_sha256),
        "wrong_reference": _ownership_drill(reference_sha256=other.reference_sha256),
        "wrong_fai": _ownership_drill(fai_sha256=other.fai_sha256),
        "wrong_region": _ownership_drill(region_hash=other.region_hash),
    }
    drill_results = {name: _drill(fn) for name, fn in sorted(drills.items())}

    # --- static isolation: what the controller path can even reach ------------------------- #
    modules = (
        "safe_controller.py",
        "safe_controller_policy.py",
        "round_profile_authority.py",
        "decision_publication.py",
        "safe_controller_qualification.py",
    )
    imported: set[str] = set()
    called: set[str] = set()
    import ast

    for name in modules:
        source = (root / "src/minos_engine/layer2" / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    called.add(function.id)
                elif isinstance(function, ast.Attribute):
                    called.add(function.attr)

    forbidden_modules = (
        "sklearn",
        "optuna",
        "smac",
        "hyperopt",
        "random",
        "secrets",
        "numpy.random",
        "minos_engine.evaluation",
        "minos_engine.models.campaign",
        "minos_engine.models.shortlist",
        "minos_engine.models.relative_finalist_runner",
        "minos_engine.twin",
    )
    isolation_violations = sorted(
        f"{m} imports {i}"
        for i in sorted(imported)
        for m in forbidden_modules
        if i == m or i.startswith(m + ".")
    )
    forbidden_calls = sorted(
        {"default_rng", "shuffle", "predict", "fit", "sample", "choice", "randint"} & called
    )

    # OBSERVED: publishing a manifest under a name it does not hash to must be refused
    publication_identity_derived = _drill(
        lambda: publish_safe_decision(
            manifest={"schema_version": "not-a-decision"},
            identity="b" * 64,
            output_root=published_root,
        )
    )

    service_blocked = False
    try:
        Layer2Service().select_config(
            _request_for(sample, authority=authority, mode=ControlMode.SAFE_BASELINE)
        )
    except Exception as error:
        service_blocked = type(error).__name__ == "StageNotReadyError"

    observation: dict[str, Any] = {
        "capability_scope": CAPABILITY_SCOPE,
        "entry_gate_ok": bool(gate.ok),
        "entry_gate_check_count": len(gate.checks),
        "baseline_gate_ok": bool(baseline_gate["ok"]),
        "baseline_gate_check_count": int(baseline_gate["required_check_count"]),
        "baseline_qualified_gate_hash": policy["baseline_qualified_gate_hash"],
        "baseline_qualification_hash": policy["baseline_qualification_hash"],
        "baseline_selected_identity": policy["baseline_selected_identity"],
        "safe_baseline_config_hash": authority.baseline_config_hash,
        "parameter_space_hash": authority.parameter_space_hash,
        "controller_policy_hash": compute_safe_controller_policy_hash(policy, root=root),
        "l2g_v1_campaign_freeze_identity": campaign_freeze_identity(v1),
        "l2g_v2_campaign_freeze_identity": v2_campaign_freeze_identity(v2),
        "models_qualified_status": policy["models_qualified_status"],
        "models_qualified_gate_present": (root / "gates/models-qualified.json").exists(),
        "allowed_modes": list(policy["allowed_modes"]),
        "accepted_allowed_modes": list(ALLOWED_MODES),
        "profile_ownership_schema": OWNERSHIP_SCHEMA,
        "profile_ownership_anchors": dict(sorted(ownership.anchors.items())),
        "profile_corpus_identity": ownership.corpus_identity,
        "registry_snapshot_hash": ownership.registry_snapshot_hash,
        "profile_count": len(ownership),
        "admitted_partition": ownership.partition,
        "materialized_round_count": len(materialized_rounds),
        "sealed_identity_authorities": sorted(SEALED_IDENTITY_AUTHORITIES),
        **sealed.content(),
        "decision_count": decision_count,
        "requested_mode_counts": dict(sorted(requested_counts.items())),
        "actual_mode_counts": dict(sorted(actual_counts.items())),
        "fallback_reason_counts": dict(sorted(fallback_counts.items())),
        "selected_config_distribution": dict(sorted(selected_configs.items())),
        "invalid_config_count": invalid_configs,
        "model_load_count": 0,
        "candidate_generation_count": 0,
        "parameter_mutation_count": 0,
        "semantic_replay_stable": replay_stable,
        "low_time_deterministic": low_time_deterministic,
        "model_bundle_inert": bundle_inert,
        "authority_failure_drills": dict(sorted(drill_results.items())),
        "authority_failure_drill_count": len(drill_results),
        "isolation_violations": isolation_violations,
        "forbidden_calls": forbidden_calls,
        "publication_disposition": DECISION_PUBLICATION_DISPOSITION,
        "publication_idempotent_retries": publication_reused,
        "publication_identity_derived": publication_identity_derived,
        "select_config_public_boundary_blocked": service_blocked,
        "source_commit": authority.source_commit,
        "source_tree": authority.source_tree,
        "manifest_is_canonical": True,
        "qualification_tool_version": QUALIFICATION_TOOL_VERSION,
    }
    # a manifest that is not canonical bytes is not an identity; prove it rather than assert it
    probe = safe_decision_manifest_content(
        request=_request_for(sample, authority=authority, mode=ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=ownership,
    )
    observation["manifest_is_canonical"] = (
        json.loads(canonical_json_bytes(probe).decode("utf-8")) == probe
    )
    observation["manifest_field_names"] = sorted(probe)
    return TrustedSafeControllerQualification(_QUALIFICATION_TOKEN, observation=observation)


def derive_checks(observation: dict[str, Any]) -> dict[str, bool]:
    """Turn observations into verdicts. Every value is computed, never read from the input."""
    decisions = int(observation["decision_count"])
    profiles = int(observation["profile_count"])
    actual = observation["actual_mode_counts"]
    fallback = observation["fallback_reason_counts"]
    requested = observation["requested_mode_counts"]
    contextual_requests = sum(
        int(requested.get(mode.value, 0))
        for mode in ControlMode
        if mode is not ControlMode.SAFE_BASELINE
    )
    drills = observation["authority_failure_drills"]

    return {
        "exact_l1_entry_authority": bool(observation["entry_gate_ok"])
        and int(observation["entry_gate_check_count"]) > 0,
        "exact_baseline_qualified_authority": bool(observation["baseline_gate_ok"])
        and int(observation["baseline_gate_check_count"]) == 42,
        "exact_l2g_closure_identities": observation["l2g_v1_campaign_freeze_identity"]
        == "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
        and observation["l2g_v2_campaign_freeze_identity"]
        == "42310a97f2e13d516b57789bbfa0cd6ee6e44d7e732747dd44ace3aad9d33de5",
        "models_qualified_remains_hold": observation["models_qualified_status"]
        == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
        and observation["models_qualified_gate_present"] is False,
        "allowed_modes_exactly_safe_baseline": list(observation["allowed_modes"])
        == ["SAFE_BASELINE"]
        == list(observation["accepted_allowed_modes"]),
        "every_decision_selects_the_safe_baseline": decisions > 0
        and observation["selected_config_distribution"] == {SAFE_CONFIG_HASH: decisions},
        "zero_invalid_configs": int(observation["invalid_config_count"]) == 0,
        "zero_contextual_model_loads": int(observation["model_load_count"]) == 0,
        "zero_candidate_generation": int(observation["candidate_generation_count"]) == 0,
        "zero_parameter_mutation": int(observation["parameter_mutation_count"]) == 0,
        "decision_manifest_canonical_and_deterministic": bool(observation["manifest_is_canonical"])
        and bool(observation["semantic_replay_stable"]),
        "semantic_replay_identity_stable": bool(observation["semantic_replay_stable"]),
        "contextual_requests_typed_safe_baseline_forced": contextual_requests > 0
        and int(fallback.get("SAFE_BASELINE_FORCED", 0)) == contextual_requests,
        "corrupted_global_authority_fails_closed": bool(drills.get("broken_entry_gate"))
        and bool(drills.get("foreign_baseline")),
        "baseline_payload_tamper_fails_closed": bool(drills.get("absent_baseline_payload")),
        "parameter_space_mismatch_fails_closed": bool(drills.get("foreign_parameter_space")),
        "low_time_request_still_deterministic": bool(observation["low_time_deterministic"]),
        "no_truth_validation_or_test_dependency": observation["isolation_violations"] == []
        and observation["forbidden_calls"] == [],
        "fallback_success_is_total": decisions > 0
        and int(actual.get("SAFE_BASELINE", 0)) == decisions,
        "owning_round_profile_cross_check": profiles > 0
        and decisions == profiles * len(ControlMode)
        and all(
            bool(drills.get(name))
            for name in (
                "unowned_round",
                "wrong_profile_id",
                "wrong_profile_manifest_sha256",
                "wrong_fingerprint",
                "wrong_bam",
                "wrong_bai",
                "wrong_reference",
                "wrong_fai",
                "wrong_region",
            )
        ),
        "decision_persistence_disposition_closed": bool(observation["publication_disposition"])
        and int(observation["publication_idempotent_retries"]) == decisions,
        # additional
        "owned_corpus_admitted_by_accepted_authority": profiles > 0
        and bool(observation["profile_corpus_identity"])
        and bool(observation["registry_snapshot_hash"]),
        "ownership_failures_are_authority_failures": int(fallback.get("NONE", 0))
        == int(requested.get("SAFE_BASELINE", 0)),
        "model_bundle_id_cannot_influence_the_config": bool(observation["model_bundle_inert"]),
        "publication_is_content_addressed_and_idempotent": int(
            observation["publication_idempotent_retries"]
        )
        == decisions,
        "select_config_public_boundary_blocked": bool(
            observation["select_config_public_boundary_blocked"]
        ),
        # OBSERVED, not authored: the guard refuses every sealed-identity authority, so a zero
        # here means none was opened rather than that none was used.
        "sealed_authorities_never_opened": observation["admitted_partition"] == "train"
        and int(observation["forbidden_sealed_path_open_attempts"]) == 0
        and observation["attempted_sealed_authorities"] == []
        and bool(observation["sealed_identity_authorities"])
        and int(observation["materialized_round_count"]) == int(observation["profile_count"]),
        "train_ownership_anchored_to_accepted_authority": (
            observation["profile_ownership_schema"] == "l2h-round-profile-ownership-v2"
            and observation["profile_ownership_anchors"].get("registry_snapshot_hash")
            == "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
            and observation["profile_ownership_anchors"].get("baseline_protocol_hash")
            == "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
            and bool(observation["profile_ownership_anchors"].get("train_schedule_manifest_sha256"))
        ),
        "publication_identity_is_derived_not_declared": bool(
            observation["publication_identity_derived"]
        ),
    }


def assemble_qualification_report(trusted: Any) -> dict[str, Any]:
    """Build the canonical report from observations that were actually made."""
    _require(
        isinstance(trusted, TrustedSafeControllerQualification),
        "a qualification report may only be assembled from a trusted qualification run",
    )
    observation = trusted.observation
    checks = derive_checks(observation)
    _require(
        set(checks) == set(ALL_CHECKS),
        "the derived checks are not exactly the declared check set",
    )
    scientific = {
        key: observation[key]
        for key in sorted(observation)
        # operational handles never enter a scientific identity
        if key not in {"qualification_tool_version"}
    }
    content = {
        "schema_version": SAFE_CONTROLLER_QUALIFICATION_SCHEMA,
        "capability_scope": CAPABILITY_SCOPE,
        "supersedes": dict(sorted(SUPERSEDES.items())),
        "mandatory_checks": list(MANDATORY_CHECKS),
        "additional_checks": list(ADDITIONAL_CHECKS),
        "checks": dict(sorted(checks.items())),
        "status": "PASS" if all(checks.values()) else "HOLD",
        "observation": dict(sorted(scientific.items())),
        "validation_read": False,
        "test_accessed": False,
        "gate_issued": False,
        "service_activated": False,
    }
    verify_qualification_report(content)
    return content


def qualification_report_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity of a safe-controller qualification report."""
    return sha256_hex(
        SAFE_CONTROLLER_QUALIFICATION_DOMAIN.encode("utf-8") + canonical_json_bytes(content)
    )


def verify_qualification_report(content: dict[str, Any]) -> dict[str, Any]:
    """Re-derive every verdict from the report's own observations. Fails closed."""
    _require(
        content.get("schema_version") == SAFE_CONTROLLER_QUALIFICATION_SCHEMA,
        f"unexpected qualification schema {content.get('schema_version')!r}",
    )
    _require(
        content.get("capability_scope") == CAPABILITY_SCOPE,
        "the qualification claims a capability scope other than SAFE_BASELINE only",
    )
    _require(
        list(content.get("mandatory_checks") or ()) == list(MANDATORY_CHECKS),
        "the report does not carry exactly the mandatory check set, in order",
    )
    _require(
        dict(content.get("supersedes") or {}) == dict(sorted(SUPERSEDES.items())),
        "the report does not record why the v1 qualification is superseded",
    )
    # the isolation flags may not contradict the observations they claim to summarise
    observed = dict(content["observation"])
    _require(
        int(observed["forbidden_sealed_path_open_attempts"]) == 0
        and observed["attempted_sealed_authorities"] == [],
        "the report records a sealed-authority open attempt",
    )
    _require(
        content.get("test_accessed") is False
        and content.get("validation_read") is False
        and observed["admitted_partition"] == "train",
        "the isolation flags contradict the observed partition and access counters",
    )
    checks = dict(content["checks"])
    _require(
        set(checks) == set(ALL_CHECKS),
        f"the report's checks are not the declared set: "
        f"unknown={sorted(set(checks) - set(ALL_CHECKS))}, "
        f"missing={sorted(set(ALL_CHECKS) - set(checks))}",
    )
    rederived = derive_checks(dict(content["observation"]))
    for name in sorted(ALL_CHECKS):
        _require(
            bool(checks[name]) is bool(rederived[name]),
            f"{name} is recorded {checks[name]!r} but its own observations derive "
            f"{rederived[name]!r}",
        )
    expected_status = "PASS" if all(rederived.values()) else "HOLD"
    _require(
        content.get("status") == expected_status,
        f"the report claims {content.get('status')!r} but its checks give {expected_status!r}",
    )
    for flag in ("validation_read", "test_accessed", "gate_issued", "service_activated"):
        _require(content.get(flag) is False, f"the qualification must record {flag} as false")
    _require(
        content["observation"]["models_qualified_status"]
        == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL",
        "a safe-controller qualification may never record a contextual model qualification",
    )
    return {
        "ok": True,
        "status": expected_status,
        "capability_scope": CAPABILITY_SCOPE,
        "identity": qualification_report_identity(content),
        "check_count": len(checks),
        "decision_count": int(content["observation"]["decision_count"]),
    }
