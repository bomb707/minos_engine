"""The SAFE_BASELINE-only controller: one possible config, two failure classes, no model.

The point of most of these tests is negative. A controller that emits the right config on the
happy path is easy; what has to be proved is that it emits *nothing* when an authority is broken,
that no caller input can change the config it selects, and that no learned-inference module is
reachable from it at all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.baseline_selected import SELECTED_CONFIG_HASH
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import StageNotReadyError
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
from minos_engine.layer2.round_profile_authority import (
    VerifiedRoundProfileAuthority,
    load_verified_round_profile_corpus,
)
from minos_engine.layer2.safe_controller import (
    SAFE_DECISION_MANIFEST_SCHEMA,
    SafeBaselineController,
    SafeControllerAuthorityError,
    VerifiedSafeBaselineAuthority,
    load_verified_safe_baseline_authority,
    safe_decision_manifest_content,
    safe_decision_manifest_identity,
    select_safe_baseline,
)
from minos_engine.layer2.safe_controller_policy import (
    ALLOWED_MODES,
    DISABLED_MODES,
    SAFE_CONTROLLER_POLICY_PATH,
    SAFE_CONTROLLER_POLICY_SCHEMA,
    SafeControllerPolicyError,
    compute_safe_controller_policy_hash,
    load_committed_safe_controller_policy,
    safe_controller_policy_content,
    verify_safe_controller_policy,
)
from minos_engine.layer2.service import Layer2Service
from minos_engine.qualification.l2f_accepted_identities import repository_root

ACCEPTED_POLICY_HASH = "638d634834c921f5ba00220caaca59c4b317368767c38bd50cafaad232241fa3"
PARAMETER_SPACE = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"


@pytest.fixture(scope="module")
def authority() -> VerifiedSafeBaselineAuthority:
    return load_verified_safe_baseline_authority(repo_root=repository_root())


@pytest.fixture(scope="module")
def ownership() -> VerifiedRoundProfileAuthority:
    return load_verified_round_profile_corpus(root=repository_root())


def _owned(ownership: VerifiedRoundProfileAuthority, index: int = 0) -> Any:
    return ownership.owned(ownership.rounds()[index])


def _profile(owned: Any, **override: Any) -> Layer1ProfileReference:
    """A reference to a REAL owned profile.

    The earlier synthetic reference satisfied its own identity tuple by construction, which is
    exactly the hole the ownership authority closes: a profile that only agrees with itself is not
    a profile anyone owns.
    """
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
    return Layer1ProfileReference(**fields)


def _request(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    *,
    mode: ControlMode = ControlMode.SAFE_BASELINE,
    model_bundle_id: str | None = None,
    baseline_sha: str | None = None,
    parameter_space: str | None = None,
    remaining_seconds: float = 30.0,
    index: int = 0,
    round_id: str | None = None,
    **profile_override: Any,
) -> DecisionRequest:
    owned = _owned(ownership, index)
    return DecisionRequest(
        round=RoundIdentity(round_id=round_id or owned.round_id),
        profile_ref=_profile(owned, **profile_override),
        parameter_space=ParameterSpaceIdentity(
            parameter_space_hash=parameter_space or authority.parameter_space_hash
        ),
        safe_baseline=ArtifactIdentity(
            uri=authority.baseline_uri, sha256=baseline_sha or authority.baseline_config_hash
        ),
        controller_version="caller-under-test",
        limits=ComputeLimits(
            remaining_seconds=remaining_seconds,
            wall_clock_budget_seconds=600.0,
            cpu_limit=2,
            memory_limit_bytes=1 << 30,
        ),
        model_bundle_id=model_bundle_id,
        requested_mode=mode,
    )


# ---------------------------------------------------------------------------------------- #
# the policy
# ---------------------------------------------------------------------------------------- #
def test_the_committed_policy_verifies_and_allows_exactly_one_mode() -> None:
    policy = load_committed_safe_controller_policy(repository_root())
    assert verify_safe_controller_policy(policy)["ok"] is True
    assert compute_safe_controller_policy_hash(policy) == ACCEPTED_POLICY_HASH
    assert policy["allowed_modes"] == ["SAFE_BASELINE"] == list(ALLOWED_MODES)
    assert sorted(policy["disabled_modes"]) == sorted(DISABLED_MODES)
    assert policy["schema_version"] == SAFE_CONTROLLER_POLICY_SCHEMA


def test_the_committed_policy_is_canonical_and_rederives_from_source() -> None:
    path = repository_root() / SAFE_CONTROLLER_POLICY_PATH
    raw = path.read_bytes()
    assert canonical_json_bytes(json.loads(raw)) == raw
    assert canonical_json_bytes(safe_controller_policy_content()) == raw


def test_the_policy_binds_the_closure_and_claims_no_qualification() -> None:
    policy = load_committed_safe_controller_policy(repository_root())
    assert policy["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert policy["models_qualified_gate_present"] is False
    assert policy["model_bundle_load_authorized"] is False
    assert policy["contextual_research_closed"] is True
    assert (
        policy["l2g_v2_campaign_freeze_identity"]
        == "42310a97f2e13d516b57789bbfa0cd6ee6e44d7e732747dd44ace3aad9d33de5"
    )
    assert (
        policy["l2g_v1_campaign_freeze_identity"]
        == "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
    )
    assert policy["safe_baseline_config_hash"] == SELECTED_CONFIG_HASH
    assert policy["select_config_public_boundary"] == "BLOCKED"
    assert policy["validation_read"] is False and policy["test_accessed"] is False


def _widen_modes(content: dict[str, Any]) -> None:
    content["allowed_modes"] = ["SAFE_BASELINE", "BOUNDED"]


def _enable_contextual(content: dict[str, Any]) -> None:
    content["contextual_modes_disabled"] = False


def _claim_qualification(content: dict[str, Any]) -> None:
    content["models_qualified_status"] = "PASS"


def _authorize_bundles(content: dict[str, Any]) -> None:
    content["model_bundle_load_authorized"] = True


def _swap_baseline(content: dict[str, Any]) -> None:
    content["safe_baseline_config_hash"] = "0" * 64


def _foreign_space(content: dict[str, Any]) -> None:
    content["parameter_space_hash"] = "a" * 64


def _unblock_boundary(content: dict[str, Any]) -> None:
    content["select_config_public_boundary"] = "OPEN"


POLICY_EDITS = (
    ("widening_the_mode_set", _widen_modes),
    ("enabling_contextual", _enable_contextual),
    ("claiming_qualification", _claim_qualification),
    ("authorizing_bundle_loads", _authorize_bundles),
    ("swapping_the_baseline", _swap_baseline),
    ("foreign_parameter_space", _foreign_space),
    ("unblocking_the_boundary", _unblock_boundary),
)


@pytest.mark.parametrize("label,mutate", POLICY_EDITS, ids=[e[0] for e in POLICY_EDITS])
def test_an_edited_policy_is_refused(label: str, mutate: Any) -> None:
    content = copy.deepcopy(safe_controller_policy_content())
    mutate(content)
    with pytest.raises(SafeControllerPolicyError):
        verify_safe_controller_policy(content)


# ---------------------------------------------------------------------------------------- #
# the decision path
# ---------------------------------------------------------------------------------------- #
def test_a_safe_request_selects_the_exact_accepted_baseline(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    result = select_safe_baseline(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert result.mode is ControlMode.SAFE_BASELINE
    assert result.selected_config.sha256 == SELECTED_CONFIG_HASH
    assert result.decision.config_hash == SELECTED_CONFIG_HASH
    assert result.fallback_reason is FallbackReason.NONE


@pytest.mark.parametrize(
    "mode", [ControlMode.BOUNDED, ControlMode.FULL_CONTEXTUAL, ControlMode.REFINEMENT]
)
def test_a_contextual_request_is_reduced_to_safe_with_a_typed_reason(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    mode: ControlMode,
) -> None:
    """Reduced, not executed, and not silently relabelled as though the mode had run."""
    result = select_safe_baseline(
        ownership=ownership, request=_request(authority, ownership, mode=mode), authority=authority
    )
    assert result.mode is ControlMode.SAFE_BASELINE
    assert result.fallback_reason is FallbackReason.SAFE_BASELINE_FORCED
    assert result.selected_config.sha256 == SELECTED_CONFIG_HASH
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership, mode=mode), authority=authority
    )
    assert manifest["requested_mode"] == mode.value
    assert manifest["actual_mode"] == ControlMode.SAFE_BASELINE.value


def test_baseline_gate_failed_is_never_claimed(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """No baseline-improvement comparison ever existed, so that reason would be a fiction."""
    for mode in ControlMode:
        result = select_safe_baseline(
            ownership=ownership,
            request=_request(authority, ownership, mode=mode),
            authority=authority,
        )
        assert result.fallback_reason is not FallbackReason.BASELINE_GATE_FAILED


def test_a_model_bundle_id_cannot_alter_the_selected_config(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    first = select_safe_baseline(
        ownership=ownership,
        request=_request(authority, ownership, model_bundle_id="bundle-A"),
        authority=authority,
    )
    second = select_safe_baseline(
        ownership=ownership,
        request=_request(authority, ownership, model_bundle_id="bundle-B"),
        authority=authority,
    )
    none_given = select_safe_baseline(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert (
        first.selected_config.sha256
        == second.selected_config.sha256
        == none_given.selected_config.sha256
        == SELECTED_CONFIG_HASH
    )


def test_the_manifest_records_bundle_presence_but_never_the_identifier(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """A bundle handle must not reach the scientific identity, so only a boolean is bound."""
    with_bundle = safe_decision_manifest_content(
        ownership=ownership,
        request=_request(authority, ownership, model_bundle_id="bundle-A"),
        authority=authority,
    )
    other_bundle = safe_decision_manifest_content(
        ownership=ownership,
        request=_request(authority, ownership, model_bundle_id="bundle-B"),
        authority=authority,
    )
    without = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert with_bundle["model_bundle_id_present"] is True
    assert without["model_bundle_id_present"] is False
    assert with_bundle["model_bundle_loaded"] is False
    assert "bundle-A" not in json.dumps(with_bundle)
    # two different bundle ids are the same decision, deliberately
    assert safe_decision_manifest_identity(with_bundle) == safe_decision_manifest_identity(
        other_bundle
    )


def test_low_remaining_time_still_produces_the_same_safe_decision(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    tight = select_safe_baseline(
        ownership=ownership,
        request=_request(authority, ownership, remaining_seconds=0.0),
        authority=authority,
    )
    roomy = select_safe_baseline(
        ownership=ownership,
        request=_request(authority, ownership, remaining_seconds=600.0),
        authority=authority,
    )
    assert tight.selected_config.sha256 == roomy.selected_config.sha256 == SELECTED_CONFIG_HASH
    assert tight.fallback_reason is FallbackReason.NONE
    # compute limits are operational, so they must not move the scientific identity
    assert tight.decision.decision_manifest_hash == roomy.decision.decision_manifest_hash


# ---------------------------------------------------------------------------------------- #
# the manifest
# ---------------------------------------------------------------------------------------- #
def test_the_manifest_is_canonical_and_deterministic(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    first = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    second = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert safe_decision_manifest_identity(first) == safe_decision_manifest_identity(second)
    assert first["schema_version"] == SAFE_DECISION_MANIFEST_SCHEMA


def test_a_different_round_is_a_different_decision(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    a = safe_decision_manifest_content(
        ownership=ownership,
        request=_request(authority, ownership, index=0),
        authority=authority,
    )
    b = safe_decision_manifest_content(
        ownership=ownership,
        request=_request(authority, ownership, index=1),
        authority=authority,
    )
    assert safe_decision_manifest_identity(a) != safe_decision_manifest_identity(b)


MANIFEST_KEYS = frozenset(
    {
        "accepted_prerequisite_identity",
        "actual_mode",
        "attestation_hash",
        "baseline_authority_identity",
        "baseline_config_hash",
        "baseline_payload_sha256",
        "baseline_qualified_gate_hash",
        "caller",
        "chromosome",
        "contextual_research_closed",
        "controller_policy_hash",
        "controller_version",
        "dataset_id",
        "execution_source_commit",
        "execution_source_tree",
        "fallback_reason",
        "guards",
        "l2g_v2_campaign_freeze_identity",
        "model_bundle_id_present",
        "model_bundle_loaded",
        "models_qualified_status",
        "parameter_space_hash",
        "profile_corpus_identity",
        "profile_fingerprint_hash",
        "profile_id",
        "profile_identity_tuple_hash",
        "profile_manifest_sha256",
        "profile_sha256",
        "profile_snapshot_hash",
        "region_hash",
        "registry_snapshot_hash",
        "request_controller_version",
        "requested_mode",
        "round_id",
        "schema_version",
        "selected_config_hash",
    }
)


def test_the_manifest_binds_owned_identity_not_the_unauthenticated_field(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """``profile_manifest_hash`` has no canonical definition, so it may not be an identity input."""
    owned = _owned(ownership)
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert "profile_manifest_hash" not in manifest
    assert manifest["profile_manifest_sha256"] == owned.profile_manifest_sha256
    assert manifest["profile_id"] == owned.profile_id
    assert manifest["attestation_hash"] == owned.attestation_hash
    # the request declared a junk value for the unauthenticated field; it changed nothing
    other = safe_decision_manifest_content(
        ownership=ownership,
        request=_request(authority, ownership, profile_manifest_hash="f" * 64),
        authority=authority,
    )
    assert safe_decision_manifest_identity(other) == safe_decision_manifest_identity(manifest)


def test_the_manifest_carries_exactly_the_expected_fields(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """A whitelist, not a substring scan.

    Scanning the serialised manifest for words like "mutation" flags this module's own
    ``parameter_mutation: false`` guard, and "f1" appears inside hex digests -- so that kind of
    test fails on its own vocabulary while proving nothing. Pinning the exact key set is what
    actually shows no truth, score, VALIDATION or TEST material can be present: a new field cannot
    appear without this test being updated deliberately.
    """
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert set(manifest) == MANIFEST_KEYS
    assert set(manifest["guards"]) == {
        "baseline_payload_verified",
        "candidate_generation",
        "entry_gate_check_count",
        "entry_gate_ok",
        "parameter_mutation",
        "parameter_space_compatible",
        "round_profile_ownership_proven",
    }


def test_the_manifest_holds_no_operational_timestamp(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """An identity that changes every call is not an identity."""
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    for key in manifest:
        assert "time" not in key and "_at" not in key and "duration" not in key


def test_the_manifest_binds_the_controller_and_the_closure(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert manifest["controller_policy_hash"] == ACCEPTED_POLICY_HASH
    assert manifest["selected_config_hash"] == SELECTED_CONFIG_HASH
    assert manifest["parameter_space_hash"] == PARAMETER_SPACE
    assert manifest["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert manifest["contextual_research_closed"] is True
    assert manifest["guards"]["candidate_generation"] is False
    assert manifest["guards"]["parameter_mutation"] is False
    assert manifest["guards"]["parameter_space_compatible"] is True


# ---------------------------------------------------------------------------------------- #
# global authority failures fail CLOSED
# ---------------------------------------------------------------------------------------- #
def _repo_copy(tmp_path: Path) -> Path:
    """A repository whose committed authorities can be tampered with in isolation."""
    import shutil

    root = repository_root()
    target = tmp_path / "repo"
    target.mkdir()
    for relative in ("reports", "manifests", "gates"):
        shutil.copytree(root / relative, target / relative)
    shutil.copytree(root / ".git", target / ".git", symlinks=True)
    return target


def test_a_caller_cannot_mint_an_authority() -> None:
    with pytest.raises(SafeControllerAuthorityError, match="only be minted"):
        VerifiedSafeBaselineAuthority(
            object(),
            policy={},
            policy_hash="a" * 64,
            baseline_config_hash=SELECTED_CONFIG_HASH,
            baseline_payload_sha256="b" * 64,
            baseline_uri="file:///nowhere",
            parameter_space_hash=PARAMETER_SPACE,
            entry_gate_checks={},
            source_commit="0" * 40,
            source_tree="0" * 40,
        )


def test_a_dict_cannot_stand_in_for_an_authority() -> None:
    with pytest.raises(SafeControllerAuthorityError, match="verified safe-baseline authority"):
        select_safe_baseline(
            ownership=ownership,
            request=None,  # type: ignore[arg-type]
            authority={"baseline_config_hash": SELECTED_CONFIG_HASH},  # type: ignore[arg-type]
        )


def test_a_missing_baseline_payload_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SafeControllerAuthorityError, match="payload is missing"):
        load_verified_safe_baseline_authority(
            repo_root=repository_root(), config_payload_root=tmp_path / "absent"
        )


def test_a_tampered_baseline_payload_fails_closed(tmp_path: Path) -> None:
    """One byte changed under the accepted name: the hash is the identity, so this must refuse."""
    from minos_engine.models.config_table import CONFIG_PAYLOAD_ROOT

    payload_root = tmp_path / "configs"
    payload_root.mkdir()
    original = json.loads((CONFIG_PAYLOAD_ROOT / f"{SELECTED_CONFIG_HASH}.json").read_bytes())
    tampered = dict(original)
    key = sorted(k for k, v in tampered.items() if isinstance(v, int))[0]
    tampered[key] = int(tampered[key]) + 1
    (payload_root / f"{SELECTED_CONFIG_HASH}.json").write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(SafeControllerAuthorityError, match="hashes to"):
        load_verified_safe_baseline_authority(
            repo_root=repository_root(), config_payload_root=payload_root
        )


def test_a_non_canonical_payload_fails_closed(tmp_path: Path) -> None:
    from minos_engine.models.config_table import CONFIG_PAYLOAD_ROOT

    payload_root = tmp_path / "configs"
    payload_root.mkdir()
    original = json.loads((CONFIG_PAYLOAD_ROOT / f"{SELECTED_CONFIG_HASH}.json").read_bytes())
    (payload_root / f"{SELECTED_CONFIG_HASH}.json").write_bytes(
        json.dumps(original, indent=2).encode("utf-8")
    )
    with pytest.raises(SafeControllerAuthorityError, match="canonical bytes"):
        load_verified_safe_baseline_authority(
            repo_root=repository_root(), config_payload_root=payload_root
        )


def test_an_edited_committed_policy_fails_closed(tmp_path: Path) -> None:
    root = _repo_copy(tmp_path)
    path = root / SAFE_CONTROLLER_POLICY_PATH
    content = json.loads(path.read_bytes())
    content["allowed_modes"] = ["SAFE_BASELINE", "FULL_CONTEXTUAL"]
    path.write_bytes(canonical_json_bytes(content))
    with pytest.raises((SafeControllerAuthorityError, SafeControllerPolicyError)):
        load_verified_safe_baseline_authority(repo_root=root)


def test_a_request_naming_a_foreign_baseline_fails_closed(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """A caller disagreeing with the authority is not a fallback case; it is a disagreement."""
    with pytest.raises(SafeControllerAuthorityError, match="names safe baseline"):
        select_safe_baseline(
            ownership=ownership,
            request=_request(authority, ownership, baseline_sha="0" * 64),
            authority=authority,
        )


def test_a_request_naming_a_foreign_parameter_space_fails_closed(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    with pytest.raises(SafeControllerAuthorityError, match="parameter space"):
        select_safe_baseline(
            ownership=ownership,
            request=_request(authority, ownership, parameter_space="a" * 64),
            authority=authority,
        )


def test_an_invalid_entry_gate_fails_closed(tmp_path: Path) -> None:
    """The repository-owned L1 gate is the authority; a repo without it cannot produce a config."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SafeControllerAuthorityError, match="entry gate"):
        load_verified_safe_baseline_authority(repo_root=empty)


