"""``SAFE-CONTROLLER-FROZEN`` — issuing and verifying the mode-scoped controller gate.

**Why this exists rather than the generic gate path.** ``GateArtifact`` already enforces that a
PASS gate carries every registered required check and that none is false. What it cannot do is ask
*where those booleans came from*. Building the gate from ``qualification["checks"]`` would make
"a document containing 29 true values" the whole authority — and the superseded v2 report contains
several identically named corrected checks, all true. It would mint this gate. So "all required
names are true" is not authority, and this module never treats it as such.

The only path to a gate runs forwards from bytes:

    accepted v3 bytes -> exact file SHA -> canonical bytes -> domain-separated identity
      -> exact schema -> exact S3 source commit/tree -> current v3 verifier PASS
      -> capability SAFE_BASELINE_ONLY -> DERIVE the checks

and every check is derived either from an authenticated v3 observation or from an authority
re-verified independently in this process. A caller-supplied ``{"everything": True}`` reaches
nothing.

**Capability.** This gate authorizes exactly ``SAFE_BASELINE``. It is not ``CONTROLLER-FROZEN``,
which would name contextual capability and therefore require MODELS-QUALIFIED; and it does not
satisfy MODELS-QUALIFIED, which does not exist. Several of its own required checks assert the
*absence* of contextual capability, so reading it as evidence of one inverts it.

**Two different sources.** ``qualified_source_git_sha`` / ``qualified_source_tree_sha`` name the
controller source that actually underwent the 200-decision qualification (S3). ``engine_git_sha``
names the checkout that issued the gate. The repository already separates these two fields; this
module keeps them separate rather than letting the issuing commit quietly become the qualified one.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError

__all__ = [
    "ACCEPTED_V3_FILE_SHA256",
    "ACCEPTED_V3_IDENTITY",
    "ACCEPTED_V3_SOURCE_COMMIT",
    "ACCEPTED_V3_SOURCE_TREE",
    "CAPABILITY_SCOPE",
    "SAFE_CONTROLLER_FROZEN_GATE",
    "SAFE_CONTROLLER_FROZEN_GATE_PATH",
    "AuthenticatedSafeControllerQualification",
    "SafeControllerGateError",
    "assemble_safe_controller_frozen_gate",
    "authenticate_v3_qualification",
    "derive_gate_checks",
    "reverify_controller_authorities",
    "verify_safe_controller_frozen_gate",
]

SAFE_CONTROLLER_FROZEN_GATE: Final = "SAFE-CONTROLLER-FROZEN"
SAFE_CONTROLLER_FROZEN_GATE_PATH: Final = "gates/safe-controller-frozen.json"
GATE_TOOL_VERSION: Final = "l2h-safe-controller-frozen-issuer-v1"

#: Exactly one qualification report may mint this gate. Pinned by identity AND by bytes.
ACCEPTED_V3_SCHEMA: Final = "l2h-safe-controller-qualification-v3"
ACCEPTED_V3_IDENTITY: Final = "a0e8840dbdad6beeca7e7548b500868a860438dbea5673b08b11dae70832ba8e"
ACCEPTED_V3_FILE_SHA256: Final = "90fb7b5f819eee2f414d8b55adfa7bbbd8164eadd874d3354393bb8f093ec7da"
#: The controller source that actually underwent the 200-decision qualification.
ACCEPTED_V3_SOURCE_COMMIT: Final = "7d064fe8bc7185bd5c07d16f1fec9dfdd21970b0"
ACCEPTED_V3_SOURCE_TREE: Final = "799cd5cc493bd79f5ad6e5ecc78033eada5a9f5b"

CAPABILITY_SCOPE: Final = "SAFE_BASELINE_ONLY"

SAFE_CONFIG_HASH: Final = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"

#: Capabilities this gate explicitly does NOT confer, recorded so no reader has to infer them.
NOT_AUTHORIZED: Final[tuple[str, ...]] = (
    "MODELS-QUALIFIED",
    "CONTROLLER-FROZEN",
    "BOUNDED",
    "FULL_CONTEXTUAL",
    "REFINEMENT",
)


class SafeControllerGateError(MinosEngineError):
    """The SAFE-CONTROLLER-FROZEN gate could not be issued or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeControllerGateError(message)


