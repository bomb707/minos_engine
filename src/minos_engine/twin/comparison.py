"""Normalize hap.py-style raw results into ComparisonMetrics.

Metric formulas (standard information-retrieval / hap.py definitions):
  precision = TP / (TP + FP)   (0.0 when TP+FP == 0)
  recall    = TP / (TP + FN)   (0.0 when TP+FN == 0)
  F1        = 2·P·R / (P + R)  (0.0 when P+R == 0)

Zero-denominator behavior is deterministic (0.0) and tested. When the raw result
supplies its own metric values, the recomputed values must match within a tight
tolerance or the ingestion fails closed (inconsistent supplied metrics).
Parsing (tools.happy) is isolated from this normalization, which is isolated
from scoring.
"""

from __future__ import annotations

from typing import Any

from minos_engine.common.errors import ComparisonError
from minos_engine.common.hashing import canonical_hash
from minos_engine.intake.contracts import Region
from minos_engine.tools.happy import RawComparison
from minos_engine.twin.identities import ToolIdentity

from .contracts import ComparisonMetrics, VariantClassCounts

__all__ = ["recompute_rates", "build_comparison_metrics"]

_TOL = 1e-9


def recompute_rates(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """Return (precision, recall, f1) with deterministic zero-denominator = 0.0."""
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def _check_supplied(name: str, recomputed: float, supplied: dict[str, float]) -> None:
    if name in supplied and abs(supplied[name] - recomputed) > _TOL:
        raise ComparisonError(
            f"supplied {name} ({supplied[name]}) disagrees with recomputed ({recomputed:.12f})"
        )


def build_comparison_metrics(
    *,
    round_id: str,
    region: Region,
    reference_sha256: str,
    raw: RawComparison,
    truth_vcf_sha256: str,
    query_vcf_sha256: str,
    tool: ToolIdentity,
    raw_payload: dict[str, Any],
) -> ComparisonMetrics:
    """Build normalized, consistency-checked ComparisonMetrics from a raw result."""
    snp = VariantClassCounts(**raw.snp)
    indel = VariantClassCounts(**raw.indel)

    sp, sr, sf = recompute_rates(snp.tp, snp.fp, snp.fn)
    ip, ir, if_ = recompute_rates(indel.tp, indel.fp, indel.fn)

    for name, val in (
        ("snp_precision", sp),
        ("snp_recall", sr),
        ("snp_f1", sf),
        ("indel_precision", ip),
        ("indel_recall", ir),
        ("indel_f1", if_),
    ):
        _check_supplied(name, val, raw.supplied)

    return ComparisonMetrics(
        round_id=round_id,
        region=region,
        reference_sha256=reference_sha256,
        snp=snp,
        indel=indel,
        snp_precision=sp,
        snp_recall=sr,
        snp_f1=sf,
        indel_precision=ip,
        indel_recall=ir,
        indel_f1=if_,
        total_calls=snp.query_total + indel.query_total,
        ti_tv=raw.ti_tv,
        het_hom=raw.het_hom,
        truth_vcf_sha256=truth_vcf_sha256,
        query_vcf_sha256=query_vcf_sha256,
        tool=tool,
        raw_result_hash=canonical_hash(raw_payload),
    )
