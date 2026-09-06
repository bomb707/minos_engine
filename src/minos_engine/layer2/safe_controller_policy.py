"""``l2h-safe-controller-policy-v1`` — the only control mode this engine may currently authorize.

L2-G asked twice whether a model could choose a GATK configuration better than the qualified
baseline: once by predicting utility (v1) and once by predicting advantage over the baseline (v2,
the strictly easier question). Both answered no on TRAIN under criteria fixed before either was
fitted, and the v2 freeze records `CONTEXTUAL_SELECTOR_RESEARCH_CLOSED`.

**This policy is not a downgrade of a failed model into a qualified one.** SAFE_BASELINE was
always a control mode in the Layer 2 architecture and always the fail-safe. What has changed is
only that it is now the *sole* authorizable mode, because the contextual modes require a qualified
model bundle and no such bundle exists. Nothing here claims MODELS-QUALIFIED, and nothing here may
be read as evidence toward it.

The three contextual modes are disabled by construction, not by configuration: this module names
exactly one allowed mode and there is no environment variable, caller argument or override that
can widen the set. Restoring a contextual mode requires a NEW explicitly authorized scientific
program producing a qualified model bundle -- not an edit to this controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.layer2.contracts import ControlMode

__all__ = [
    "ALLOWED_MODES",
    "CONTEXTUAL_DISABLED_REASON",
    "DISABLED_MODES",
    "SAFE_CONTROLLER_POLICY_DOMAIN",
    "SAFE_CONTROLLER_POLICY_PATH",
    "SAFE_CONTROLLER_POLICY_SCHEMA",
    "SafeControllerPolicyError",
    "compute_safe_controller_policy_hash",
    "load_committed_safe_controller_policy",
    "safe_controller_policy_content",
    "verify_safe_controller_policy",
]

SAFE_CONTROLLER_POLICY_SCHEMA: Final = "l2h-safe-controller-policy-v1"
SAFE_CONTROLLER_POLICY_DOMAIN: Final = "minos:l2h-safe-controller-policy:v1\n"
SAFE_CONTROLLER_POLICY_PATH: Final = "reports/layer2/l2h-safe-controller-policy-v1.json"

#: The controller source version. A change here changes the policy hash, which is the point:
#: a decision names the controller that produced it.
SAFE_CONTROLLER_VERSION: Final = "l2h-safe-baseline-controller-v1"

#: EXACTLY one mode. Not a default that something else may widen.
ALLOWED_MODES: Final[tuple[str, ...]] = (ControlMode.SAFE_BASELINE.value,)

DISABLED_MODES: Final[tuple[str, ...]] = (
    ControlMode.BOUNDED.value,
    ControlMode.FULL_CONTEXTUAL.value,
    ControlMode.REFINEMENT.value,
)

CONTEXTUAL_DISABLED_REASON: Final = (
    "contextual control modes require a qualified model bundle; L2-G v1 and v2 both closed with "
    "an empty TRAIN shortlist, MODELS-QUALIFIED is HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL, and "
    "no qualified bundle exists. Restoring a contextual mode requires a new explicitly authorized "
    "scientific program, not a change to this controller."
)

#: REFINEMENT is disabled *for configuration selection*. It is listed with the other contextual
#: modes rather than treated as a lesser case: refining a configuration is still choosing one.
REFINEMENT_DISABLED_SCOPE: Final = "CONFIG_SELECTION"


class SafeControllerPolicyError(MinosEngineError):
    """The safe-controller policy could not be built or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeControllerPolicyError(message)


def _resolve_root(root: Any = None) -> Path:
    """Resolve the authority domain ONCE. Everything downstream must use this same root."""
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    return Path(root) if root is not None else repository_root()