_AUTHENTICATED_TOKEN: Final = object()


class AuthenticatedSafeControllerQualification:
    """The exact accepted v3 report, authenticated from its bytes forwards.

    Minted only by :func:`authenticate_v3_qualification`. Holding one means the file SHA, the
    canonical bytes, the domain-separated identity, the schema, the qualified source and the
    current verifier all agreed -- not that a document said so.
    """

    __slots__ = ("_report", "file_sha256", "identity", "source_commit", "source_tree")

    def __init__(
        self,
        token: object,
        *,
        report: dict[str, Any],
        identity: str,
        file_sha256: str,
        source_commit: str,
        source_tree: str,
    ) -> None:
        if token is not _AUTHENTICATED_TOKEN:
            raise SafeControllerGateError(
                "a qualification may only be authenticated by reading and verifying its accepted "
                "bytes; a dictionary of true checks is not evidence"
            )
        self._report = copy.deepcopy(report)
        self.identity = identity
        self.file_sha256 = file_sha256
        self.source_commit = source_commit
        self.source_tree = source_tree

    @property
    def report(self) -> dict[str, Any]:
        return copy.deepcopy(self._report)

    @property
    def observation(self) -> dict[str, Any]:
        return copy.deepcopy(self._report["observation"])


def authenticate_v3_qualification(root: Any = None) -> AuthenticatedSafeControllerQualification:
    """Authenticate the accepted v3 qualification. This is the ONLY door to the gate."""
    from minos_engine.layer2.safe_controller_policy import _resolve_root
    from minos_engine.layer2.safe_controller_qualification import (
        SAFE_CONTROLLER_QUALIFICATION_PATH,
        qualification_report_identity,
        verify_qualification_report,
    )

    base = _resolve_root(root)
    path = base / SAFE_CONTROLLER_QUALIFICATION_PATH
    _require(path.is_file(), f"the accepted qualification is missing: {path}")
    raw = path.read_bytes()

    # BYTES first. A report that is not the accepted bytes is not the accepted report, whatever
    # its checks say.
    actual_sha = hashlib.sha256(raw).hexdigest()
    _require(
        actual_sha == ACCEPTED_V3_FILE_SHA256,
        f"the qualification file hashes to {actual_sha}, not the accepted "
        f"{ACCEPTED_V3_FILE_SHA256}",
    )
    report = json.loads(raw)
    _require(
        canonical_json_bytes(report) == raw,
        "the accepted qualification is not canonical bytes",
    )
    _require(
        report.get("schema_version") == ACCEPTED_V3_SCHEMA,
        f"the qualification schema is {report.get('schema_version')!r}, not {ACCEPTED_V3_SCHEMA}",
    )
    identity = qualification_report_identity(report)
    _require(
        identity == ACCEPTED_V3_IDENTITY,
        f"the qualification identity is {identity}, not the accepted {ACCEPTED_V3_IDENTITY}",
    )

    observation = report["observation"]
    _require(
        observation.get("source_commit") == ACCEPTED_V3_SOURCE_COMMIT
        and observation.get("source_tree") == ACCEPTED_V3_SOURCE_TREE,
        "the qualification names a different controller source than the accepted one",
    )
    _require(
        report.get("capability_scope") == CAPABILITY_SCOPE,
        f"the qualification claims capability {report.get('capability_scope')!r}, not "
        f"{CAPABILITY_SCOPE}",
    )
    _require(report.get("status") == "PASS", "the qualification did not pass")
    _require(
        report.get("gate_issued") is False and report.get("service_activated") is False,
        "the qualification already claims a gate or an activated service",
    )

    # the CURRENT verifier, which re-derives every check from the report's own observations
    verified = verify_qualification_report(report)
    _require(bool(verified["ok"]) and verified["status"] == "PASS", "the qualification failed")
    _require(
        verified["capability_scope"] == CAPABILITY_SCOPE,
        "the verified capability scope is not SAFE_BASELINE only",
    )

    return AuthenticatedSafeControllerQualification(
        _AUTHENTICATED_TOKEN,
        report=report,
        identity=identity,
        file_sha256=actual_sha,
        source_commit=str(observation["source_commit"]),
        source_tree=str(observation["source_tree"]),
    )


