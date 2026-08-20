"""E0 derived feature inventory: authoritative counts + feature_values_hash domain proof.

Correction 7 (L2-A erratum): documentation says 147 ELIGIBLE; the EXECUTABLE registry
(production_eligible_fields() at registry hash 0d861270...) yields 141 = 129 bam-profile-v1
+ 12 window-profile-v1. The executable registry is authoritative; FEATURE-READY-v1
selects the 129 BAM-only scalars.
"""

from __future__ import annotations

from collections import Counter

from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.feature_registry import (
    FEATURE_REGISTRY,
    REGISTRY_HASH,
    production_eligible_fields,
)

_RECS = {r.field_path: r for r in FEATURE_REGISTRY}
_ELIGIBLE = production_eligible_fields()
_BAM_ONLY = tuple(p for p in _ELIGIBLE if _RECS[p].source_schema == "bam-profile-v1")


def test_registry_hash_is_the_accepted_identity() -> None:
    assert REGISTRY_HASH == PRE.ACCEPTED_FEATURE_REGISTRY_HASH
    assert REGISTRY_HASH.startswith("0d861270")


def test_eligible_total_is_141_not_147() -> None:
    """The executable registry is authoritative over the documentation figure (147)."""
    assert len(_ELIGIBLE) == 141
    by_schema = Counter(_RECS[p].source_schema for p in _ELIGIBLE)
    assert by_schema == {"bam-profile-v1": 129, "window-profile-v1": 12}


def test_selected_bam_only_set_is_129() -> None:
    assert len(_BAM_ONLY) == 129
    assert len(set(_BAM_ONLY)) == 129
    kinds = Counter(_RECS[p].value_kind.value for p in _BAM_ONLY)
    assert kinds == {"REAL": 84, "FRACTION": 45}  # zero COUNT columns in v1
    # frozen column order for FEATURE-READY-v1 = sorted paths
    assert tuple(sorted(_BAM_ONLY)) == tuple(sorted(set(_BAM_ONLY)))
    for p in _BAM_ONLY:
        assert _RECS[p].state.value == "ELIGIBLE"
        assert _RECS[p].source_schema == "bam-profile-v1"


def test_research_only_candidate_density_excluded() -> None:
    assert not any("candidate_snp_density" in p for p in _BAM_ONLY)


def test_feature_values_hash_domain_is_selected_set() -> None:
    """Correction 6 (behavioral proof only — no source-string matching): the snapshot
    feature_values_hash covers EXACTLY the selected 129 BAM-only ELIGIBLE paths, so it
    is the valid source anchor for L2-E (no separate selected_feature_values_hash)."""
    from minos_engine.layer2.ingest.contracts import (
        canonical_feature_values_hash,
        extract_eligible_feature_values,
    )

    def build_doc() -> dict:
        # a document carrying ALL 141 executable ELIGIBLE fields (129 BAM + 12 window)
        doc: dict = {}
        for path in _ELIGIBLE:
            node = doc
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = 0.5 if _RECS[path].value_kind.value == "FRACTION" else 1.0
        return doc

    window = [p for p in _ELIGIBLE if _RECS[p].source_schema == "window-profile-v1"]
    assert len(window) == 12

    doc = build_doc()
    values = extract_eligible_feature_values(doc)
    # exactly the 129 BAM-only fields; every window field excluded.
    assert set(values) == set(_BAM_ONLY) and len(values) == 129
    assert all(w not in values for w in window)

    base_hash = canonical_feature_values_hash(extract_eligible_feature_values(doc))

    # mutate EVERY window value -> canonical feature hash UNCHANGED (outside domain).
    mutated = build_doc()
    for path in window:
        node = mutated
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = 0.9999
    assert canonical_feature_values_hash(extract_eligible_feature_values(mutated)) == base_hash

    # mutate a single selected BAM value -> hash CHANGES (inside domain).
    mutated2 = build_doc()
    path = _BAM_ONLY[0]
    node = mutated2
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = 0.123456
    assert canonical_feature_values_hash(extract_eligible_feature_values(mutated2)) != base_hash