# ---------------------------------------------------------------------------------------- #
# contextual capability is structurally absent
# ---------------------------------------------------------------------------------------- #
_CONTROLLER_MODULES = ("safe_controller.py", "safe_controller_policy.py")

#: Module paths the safe controller must never reach. Matched on dotted-path segments, so
#: ``minos_engine.models.campaign_freeze`` is not mistaken for ``minos_engine.models.campaign``.
FORBIDDEN_IMPORTS = (
    "minos_engine.models.relative_finalist_runner",
    "minos_engine.models.relative_finalist_authority",
    "minos_engine.models.relative_finalist_protocol",
    "minos_engine.models.campaign",
    "minos_engine.models.shortlist",
    "minos_engine.evaluation",
    "minos_engine.baseline.phase_d_selection",
    "sklearn",
    "optuna",
    "smac",
    "hyperopt",
    "random",
    "secrets",
    "numpy.random",
)


def _imported_modules(name: str) -> set[str]:
    """Every module either controller file imports, function-local imports included."""
    import ast

    source = (repository_root() / "src/minos_engine/layer2" / name).read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _covers(imported: str, forbidden: str) -> bool:
    """Dotted-path containment, never bare substring."""
    return imported == forbidden or imported.startswith(forbidden + ".")


@pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
def test_the_controller_cannot_reach_learned_inference(forbidden: str) -> None:
    """A future accidental contextual call must have nowhere to land."""
    for name in _CONTROLLER_MODULES:
        for imported in _imported_modules(name):
            assert not _covers(imported, forbidden), f"{name} imports {imported}"