def reverify_controller_authorities(root: Any = None) -> dict[str, Any]:
    """Recompute every critical authority in this process. No string is taken on trust.

    The v3 report records these identities, but a report that recorded the wrong ones would still
    be internally consistent. They are therefore recomputed from their owning modules here, and
    the gate is refused if any disagrees. No sealed member manifest is opened: the
    BASELINE-QUALIFIED gate is authenticated as an artifact rather than through its own verifier,
    which reads the split manifest.
    """
    from minos_engine.baseline.baseline_selected import (
        SELECTED_CONFIG_HASH,
        compute_baseline_selected_hash,
    )
    from minos_engine.experiments.gatk_live_space import live_gatk_parameter_space
    from minos_engine.gates.required_checks import required_checks_for
    from minos_engine.gates.verifier import load_gate
    from minos_engine.layer2.safe_controller_policy import (
        _resolve_root,
        compute_safe_controller_policy_hash,
        load_committed_safe_controller_policy,
    )
    from minos_engine.models.campaign_freeze import (
        CAMPAIGN_FREEZE_PATH,
        campaign_freeze_identity,
        verify_campaign_freeze,
    )
    from minos_engine.models.contract import (
        BASELINE_QUALIFICATION_HASH,
        BASELINE_QUALIFIED_GATE_HASH,
    )
    from minos_engine.models.relative_finalist_freeze import (
        MODELS_QUALIFIED_STATUS_HOLD,
        V2_FREEZE_PATH,
        v2_campaign_freeze_identity,
        verify_v2_campaign_freeze,
    )

    base = _resolve_root(root)

    policy = load_committed_safe_controller_policy(base)
    policy_hash = compute_safe_controller_policy_hash(policy, root=base)

    baseline_gate = load_gate(base / "gates/baseline-qualified.json")
    _require(
        baseline_gate.compute_hash() == BASELINE_QUALIFIED_GATE_HASH == baseline_gate.gate_hash
        and baseline_gate.status.value == "PASS"
        and set(baseline_gate.mandatory_checks) == required_checks_for("BASELINE-QUALIFIED")
        and all(baseline_gate.mandatory_checks.values()),
        "the BASELINE-QUALIFIED gate does not reverify against its accepted source constant",
    )
    _require(
        baseline_gate.input_hashes.get("qualification_hash") == BASELINE_QUALIFICATION_HASH,
        "the BASELINE-QUALIFIED gate does not bind the accepted qualification hash",
    )

    v1 = json.loads((base / CAMPAIGN_FREEZE_PATH).read_bytes())
    verify_campaign_freeze(v1)
    v2 = json.loads((base / V2_FREEZE_PATH).read_bytes())
    verify_v2_campaign_freeze(v2)

    return {
        "safe_controller_policy_hash": policy_hash,
        "safe_baseline_config_hash": SELECTED_CONFIG_HASH,
        "baseline_selected_identity": compute_baseline_selected_hash(),
        "baseline_qualified_gate_hash": baseline_gate.compute_hash(),
        "baseline_qualification_hash": BASELINE_QUALIFICATION_HASH,
        "parameter_space_hash": live_gatk_parameter_space().parameter_space_hash,
        "l2g_v1_campaign_freeze_identity": campaign_freeze_identity(v1),
        "l2g_v2_campaign_freeze_identity": v2_campaign_freeze_identity(v2),
        "models_qualified_status": MODELS_QUALIFIED_STATUS_HOLD,
        "models_qualified_gate_present": (base / "gates/models-qualified.json").exists(),
        "controller_frozen_gate_present": (base / "gates/controller-frozen.json").exists(),
        "policy_allowed_modes": list(policy["allowed_modes"]),
        "select_config_blocked": _select_config_is_blocked(),
    }