def safe_controller_policy_content(root: Any = None) -> dict[str, Any]:
    """Derive THE policy for one repository authority domain.

    Every field comes from an owning authority resolved against ``root``: nothing is a literal
    copied from a task description, and nothing is read from a different repository than the one
    the caller named. The two campaign freezes are *verified* here rather than merely read,
    because this policy's entire justification is that contextual research is closed.

    The GATK parameter space is the one deliberate exception: ``live_gatk_parameter_space()``
    resolves two fixed committed paths from the installed source package and takes no root by
    design (it refuses caller-supplied documents outright). It is therefore package-scoped rather
    than root-scoped, and that boundary is asserted in the tests rather than left implicit.
    """
    import json

    from minos_engine.baseline.baseline_selected import (
        SELECTED_CONFIG_HASH,
        compute_baseline_selected_hash,
    )
    from minos_engine.experiments.gatk_live_space import (
        LIVE_SPACE_SCHEMA,
        live_gatk_parameter_space,
    )
    from minos_engine.layer2.prerequisites import ACCEPTED
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
        OUTCOME_NO_CONTEXTUAL_SELECTOR,
        RESEARCH_DISPOSITION_CLOSED,
        V2_FREEZE_PATH,
        v2_campaign_freeze_identity,
        verify_v2_campaign_freeze,
    )

    base = _resolve_root(root)

    v2_path = base / V2_FREEZE_PATH
    _require(v2_path.is_file(), f"the L2-G v2 campaign freeze is missing: {v2_path}")
    v2_freeze = json.loads(v2_path.read_bytes())
    verify_v2_campaign_freeze(v2_freeze)
    _require(
        v2_freeze["shortlist"] == []
        and v2_freeze["campaign_outcome"] == OUTCOME_NO_CONTEXTUAL_SELECTOR
        and v2_freeze["research_disposition"] == RESEARCH_DISPOSITION_CLOSED
        and v2_freeze["models_qualified_status"] == MODELS_QUALIFIED_STATUS_HOLD,
        "the v2 freeze does not record a closed contextual research programme",
    )
    _require(
        v2_freeze["production_fallback_config_hash"] == SELECTED_CONFIG_HASH,
        "the v2 freeze's production fallback is not the qualified baseline",
    )

    v1_path = base / CAMPAIGN_FREEZE_PATH
    _require(v1_path.is_file(), f"the L2-G v1 campaign freeze is missing: {v1_path}")
    v1_freeze = json.loads(v1_path.read_bytes())
    verify_campaign_freeze(v1_freeze)

    space = live_gatk_parameter_space()

    return {
        "schema_version": SAFE_CONTROLLER_POLICY_SCHEMA,
        "controller_version": SAFE_CONTROLLER_VERSION,
        "allowed_modes": list(ALLOWED_MODES),
        "disabled_modes": list(DISABLED_MODES),
        "contextual_modes_disabled": True,
        "contextual_disabled_reason": CONTEXTUAL_DISABLED_REASON,
        "refinement_disabled_scope": REFINEMENT_DISABLED_SCOPE,
        "models_qualified_status": MODELS_QUALIFIED_STATUS_HOLD,
        "models_qualified_gate_present": False,
        "model_bundle_load_authorized": False,
        "caller": "gatk",
        "safe_baseline_config_hash": SELECTED_CONFIG_HASH,
        "baseline_selected_identity": compute_baseline_selected_hash(),
        "baseline_qualified_gate_hash": BASELINE_QUALIFIED_GATE_HASH,
        "baseline_qualification_hash": BASELINE_QUALIFICATION_HASH,
        "parameter_space_hash": space.parameter_space_hash,
        "parameter_space_schema": LIVE_SPACE_SCHEMA,
        "config_schema": "l2f-gatk-live-effective-config-v1",
        "l2g_v1_campaign_freeze_identity": campaign_freeze_identity(v1_freeze),
        "l2g_v2_campaign_freeze_identity": v2_campaign_freeze_identity(v2_freeze),
        "contextual_research_closed": True,
        "accepted_prerequisites": dict(sorted(ACCEPTED.model_dump(mode="json").items())),
        "select_config_public_boundary": "BLOCKED",
        "validation_read": False,
        "test_accessed": False,
    }


