"""Generate the Layer 2 feature-registry reconciliation artifact (L2-A remediation).

Reconciles the code-owned feature registry against the authoritative Layer 1 v2
analytical inventory and the accepted profiler configuration:

  * every Layer 1 v2 analytical field maps to exactly one registry record
    (data-dependent map bins map to their documentation container);
  * config-bound dynamic-map leaves (quantiles, support/AF site counts) are
    expanded to concrete scalar leaves and each is emitted by the profiler;
  * no container is flagged as a scalar model feature.

Run: ``python -m scripts.qualification.layer2_feature_reconciliation`` (or execute
the file). Writes ``reports/LAYER2_FEATURE_REGISTRY_RECONCILIATION.json`` and exits
non-zero if reconciliation fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from minos_engine.layer2 import feature_registry as FR  # noqa: E402
from minos_engine.layer2.prerequisites import LAYER1_SCHEMA_HASH, PROFILER_CONFIG_HASH  # noqa: E402
from minos_engine.schema_registry import validate_against  # noqa: E402

_V2 = REPO_ROOT / "reports" / "LAYER1_MULTI_DATASET_ACCURACY_RESULTS_V2.json"
_OUT = REPO_ROOT / "reports" / "LAYER2_FEATURE_REGISTRY_RECONCILIATION.json"
_ANALYTIC = {"exact", "float_strict", "approximate", "derived", "sampled"}
_DATA_DEPENDENT = (
    "reference_context.homopolymer_length_histogram",
    "spatial.stratum_window_counts",
)


def build_reconciliation() -> dict[str, object]:
    d = json.loads(_V2.read_text(encoding="utf-8"))
    datasets = d["datasets"]
    v2_union: set[str] = set()
    for ds in datasets:
        v2_union |= {r["path"] for r in ds["field_records"] if r["classification"] in _ANALYTIC}

    reg_paths = {r.field_path for r in FR.FEATURE_REGISTRY}
    bam_scalar = {
        r.field_path
        for r in FR.FEATURE_REGISTRY
        if r.model_feature and r.source_schema == "bam-profile-v1"
    }

    # Missing: a v2 analytical field with no registry mapping (bins -> container).
    missing: set[str] = set()
    for p in v2_union:
        if any(p.startswith(cp + ".") for cp in _DATA_DEPENDENT):
            if p.rsplit(".", 1)[0] not in reg_paths:
                missing.add(p)
        elif p not in reg_paths:
            missing.add(p)

    # Unknown: a bam-profile scalar model feature the profiler never emits.
    unknown = bam_scalar - v2_union
    # Duplicate: any registry path present more than once (guarded at build too).
    counts: dict[str, int] = {}
    for r in FR.FEATURE_REGISTRY:
        counts[r.field_path] = counts.get(r.field_path, 0) + 1
    duplicate = sorted(p for p, c in counts.items() if c > 1)
    # Unclassified: registry record without a valid state (impossible by contract).
    unclassified = [
        r.field_path for r in FR.FEATURE_REGISTRY if r.state.value not in FR.counts_by_state()
    ]
    non_scalar_mf = [
        r.field_path
        for r in FR.FEATURE_REGISTRY
        if r.value_kind.value == "CONTAINER" and r.model_feature
    ]

    window_scalar = sum(
        1 for r in FR.FEATURE_REGISTRY if r.model_feature and r.source_schema == "window-profile-v1"
    )
    analytical_scalar = len(bam_scalar)

    passed = (
        not missing and not duplicate and not unclassified and not unknown and not non_scalar_mf
    )
    return {
        "schema_version": "layer2-feature-reconciliation-v1",
        "accepted_layer1_schema_hash": LAYER1_SCHEMA_HASH,
        "accepted_profiler_config_hash": PROFILER_CONFIG_HASH,
        "registry_hash": FR.REGISTRY_HASH,
        "source_v2_results": _V2.name,
        "serialized_leaf_count": int(d["field_inventory"]["total_serialized_fields"]),
        "analytical_scalar_count": analytical_scalar,
        "window_scalar_count": window_scalar,
        "container_count": len(FR.container_paths()),
        "counts_by_state": FR.counts_by_state(),
        "counts_by_value_kind": FR.counts_by_value_kind(),
        "missing_analytical_scalar_paths": sorted(missing),
        "duplicate_analytical_scalar_paths": duplicate,
        "unclassified_analytical_scalar_paths": sorted(unclassified),
        "unknown_registry_scalar_paths": sorted(unknown),
        "non_scalar_model_feature_paths": sorted(non_scalar_mf),
        "data_dependent_container_parents": list(_DATA_DEPENDENT),
        "passed": passed,
    }


def main() -> int:
    artifact = build_reconciliation()
    validate_against("layer2-feature-reconciliation-v1", artifact)
    _OUT.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {_OUT.relative_to(REPO_ROOT)} passed={artifact['passed']}")
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
