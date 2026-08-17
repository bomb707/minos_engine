"""``minos-engine doctor`` — environment and readiness report (composition only).

Builds a structured health report from domain modules. Does not fetch live
protocol state (that requires a snapshot); live identities are reported as
``unknown`` here rather than invented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from minos_engine import STAGE, __version__
from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.common.versions import IdentityStatus, engine_git_sha, python_version
from minos_engine.intake.reference_registry import ReferenceRegistry
from minos_engine.schema_registry import available_schemas
from minos_engine.settings import Settings

__all__ = ["build_doctor_report"]

_EXPECTED_SCHEMAS = 6
_GATK_PARAM_COUNT = 25


def _l1_ready(gates_dir: Path) -> bool:
    return (gates_dir / "l1-ready.json").exists()


def _layer2_blocked() -> bool:
    from minos_engine.qualification.checks import layer2_blocked

    return layer2_blocked()


def _layer1_status() -> dict[str, Any]:
    from minos_engine.layer1.config import load_layer1_config

    cfg = load_layer1_config()
    return {
        "implemented": True,
        "profiler_version": cfg.profiler_config_version,
        "profiler_config_hash": cfg.config_hash,
        "primary_window_bp": cfg.window.primary_bp,
        "hard_limit_seconds": cfg.budget.hard_seconds,
        "overlap_policy": cfg.coverage.overlap_policy,
    }


def build_doctor_report(
    *,
    settings: Settings | None = None,
    reference_registry: ReferenceRegistry | None = None,
    gates_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = settings or Settings.load()
    ref = reference_registry or ReferenceRegistry()
    gdir = gates_dir or (Path(__file__).resolve().parents[3] / "gates")

    git_sha = engine_git_sha()
    schemas = available_schemas()
    registry_ok = len(REGISTRY) == _GATK_PARAM_COUNT
    schemas_ok = len(schemas) >= _EXPECTED_SCHEMAS
    gatk_only_ok = cfg.runtime_policy.active == "gatk" and cfg.runtime_policy.allowed == ("gatk",)
    l1_ready = _l1_ready(gdir)

    from minos_engine.common.runtime import is_supported_runtime, runtime_report

    checks = {
        "gatk_registry_complete": registry_ok,
        "schemas_available": schemas_ok,
        "gatk_only_policy": gatk_only_ok,
        "truth_isolation_enabled": cfg.engine.truth_isolation_enabled,
        "python_runtime_is_3_12": is_supported_runtime(),
    }
    overall = "healthy" if all(checks.values()) else "degraded"

    runtime = runtime_report()
    return {
        "engine": {
            "stage": STAGE,
            "package_version": __version__,
            "python_version": python_version(),
            "git_sha": git_sha,
            "git_sha_status": (
                IdentityStatus.AVAILABLE.value if git_sha else IdentityStatus.UNAVAILABLE.value
            ),
        },
        "runtime": runtime,
        "caller": {
            "active": cfg.runtime_policy.active,
            "allowed": list(cfg.runtime_policy.allowed),
            "disabled": list(cfg.runtime_policy.disabled),
            "gatk_only_policy": gatk_only_ok,
        },
        "gatk_registry": {
            "parameter_count": len(REGISTRY),
            "expected": _GATK_PARAM_COUNT,
            "registry_hash": REGISTRY.registry_hash(),
            "complete": registry_ok,
        },
        "configuration": {
            "config_dir": str(Path(__file__).resolve().parents[3] / "configs"),
            "round_duration_seconds": cfg.engine.round_duration_seconds,
            "prediction_target_seconds": cfg.engine.prediction_target_seconds,
        },
        "schemas": {"count": len(schemas), "available": list(schemas)},
        "provenance": {
            "upstream_minos_identity_status": IdentityStatus.UNKNOWN.value,
            "scorer_identity_status": IdentityStatus.UNKNOWN.value,
            "note": "live identities require a protocol snapshot; not fetched by doctor",
        },
        "reference_registry": {
            "contigs": list(ref.contigs()),
            "status": "populated" if ref.contigs() else "empty",
        },
        "layer1": _layer1_status(),
        "stage_gates": {
            # A present L1-READY gate is a prerequisite for Layer 2, but Layer 2 is
            # blocked because it is not implemented (its service refuses), which is
            # true regardless of whether the gate exists.
            "l1_ready_gate_present": l1_ready,
            "layer2_blocked": _layer2_blocked(),
        },
        "mandatory_checks": checks,
        "overall_health": overall,
    }
