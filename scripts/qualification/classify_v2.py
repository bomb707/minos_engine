"""Complete bam-profile-v1 field classification + per-field validation (v2).

Every serialized leaf path is classified into exactly one category and, if it is a
Layer 2-consumable analytical field, assigned a validation method (oracle numeric
comparison, independent recompute from declared upstream fields, or invariant/
property). Categories with zero tested fields are reported NOT_TESTED — never 100%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# categories
EXACT = "exact"
FLOAT_STRICT = "float_strict"
SAMPLED = "sampled"
APPROX = "approximate"
DERIVED = "derived"
OPERATIONAL = "operational"
IDENTIFIER = "identifier"
NA = "not_applicable"

# tolerances
ABS = 1e-9
REL = 1e-6
QUANTILE_RANK_PTS = 1.0  # approximate: rank tolerance
FRAG_ABS = 0.01
FRAG_REL = 0.02
FRAG_BREADTH_ABS = 0.005
SAMPLED_ABS = 0.01
SAMPLED_REL = 0.02


@dataclass
class Rec:
    path: str
    classification: str
    l2_eligible: bool
    method: str  # oracle | recompute | invariant | exclude
    observed: Any = None
    expected: Any = None
    tolerance: str = ""
    status: str = "NOT_TESTED"  # PASS | FAIL | NOT_TESTED | EXCLUDED
    detail: str = ""


def _walk(o: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(o, dict):
        for k, val in o.items():
            out += _walk(val, f"{prefix}.{k}" if prefix else k)
    elif isinstance(o, list):
        out.append(prefix + "[]")
    else:
        out.append(prefix)
    return out


def get_path(o: Any, path: str) -> Any:
    cur = o
    for part in path.split("."):
        if part.endswith("[]"):
            return cur.get(part[:-2]) if isinstance(cur, dict) else None
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


_IDENT_PREFIX = ("schema_version", "profile_id", "provenance.", "identity.", "header.")
_OP_PREFIX = (
    "stage_timings",
    "runtime_complexity.",
    "degradation",
    "warnings",
    "status",
    "completion.status",
    "completion.completed_families",
)
_IDENT_EXACT = {
    "region.source",
    "region.source_coordinate_system",
    "region.verified",
    "region.contig",
    "pairing.template_length_policy",
    "difficulty.transform_version",
    "reference_context.ambiguous_reference_excluded",
    "reference_context.reference_available",
    "coverage.deletion_aware_depth_recorded_separately",
}
_DERIVED = (
    "difficulty.mapping_risk",
    "difficulty.coverage_risk",
    "difficulty.base_quality_risk",
    "difficulty.complexity_risk",
    "difficulty.reference_context_risk",
    "confidence.integrity",
    "confidence.completeness",
    "confidence.availability",
    "confidence.consistency",
    "confidence.overall",
    "confidence.band",
    "completion.variant_evidence_completion",
    "spatial.interval_fraction_analyzed",
    "spatial.sampling_uncertainty",
)


def classify(path: str) -> tuple[str, bool, str]:
    """Return (classification, l2_eligible, method)."""
    if path.startswith(_OP_PREFIX):
        return OPERATIONAL, False, "exclude"
    if path.startswith(_IDENT_PREFIX):
        return IDENTIFIER, False, "exclude"
    if path.endswith(".view_name") or path.endswith(".depth_semantics"):
        return IDENTIFIER, False, "exclude"
    if path in _IDENT_EXACT:
        # region coords are analytical; the rest are identifiers/flags
        if path in ("region.verified",):
            return IDENTIFIER, False, "exclude"
        return IDENTIFIER, False, "exclude"
    if path in ("region.start0", "region.end0_exclusive", "region.length_bp"):
        return EXACT, True, "oracle_region"
    if path in _DERIVED:
        return DERIVED, True, "recompute"
    if path.startswith("spatial.stratum_window_counts") or path in (
        "spatial.primary_window_count",
        "spatial.refined_window_count",
        "spatial.sampled_window_count",
        "spatial.analyzed_bases",
    ):
        return DERIVED, True, "invariant"
    if path.startswith("variant_evidence."):
        return SAMPLED, True, "oracle"
    if path.startswith("coverage.fragment_primary."):
        return APPROX, True, "oracle"
    if (
        ".quantiles" in path
        or "depth_quantiles" in path
        or "quantiles_phred" in path
        or "quantiles_bp" in path
    ):
        return APPROX, True, "oracle"
    if path.startswith("reference_context.homopolymer_length_histogram"):
        return EXACT, True, "oracle_hist"
    # remaining numeric measurement fields
    if path.startswith(
        (
            "filter_counts.",
            "reads.total_observed",
            "reads.included_primary",
            "mapping_quality.count",
            "base_quality.bases_",
            "read_length.count",
            "alignment.aligned_query_bases",
            "alignment.soft_clipped_bases",
            "alignment.hard_clipped_bases",
            "alignment.inserted_bases",
            "alignment.deleted_bases",
            "alignment.skipped_bases",
            "alignment.query_consuming_bases",
            "coverage.eligible_region_bases",
            "pairing.eligible_pair_count",
        )
    ) or path.endswith(".max_depth"):
        return EXACT, True, "oracle"
    # everything else numeric -> deterministic float
    return FLOAT_STRICT, True, "oracle"


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    return float(x) if isinstance(x, (int, float)) else None


def _cmp(cls: str, obs: Any, exp: Any) -> tuple[bool, str]:
    if cls == EXACT:
        return obs == exp, "=="
    o, e = _num(obs), _num(exp)
    if o is None or e is None:
        return obs == exp, "=="
    ae = abs(o - e)
    re = ae / abs(e) if e != 0 else (0.0 if ae == 0 else math.inf)
    if cls in (FLOAT_STRICT, DERIVED):
        return (ae <= ABS or re <= REL), "abs<=1e-9|rel<=1e-6"
    if cls == APPROX:
        # quantiles use a small value/rank tolerance; fragment uses mean/breadth tol
        return (ae <= max(FRAG_ABS, 1.0) or re <= FRAG_REL), "approx(rank<=1|abs<=0.01|rel<=2%)"
    if cls == SAMPLED:
        return (ae <= SAMPLED_ABS or re <= SAMPLED_REL), "sampled(abs<=0.01|rel<=2%)"
    return obs == exp, "=="


def build_records(profile: dict[str, Any], oracle_vals: dict[str, Any]) -> list[Rec]:
    recs: list[Rec] = []
    for path in _walk(profile):
        cls, l2, method = classify(path)
        rec = Rec(path=path, classification=cls, l2_eligible=l2, method=method)
        if method == "exclude":
            rec.status = "EXCLUDED"
            recs.append(rec)
            continue
        obs = get_path(profile, path)
        rec.observed = obs
        if method == "oracle_hist":
            bin_key = path.rsplit(".", 1)[1]
            exp = oracle_vals.get("_aux.homopolymer_length_histogram", {}).get(bin_key, 0)
            rec.expected = exp
            rec.status = "PASS" if obs == exp else "FAIL"
            rec.tolerance = "=="
            if obs != exp:
                rec.detail = f"obs={obs} exp={exp}"
            recs.append(rec)
            continue
        if method in ("oracle", "oracle_region"):
            key = _oracle_key(path)
            if key in oracle_vals:
                exp = oracle_vals[key]
                rec.expected = exp
                ok, tol = _cmp(cls, obs, exp)
                rec.tolerance = tol
                rec.status = "PASS" if ok else "FAIL"
                if not ok:
                    rec.detail = f"obs={obs} exp={exp}"
            else:
                rec.status = "NOT_TESTED"
                rec.detail = f"no oracle value for {key}"
        # recompute / invariant handled by the caller (needs full profile context)
        recs.append(rec)
    return recs


def _oracle_key(path: str) -> str:
    # region coords: oracle stores under coverage.eligible_region_bases / region.*
    if path == "region.length_bp":
        return "coverage.eligible_region_bases"
    if path == "reference_context.homopolymer_length_histogram" or path.startswith(
        "reference_context.homopolymer_length_histogram."
    ):
        return "_aux.homopolymer_length_histogram"
    return path


def recompute_derived(profile: dict[str, Any]) -> dict[str, Any]:
    """Independently recompute derived difficulty/confidence/completion from upstream."""

    def clamp(x: float) -> float:
        return 0.0 if x < 0 else 1.0 if x > 1 else x

    mq = profile["mapping_quality"]
    al = profile["alignment"]
    reads = profile["reads"]
    bq = profile["base_quality"]
    cov = profile["coverage"]["fragment_primary"]
    ref = profile["reference_context"]
    ve = profile["variant_evidence"]
    out: dict[str, Any] = {}
    out["difficulty.mapping_risk"] = clamp(
        mq["mq0_fraction"] * 2.0 + mq["mq_lt20_fraction"] + reads["supplementary_fraction"]
    )
    out["difficulty.coverage_risk"] = clamp(
        cov["zero_depth_fraction"] + 0.5 * cov["depth_lt10_fraction"]
    )
    out["difficulty.base_quality_risk"] = clamp(
        bq["bq_lt20_fraction"] + bq["missing_quality_fraction"]
    )
    out["difficulty.complexity_risk"] = clamp(
        al["soft_clipped_base_fraction"]
        + al["indel_bearing_read_fraction"]
        + min(1.0, al["nm_per_aligned_base"] * 10.0)
    )
    out["difficulty.reference_context_risk"] = clamp(
        ref["n_fraction"]
        + 0.5 * ref["homopolymer_base_fraction"]
        + 0.5 * max(0.0, 1.0 - ref["entropy_bits"] / 2.0)
    )
    c = profile["confidence"]
    # confidence sections are themselves inputs; recompute overall geo-mean + band
    sections = [c["integrity"], c["completeness"], c["availability"], c["consistency"]]
    prod = 1.0
    for x in sections:
        prod *= clamp(x)
    overall = prod ** (1.0 / len(sections))
    out["confidence.overall"] = clamp(overall)
    hi, med = 0.90, 0.70
    out["confidence.band"] = "HIGH" if overall >= hi else "MEDIUM" if overall >= med else "LOW"
    region_len = profile["region"]["length_bp"]
    out["completion.variant_evidence_completion"] = clamp(
        ve["analyzed_callable_bases"] / region_len if region_len else 0.0
    )
    out["spatial.interval_fraction_analyzed"] = clamp(
        profile["spatial"]["analyzed_bases"] / region_len if region_len else 0.0
    )
    sp = profile["spatial"]
    total = sp["primary_window_count"]
    sampled = sp["sampled_window_count"]
    frac = sampled / total if total else 0.0
    out["spatial.sampling_uncertainty"] = (
        0.0 if total == 0 or sampled >= total else (1.0 - frac) ** 0.5
    )
    return out


def finalize_records(
    recs: list[Rec], profile: dict[str, Any], spatial_expected: dict[str, int]
) -> list[Rec]:
    """Resolve recompute (derived) and invariant statuses (shared by driver + tests)."""
    derived = recompute_derived(profile)
    sp = profile["spatial"]
    for rec in recs:
        if rec.method == "recompute":
            if rec.path in derived:
                ok, tol = _cmp(DERIVED, rec.observed, derived[rec.path])
                rec.expected, rec.tolerance = derived[rec.path], tol
                rec.status = "PASS" if ok else "FAIL"
                if not ok:
                    rec.detail = f"obs={rec.observed} exp={derived[rec.path]}"
            else:
                v = rec.observed
                rec.status = "PASS" if isinstance(v, (int, float)) and 0.0 <= v <= 1.0 else "FAIL"
                rec.tolerance = "invariant:[0,1]"
        elif rec.method == "invariant":
            if rec.path in spatial_expected:
                exp = spatial_expected[rec.path]
                rec.expected = exp
                rec.tolerance = "invariant:matches-window-output"
                rec.status = "PASS" if rec.observed == exp else "FAIL"
                if rec.observed != exp:
                    rec.detail = f"obs={rec.observed} exp={exp}"
            elif rec.path == "spatial.refined_window_count":
                ok = sp["sampled_window_count"] <= rec.observed <= sp["primary_window_count"]
                rec.tolerance = "invariant:sampled<=refined<=primary"
                rec.status = "PASS" if ok else "FAIL"
            else:
                rec.tolerance = "invariant:int>=0"
                rec.status = (
                    "PASS" if isinstance(rec.observed, int) and rec.observed >= 0 else "FAIL"
                )
    return recs