def test_the_controller_reaches_no_truth_or_evaluation_surface() -> None:
    for name in _CONTROLLER_MODULES:
        for imported in _imported_modules(name):
            lowered = imported.lower()
            assert "truth" not in lowered
            assert "l2f2_validation" not in lowered
            assert "happy" not in lowered
            assert "scorer" not in lowered


def test_the_controller_makes_no_random_or_estimator_call() -> None:
    """No RNG and no estimator construction anywhere in the call path."""
    import ast

    for name in _CONTROLLER_MODULES:
        source = (repository_root() / "src/minos_engine/layer2" / name).read_text(encoding="utf-8")
        called: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    called.add(function.id)
                elif isinstance(function, ast.Attribute):
                    called.add(function.attr)
        for forbidden in (
            "default_rng",
            "shuffle",
            "seed",
            "predict",
            "fit",
            "sample",
            "choice",
            "uniform",
            "randint",
        ):
            assert forbidden not in called, f"{name} calls {forbidden}()"


def test_the_policy_module_names_exactly_one_allowed_mode() -> None:
    assert ALLOWED_MODES == ("SAFE_BASELINE",)
    assert set(ALLOWED_MODES).isdisjoint(DISABLED_MODES)
    assert sorted(DISABLED_MODES) == ["BOUNDED", "FULL_CONTEXTUAL", "REFINEMENT"]