def compute_safe_controller_policy_hash(
    content: dict[str, Any] | None = None, *, root: Any = None
) -> str:
    """Domain-separated identity of the safe-controller policy."""
    body = content if content is not None else safe_controller_policy_content(root)
    return sha256_hex(SAFE_CONTROLLER_POLICY_DOMAIN.encode("utf-8") + canonical_json_bytes(body))


def _describe_difference(observed: dict[str, Any], expected: dict[str, Any]) -> str:
    """Name what actually differs. Exact equality is the rule; this is only the diagnosis."""
    unknown = sorted(set(observed) - set(expected))
    missing = sorted(set(expected) - set(observed))
    if unknown:
        return f"unknown policy field(s) {unknown}"
    if missing:
        return f"missing policy field(s) {missing}"
    for key in sorted(expected):
        if observed[key] != expected[key]:
            if isinstance(expected[key], dict) and isinstance(observed[key], dict):
                nested = _describe_difference(observed[key], expected[key])
                return f"{key}: {nested}"
            return f"{key} is {observed[key]!r}, but this authority derives {expected[key]!r}"
    return "the documents differ in ordering or type only"


def verify_safe_controller_policy(content: dict[str, Any], *, root: Any = None) -> dict[str, Any]:
    """Require the document to be THE policy this authority domain derives. Exactly one is valid.

    Not a list of spot checks. The previous implementation asserted a handful of fields and only
    required the two campaign-freeze identities to be non-empty, so a document could carry a
    swapped freeze identity, an edited nested prerequisite, a rewritten reason string or an extra
    key and still verify. A committed artifact must not become trusted because a unit test
    compared it to the deriver at some earlier point; the runtime loader has to do that itself,
    every time, against the same authorities.

    So the rule is equality with the derived document, field for field and nested value for
    nested value. Anything omitted, added, reordered into a different value, or altered at any
    depth fails.
    """
    expected = safe_controller_policy_content(root)
    _require(
        isinstance(content, dict),
        f"a policy must be a document, not {type(content).__name__}",
    )
    _require(
        content.get("schema_version") == SAFE_CONTROLLER_POLICY_SCHEMA,
        f"unexpected policy schema {content.get('schema_version')!r}",
    )
    # canonical bytes, so equality cannot be satisfied by a type the encoder would reject
    _require(
        canonical_json_bytes(content) == canonical_json_bytes(expected),
        "the policy is not the one this authority derives: "
        + _describe_difference(content, expected),
    )
    # belt and braces on the two invariants that matter most if the deriver itself is ever edited
    _require(
        list(content["allowed_modes"]) == list(ALLOWED_MODES),
        f"the policy allows {content['allowed_modes']!r}, not exactly {list(ALLOWED_MODES)}",
    )
    _require(
        set(content["allowed_modes"]).isdisjoint(set(content["disabled_modes"])),
        "a mode is both allowed and disabled",
    )
    return {
        "ok": True,
        "schema_version": SAFE_CONTROLLER_POLICY_SCHEMA,
        "policy_hash": compute_safe_controller_policy_hash(content),
        "allowed_modes": list(ALLOWED_MODES),
    }


def load_committed_safe_controller_policy(root: Any = None) -> dict[str, Any]:
    """Read the committed policy for ONE authority domain and prove it is the derived one."""
    import json

    base = _resolve_root(root)
    path = base / SAFE_CONTROLLER_POLICY_PATH
    _require(path.is_file(), f"the safe-controller policy is missing: {path}")
    raw = path.read_bytes()
    content = json.loads(raw)
    _require(
        canonical_json_bytes(content) == raw,
        "the committed safe-controller policy is not canonical bytes",
    )
    verify_safe_controller_policy(content, root=base)
    return dict(content)