def _select_config_is_blocked() -> bool:
    """Observed, not assumed: the public boundary must still refuse."""
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    except Exception:
        return False
    return False


def derive_gate_checks(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> dict[str, bool]:
    """Derive every required check from authenticated evidence and reverified authorities.

    Nothing is copied from ``report["checks"]``. Each value below is computed here, so a report
    whose checks disagree with its own observations -- or whose observations disagree with this
    process's view of the authorities -- cannot produce a PASS gate.
    """
    from minos_engine.gates.required_checks import required_checks_for

    report = authenticated.report
    observation = authenticated.observation
    decisions = int(observation["decision_count"])
    profiles = int(observation["profile_count"])
    requested = observation["requested_mode_counts"]
    fallback = observation["fallback_reason_counts"]
    drills = observation["authority_failure_drills"]
    anchors = observation["profile_ownership_anchors"]
    contextual = sum(int(count) for mode, count in requested.items() if mode != "SAFE_BASELINE")

    checks: dict[str, bool] = {
        # --- entry authority, reverified in this process ------------------------------------ #
        "exact_l1_entry_authority": bool(observation["entry_gate_ok"])
        and int(observation["entry_gate_check_count"]) > 0,
        "exact_baseline_qualified_authority": bool(observation["baseline_gate_ok"])
        and int(observation["baseline_gate_check_count"]) == 42
        and observation["baseline_qualified_gate_hash"]
        == authorities["baseline_qualified_gate_hash"]
        and observation["baseline_qualification_hash"] == authorities["baseline_qualification_hash"]
        and observation["baseline_selected_identity"] == authorities["baseline_selected_identity"],
        "exact_l2g_closure_identities": observation["l2g_v1_campaign_freeze_identity"]
        == authorities["l2g_v1_campaign_freeze_identity"]
        and observation["l2g_v2_campaign_freeze_identity"]
        == authorities["l2g_v2_campaign_freeze_identity"],
        "models_qualified_remains_hold": observation["models_qualified_status"]
        == authorities["models_qualified_status"]
        and authorities["models_qualified_gate_present"] is False
        and observation["models_qualified_gate_present"] is False,
        # --- capability scope ---------------------------------------------------------------- #
        "allowed_modes_exactly_safe_baseline": list(observation["allowed_modes"])
        == ["SAFE_BASELINE"]
        == list(authorities["policy_allowed_modes"])
        and observation["controller_policy_hash"] == authorities["safe_controller_policy_hash"],
        "contextual_requests_typed_safe_baseline_forced": contextual > 0
        and int(fallback.get("SAFE_BASELINE_FORCED", 0)) == contextual,
        "zero_contextual_model_loads": int(observation["model_load_count"]) == 0,
        "zero_candidate_generation": int(observation["candidate_generation_count"]) == 0,
        "model_bundle_id_cannot_influence_the_config": bool(observation["model_bundle_inert"]),
        # --- scientific output ----------------------------------------------------------------#
        "every_decision_selects_the_safe_baseline": decisions > 0
        and observation["selected_config_distribution"]
        == {authorities["safe_baseline_config_hash"]: decisions}
        and authorities["safe_baseline_config_hash"] == SAFE_CONFIG_HASH,
        "zero_invalid_configs": int(observation["invalid_config_count"]) == 0,
        "zero_parameter_mutation": int(observation["parameter_mutation_count"]) == 0
        and observation["parameter_space_hash"] == authorities["parameter_space_hash"],
        "fallback_success_is_total": decisions > 0
        and int(observation["actual_mode_counts"].get("SAFE_BASELINE", 0)) == decisions,
        # --- identity ------------------------------------------------------------------------ #
        "decision_manifest_canonical_and_deterministic": bool(observation["manifest_is_canonical"])
        and bool(observation["semantic_replay_stable"]),
        "semantic_replay_identity_stable": bool(observation["semantic_replay_stable"]),
        "owning_round_profile_cross_check": profiles > 0
        and decisions == profiles * 4
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
        "owned_corpus_admitted_by_accepted_authority": profiles > 0
        and bool(observation["profile_corpus_identity"])
        and observation["registry_snapshot_hash"] == anchors.get("registry_snapshot_hash"),
        # --- fail closed ---------------------------------------------------------------------- #
        "corrupted_global_authority_fails_closed": bool(drills.get("broken_entry_gate"))
        and bool(drills.get("foreign_baseline")),
        "baseline_payload_tamper_fails_closed": bool(drills.get("absent_baseline_payload")),
        "parameter_space_mismatch_fails_closed": bool(drills.get("foreign_parameter_space")),
        "ownership_failures_are_authority_failures": int(fallback.get("NONE", 0))
        == int(requested.get("SAFE_BASELINE", 0)),
        "low_time_request_still_deterministic": bool(observation["low_time_deterministic"]),
        # --- isolation and boundary ------------------------------------------------------------ #
        "no_truth_validation_or_test_dependency": observation["isolation_violations"] == []
        and observation["forbidden_calls"] == []
        and report["test_accessed"] is False
        and report["validation_read"] is False,
        "decision_persistence_disposition_closed": bool(observation["publication_disposition"])
        and int(observation["publication_idempotent_retries"]) == decisions,
        "publication_is_content_addressed_and_idempotent": int(
            observation["publication_idempotent_retries"]
        )
        == decisions,
        "select_config_public_boundary_blocked": bool(
            observation["select_config_public_boundary_blocked"]
        )
        and bool(authorities["select_config_blocked"]),
        # --- corrected properties (v3 only) ---------------------------------------------------- #
        "sealed_authorities_never_opened": int(observation["forbidden_sealed_path_open_attempts"])
        == 0
        and int(observation["test_identity_authority_open_attempts"]) == 0
        and int(observation["validation_identity_authority_open_attempts"]) == 0
        and observation["attempted_sealed_authorities"] == []
        and list(observation["guarded_file_apis"]) == ["builtins.open", "io.open", "os.open"]
        and observation["admitted_partition"] == "train",
        "train_ownership_anchored_to_accepted_authority": observation["profile_ownership_schema"]
        == "l2h-round-profile-ownership-v2"
        and anchors.get("phase_a_authority_hash")
        == "9ad0ba48c80e7b305505fea201e93185deb15ae735338086d05b38afcf4deb3f"
        and anchors.get("phase_a_accepted_source_commit")
        == "9395c116e22c52777441d76200acd96a738417bf"
        and anchors.get("phase_a_accepted_source_tree")
        == "fe83142845574a7ae28f7a236e959b56474ed997"
        and anchors.get("train_schedule_manifest_sha256")
        == "694a8993ef64f72ca3705442c1bc070c0288d46e08e00e39bab1155d4415d454",
        "publication_identity_is_derived_not_declared": bool(
            observation["publication_identity_derived"]
        ),
    }
    required = required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    _require(
        set(checks) == set(required),
        f"the derived checks are not exactly the registered set: "
        f"unknown={sorted(set(checks) - set(required))}, "
        f"missing={sorted(set(required) - set(checks))}",
    )
    return checks


def _gate_input_hashes(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> dict[str, str]:
    observation = authenticated.observation
    anchors = observation["profile_ownership_anchors"]
    return {
        "baseline_qualification_hash": authorities["baseline_qualification_hash"],
        "baseline_qualified_gate_hash": authorities["baseline_qualified_gate_hash"],
        "baseline_selected_identity": authorities["baseline_selected_identity"],
        "l2g_v1_campaign_freeze_identity": authorities["l2g_v1_campaign_freeze_identity"],
        "l2g_v2_campaign_freeze_identity": authorities["l2g_v2_campaign_freeze_identity"],
        "ownership_corpus_identity": str(observation["profile_corpus_identity"]),
        "parameter_space_hash": authorities["parameter_space_hash"],
        "phase_a_authority_identity": str(anchors["phase_a_authority_hash"]),
        "qualification_file_sha256": authenticated.file_sha256,
        "qualification_identity": authenticated.identity,
        "qualification_source_commit": authenticated.source_commit,
        "qualification_source_tree": authenticated.source_tree,
        "registry_snapshot_hash": str(anchors["registry_snapshot_hash"]),
        "safe_baseline_config_hash": authorities["safe_baseline_config_hash"],
        "safe_controller_policy_hash": authorities["safe_controller_policy_hash"],
        "train_schedule_manifest_sha256": str(anchors["train_schedule_manifest_sha256"]),
    }


def assemble_safe_controller_frozen_gate(
    authenticated: AuthenticatedSafeControllerQualification,
    authorities: dict[str, Any],
    *,
    engine_git_sha: str,
    root: Any = None,
    created_at: str | None = None,
) -> Any:
    """Build the gate. Only an authenticated qualification may produce one."""
    from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
    from minos_engine.layer2.safe_controller_policy import _resolve_root
    from minos_engine.layer2.safe_controller_qualification import (
        SAFE_CONTROLLER_QUALIFICATION_PATH,
    )

    _require(
        isinstance(authenticated, AuthenticatedSafeControllerQualification),
        "a gate may only be assembled from an authenticated qualification; a dictionary of true "
        "checks is not authority",
    )
    base = _resolve_root(root)
    checks = derive_gate_checks(authenticated, authorities)
    _require(all(checks.values()), "a required check is false; the gate must not be issued")
    _require(
        len(engine_git_sha) == 40,
        "the issuing engine commit must be a full git object name",
    )

    report_path = base / SAFE_CONTROLLER_QUALIFICATION_PATH
    evidence = (
        EvidenceItem(
            description=(
                "SAFE-controller qualification v3 (the 200-decision run this gate promotes)"
            ),
            path=SAFE_CONTROLLER_QUALIFICATION_PATH,
            sha256=authenticated.file_sha256,
            size_bytes=report_path.stat().st_size,
        ),
    )
    return GateArtifact(
        gate_name=SAFE_CONTROLLER_FROZEN_GATE,
        status=GateStatus.PASS,
        # the checkout that ISSUED the gate -- deliberately not the qualified controller source
        engine_git_sha=engine_git_sha,
        input_hashes=_gate_input_hashes(authenticated, authorities),
        evidence=evidence,
        mandatory_checks=dict(sorted(checks.items())),
        # the controller source that actually underwent qualification
        qualified_source_git_sha=authenticated.source_commit,
        qualified_source_tree_sha=authenticated.source_tree,
        qualification_tool_version=GATE_TOOL_VERSION,
        created_at=created_at or "2026-09-07T00:00:00Z",
    )


def verify_safe_controller_frozen_gate(root: Any = None) -> dict[str, Any]:
    """Re-derive the whole gate from the accepted evidence. Fails closed."""
    from minos_engine.gates.required_checks import required_checks_for
    from minos_engine.gates.verifier import load_gate, verify_gate_integrity
    from minos_engine.layer2.safe_controller_policy import _resolve_root

    base = _resolve_root(root)
    path = base / SAFE_CONTROLLER_FROZEN_GATE_PATH
    _require(path.is_file(), f"the SAFE-CONTROLLER-FROZEN gate is missing: {path}")
    gate = load_gate(path)

    _require(
        gate.gate_name == SAFE_CONTROLLER_FROZEN_GATE,
        f"this is a {gate.gate_name} gate, not {SAFE_CONTROLLER_FROZEN_GATE}",
    )
    _require(gate.status.value == "PASS", "the gate is not PASS")
    _require(
        gate.compute_hash() == gate.gate_hash,
        "the gate does not hash to its own recorded identity",
    )
    # Gate-hash integrity only. `verify_gate_integrity(base_dir=...)` re-hashes evidence from the
    # QUALIFIED commit's blobs, which is right when the evidence is an INPUT to the qualification
    # and impossible when it is the qualification's own OUTPUT: the v3 report is what running from
    # S3 produced, so it cannot exist at S3. (The accepted BASELINE-QUALIFIED gate sidesteps this
    # by carrying no evidence at all; §I requires the report to BE evidence, so the bytes are
    # re-hashed here instead -- from the file, against the hash the gate recorded, and against the
    # accepted constant.)
    integrity = verify_gate_integrity(gate, base_dir=None)
    _require(
        bool(integrity.ok),
        "the gate does not verify: " + "; ".join(integrity.reasons or ("unknown",)),
    )
    _require(bool(gate.evidence), "the gate carries no evidence")
    for item in gate.evidence:
        evidence_path = base / item.path
        _require(evidence_path.is_file(), f"evidence is missing: {item.path}")
        _require(not evidence_path.is_symlink(), f"{item.path} is a symlink")
        raw = evidence_path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        _require(
            actual == item.sha256,
            f"evidence {item.path} hashes to {actual}, but the gate records {item.sha256}",
        )
        _require(
            item.size_bytes == len(raw),
            f"evidence {item.path} is {len(raw)} bytes, but the gate records {item.size_bytes}",
        )
    _require(
        any(item.sha256 == ACCEPTED_V3_FILE_SHA256 for item in gate.evidence),
        "the accepted v3 qualification is not among the gate's evidence",
    )
    _require(
        all(SAFE_CONTROLLER_FROZEN_GATE_PATH not in item.path for item in gate.evidence),
        "the gate hashes itself as evidence",
    )

    # THE point: re-authenticate the evidence and re-derive the verdicts, rather than believing
    # the booleans the gate carries.
    authenticated = authenticate_v3_qualification(base)
    authorities = reverify_controller_authorities(base)
    derived = derive_gate_checks(authenticated, authorities)

    required = required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    _require(
        set(gate.mandatory_checks) == set(required),
        f"the gate's checks are not exactly the registered set: "
        f"unknown={sorted(set(gate.mandatory_checks) - set(required))}, "
        f"missing={sorted(set(required) - set(gate.mandatory_checks))}",
    )
    for name in sorted(required):
        _require(
            bool(gate.mandatory_checks[name]) is bool(derived[name]) is True,
            f"{name} is recorded {gate.mandatory_checks[name]!r} but re-derives {derived[name]!r}",
        )
    _require(
        gate.qualified_source_git_sha == authenticated.source_commit
        and gate.qualified_source_tree_sha == authenticated.source_tree,
        "the gate does not name the controller source that was qualified",
    )
    _require(
        gate.input_hashes == _gate_input_hashes(authenticated, authorities),
        "the gate's authority bindings are not what this process derives",
    )
    _require(
        authorities["models_qualified_gate_present"] is False
        and authorities["controller_frozen_gate_present"] is False,
        "a contextual-capability gate exists; this mode-scoped gate may not stand beside one",
    )
    _require(
        bool(authorities["select_config_blocked"]),
        "the public select_config boundary is not blocked",
    )
    return {
        "ok": True,
        "gate_name": SAFE_CONTROLLER_FROZEN_GATE,
        "gate_hash": gate.gate_hash,
        "capability_scope": CAPABILITY_SCOPE,
        "not_authorized": list(NOT_AUTHORIZED),
        "qualified_source_git_sha": gate.qualified_source_git_sha,
        "qualified_source_tree_sha": gate.qualified_source_tree_sha,
        "engine_git_sha": gate.engine_git_sha,
        "qualification_identity": authenticated.identity,
        "check_count": len(gate.mandatory_checks),
    }