# ---------------------------------------------------------------------------------------- #
# the public boundary and the surrounding locks
# ---------------------------------------------------------------------------------------- #
def test_the_public_select_config_boundary_is_still_blocked(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """L2-H builds source authority only; the service stays shut."""
    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(_request(authority, ownership))


def test_the_controller_class_is_not_wired_into_the_service() -> None:
    import inspect

    from minos_engine.layer2 import service

    source = inspect.getsource(service)
    assert "SafeBaselineController" not in source
    assert "select_safe_baseline" not in source


def test_the_controller_class_still_reaches_the_same_decision(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    controller = SafeBaselineController(authority, ownership)
    direct = select_safe_baseline(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert controller.decide(_request(authority, ownership)).decision == direct.decision


def test_models_qualified_remains_absent() -> None:
    assert not (repository_root() / "gates/models-qualified.json").exists()


def test_the_l2g_freezes_are_byte_identical() -> None:
    import hashlib

    root = repository_root()
    for relative, expected in (
        (
            "reports/layer2/l2g-v2-train-oof-campaign-freeze-v1.json",
            "17c0a56afaab1e9ad9e26046f9fa4126f8395643359ce34cefcffd8002dfd188",
        ),
        (
            "reports/layer2/l2g-v2-prefit-authority.json",
            "6b2edd38eeee0765c96a2b55083fa534647631c2e449bf702b9d4204f0f894d8",
        ),
    ):
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected


# ---------------------------------------------------------------------------------------- #
# the RUNTIME loader refuses a tampered committed policy (not merely a source test)
# ---------------------------------------------------------------------------------------- #
def _tamper_policy(root: Path, mutate: Any, *, canonical: bool = True) -> None:
    """Edit the committed policy inside a coherent repository copy, then re-canonicalise."""
    path = root / SAFE_CONTROLLER_POLICY_PATH
    content = json.loads(path.read_bytes())
    mutate(content)
    if canonical:
        path.write_bytes(canonical_json_bytes(content))
    else:
        path.write_bytes(json.dumps(content, indent=2).encode("utf-8"))


def _wrong_v1_freeze(content: dict[str, Any]) -> None:
    content["l2g_v1_campaign_freeze_identity"] = content["l2g_v2_campaign_freeze_identity"]


def _wrong_v2_freeze(content: dict[str, Any]) -> None:
    content["l2g_v2_campaign_freeze_identity"] = content["l2g_v1_campaign_freeze_identity"]


def _edit_nested_prerequisite(content: dict[str, Any]) -> None:
    content["accepted_prerequisites"]["l1_gate_hash"] = "e" * 64


def _drop_prerequisites(content: dict[str, Any]) -> None:
    del content["accepted_prerequisites"]


def _edit_parameter_space_schema(content: dict[str, Any]) -> None:
    content["parameter_space_schema"] = "l2f-gatk-live-parameter-space-v2"


def _edit_config_schema(content: dict[str, Any]) -> None:
    content["config_schema"] = "some-other-config-schema-v1"


def _edit_refinement_scope(content: dict[str, Any]) -> None:
    content["refinement_disabled_scope"] = "NONE"


def _edit_disabled_reason(content: dict[str, Any]) -> None:
    content["contextual_disabled_reason"] = "contextual modes are fine actually"


def _add_unknown_key(content: dict[str, Any]) -> None:
    content["contextual_override_allowed"] = True


def _drop_required_key(content: dict[str, Any]) -> None:
    del content["models_qualified_status"]


def _quietly_widen(content: dict[str, Any]) -> None:
    """Canonical bytes, valid JSON, scientifically a different policy entirely."""
    content["allowed_modes"] = ["SAFE_BASELINE", "FULL_CONTEXTUAL"]
    content["disabled_modes"] = ["BOUNDED", "REFINEMENT"]
    content["contextual_modes_disabled"] = False


def _swap_baseline_identity(content: dict[str, Any]) -> None:
    content["baseline_selected_identity"] = "d" * 64


POLICY_TAMPERS: tuple[tuple[str, Any, bool], ...] = (
    ("wrong_v1_freeze_identity", _wrong_v1_freeze, True),
    ("wrong_v2_freeze_identity", _wrong_v2_freeze, True),
    ("changed_nested_prerequisite", _edit_nested_prerequisite, True),
    ("deleted_prerequisites", _drop_prerequisites, True),
    ("changed_parameter_space_schema", _edit_parameter_space_schema, True),
    ("changed_config_schema", _edit_config_schema, True),
    ("changed_refinement_scope", _edit_refinement_scope, True),
    ("changed_disabled_reason", _edit_disabled_reason, True),
    ("unknown_top_level_key", _add_unknown_key, True),
    ("deleted_required_key", _drop_required_key, True),
    ("canonical_but_widened", _quietly_widen, True),
    ("swapped_baseline_identity", _swap_baseline_identity, True),
    ("non_canonical_bytes", lambda content: None, False),
)


@pytest.mark.parametrize(
    "label,mutate,canonical", POLICY_TAMPERS, ids=[t[0] for t in POLICY_TAMPERS]
)
def test_the_runtime_loader_refuses_a_tampered_policy(
    tmp_path: Path, label: str, mutate: Any, canonical: bool
) -> None:
    """The REAL loader, against a coherent repository copy. No substring scanning anywhere.

    Each case rewrites the committed policy as valid canonical JSON, so nothing here is caught by
    a syntax error: the only thing that can refuse them is field-for-field re-derivation from the
    owning authorities.
    """
    root = _repo_copy(tmp_path)
    _tamper_policy(root, mutate, canonical=canonical)
    with pytest.raises((SafeControllerPolicyError, SafeControllerAuthorityError)):
        load_committed_safe_controller_policy(root)
    with pytest.raises((SafeControllerPolicyError, SafeControllerAuthorityError)):
        load_verified_safe_baseline_authority(repo_root=root)


def test_the_untampered_copy_still_verifies(tmp_path: Path) -> None:
    """The tamper harness itself must not be what fails the cases above."""
    root = _repo_copy(tmp_path)
    policy = load_committed_safe_controller_policy(root)
    assert compute_safe_controller_policy_hash(policy, root=root) == ACCEPTED_POLICY_HASH
    authority = load_verified_safe_baseline_authority(repo_root=root)
    assert authority.baseline_config_hash == SELECTED_CONFIG_HASH


def test_exactly_one_policy_document_is_valid_for_an_authority_domain(tmp_path: Path) -> None:
    """The invariant, stated directly: equality with the derived document, or refusal."""
    root = _repo_copy(tmp_path)
    derived = safe_controller_policy_content(root)
    assert verify_safe_controller_policy(derived, root=root)["ok"] is True
    for key in sorted(derived):
        broken = copy.deepcopy(derived)
        del broken[key]
        with pytest.raises(SafeControllerPolicyError):
            verify_safe_controller_policy(broken, root=root)


def test_a_tampered_v2_freeze_breaks_the_policy_derivation(tmp_path: Path) -> None:
    """The freeze is an AUTHORITY for this policy, so a broken freeze cannot yield one."""
    from minos_engine.models.relative_finalist_freeze import V2_FREEZE_PATH

    root = _repo_copy(tmp_path)
    path = root / V2_FREEZE_PATH
    freeze = json.loads(path.read_bytes())
    freeze["research_disposition"] = "CONTEXTUAL_SELECTOR_RESEARCH_REOPENED"
    path.write_bytes(canonical_json_bytes(freeze))
    with pytest.raises(Exception, match="research|freeze|disposition"):
        load_committed_safe_controller_policy(root)


# ---------------------------------------------------------------------------------------- #
# one authority domain, no ambient repository state
# ---------------------------------------------------------------------------------------- #
def test_the_capability_carries_the_provenance_of_the_root_it_verified(
    tmp_path: Path,
) -> None:
    from minos_engine.qualification.provenance import read_provenance

    root = _repo_copy(tmp_path)
    authority = load_verified_safe_baseline_authority(repo_root=root)
    expected = read_provenance(root)
    assert authority.source_commit == expected.head_sha
    assert authority.source_tree == expected.tree_sha


def test_the_manifest_provenance_comes_from_the_capability_not_a_global_lookup(
    tmp_path: Path,
    ownership: VerifiedRoundProfileAuthority,
) -> None:
    root = _repo_copy(tmp_path)
    authority = load_verified_safe_baseline_authority(repo_root=root)
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    assert manifest["execution_source_commit"] == authority.source_commit
    assert manifest["execution_source_tree"] == authority.source_tree


def test_no_global_repository_lookup_survives_in_the_controller() -> None:
    """After minting, nothing may resolve a repository again."""
    import ast

    source = (repository_root() / "src/minos_engine/layer2/safe_controller.py").read_text(
        encoding="utf-8"
    )
    called = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "repository_root" not in called


def test_the_manifest_binds_no_filesystem_path(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """A path is operational; it must never enter a scientific identity."""
    manifest = safe_decision_manifest_content(
        ownership=ownership, request=_request(authority, ownership), authority=authority
    )
    for value in json.dumps(manifest).split('"'):
        assert not value.startswith("/")
        assert "file://" not in value


def test_the_parameter_space_authority_is_package_scoped_by_design() -> None:
    """The one authority that is deliberately not root-scoped, asserted rather than assumed.

    ``live_gatk_parameter_space()`` reads two fixed committed paths from the installed source and
    refuses caller-supplied documents outright. Making it root-scoped would mean weakening that
    refusal, so the boundary is pinned here instead of being left to a reader to discover.
    """
    import inspect

    from minos_engine.experiments.gatk_live_space import load_committed_live_gatk_parameter_space

    assert inspect.signature(load_committed_live_gatk_parameter_space).parameters == {}
