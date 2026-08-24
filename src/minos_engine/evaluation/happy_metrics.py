"""Parse hap.py output into exactly the metric dictionary ``l2f2-minos-scoring-v1`` scores.

This reproduces the audited upstream pipeline at commit
``649bb92c6abccebde58a736a2b2af7fd77a701c1`` (``utils/scoring.py``), in its exact order:

1. ``<prefix>.summary.csv`` — **PASS rows only**, SNP and INDEL, into the base metric names;
2. missing F1/precision/recall keys defaulted to ``0.0``; ``weighted_f1 = 0.7*snp + 0.3*indel``;
3. ``parse_happy_vcf_assessed_metrics`` — recompute ``query_total``, ``Frac_NA``, Ti/Tv and
   het/hom from **assessed** variants only (``BD`` in ``TP``/``FP``), because hap.py's
   summary.csv computes them over the entire query VCF including UNK variants outside the
   assessed regions, which inflates query totals and skews every ratio;
4. ``compute_synthetic_only_metrics`` — recount TP/FP/FN against the target mutations only, then
   ``parse_region_overcall_metrics`` — the full-region false-positive guardrail.

Only the standard library is used: the upstream functions read plain (optionally gzipped) VCF
text, so no VCF library is required and CI needs no upstream checkout.

Where this deliberately differs from upstream: upstream substitutes an all-zero score when its
parsing returns nothing usable. A zero score is a *scientific* statement, so unusable output is
raised as a typed failure here and recorded as ``HAPPY_OUTPUT_INVALID`` instead — a silent zero
would enter the baseline as a real, terrible configuration rather than a broken evaluation.
"""

from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.evaluation.happy_runner import HappyOutputError

__all__ = [
    "INDEL_POSITION_TOLERANCE",
    "ParsedHappyMetrics",
    "compute_mutation_only_metrics",
    "parse_assessed_only_metrics",
    "parse_happy_outputs",
    "parse_region_overcall_metrics",
    "parse_summary_csv",
]

#: upstream ``position_tolerance`` for INDEL matching, which absorbs normalisation differences.
INDEL_POSITION_TOLERANCE = 10

_MIN_VCF_FIELDS = 11

#: an unreadable or corrupt output file is unusable OUTPUT, not a crash: every caller converts
#: these into the bounded HAPPY_OUTPUT_INVALID failure rather than letting them escape untyped.
_UNREADABLE = (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError, ValueError)


@dataclass(frozen=True)
class ParsedHappyMetrics:
    """Every metric family one evaluation produces, kept separable for the artifact."""

    #: the merged dictionary the AdvancedScorer consumes.
    happy_metrics: dict[str, Any]
    mutation_only_metrics: dict[str, Any]
    assessed_only_metrics: dict[str, Any]
    overcall: dict[str, Any]


def _safe_float(value: Any) -> float:
    """Upstream ``safe_float``: blank/``nan``/unparseable becomes ``0.0``."""
    try:
        return float(value) if value and value != "nan" else 0.0
    except (ValueError, TypeError):
        return 0.0


def _open_text(path: Path) -> Any:
    return gzip.open(path, "rt") if str(path).endswith(".gz") else path.open("rt")


def _variant_lines(path: Path) -> Any:
    """Yield ``(fields, fmt_truth, fmt_query)`` for each usable hap.py VCF record."""
    with _open_text(path) as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            fields = line.strip().split("\t")
            if len(fields) < _MIN_VCF_FIELDS:
                continue
            keys = fields[8].split(":")
            yield (
                fields,
                dict(zip(keys, fields[9].split(":"), strict=False)),
                dict(zip(keys, fields[10].split(":"), strict=False)),
            )


def parse_summary_csv(summary_csv: Path) -> dict[str, Any]:
    """Stage 1+2 — the base hap.py metrics from PASS rows only."""
    if summary_csv.is_symlink() or not summary_csv.is_file():
        raise HappyOutputError(f"hap.py summary CSV is missing or unsafe: {summary_csv}")

    metrics: dict[str, Any] = {}
    rows_parsed = 0
    with summary_csv.open("rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            variant_type = row.get("Type", "")
            if variant_type not in ("INDEL", "SNP"):
                continue
            rows_parsed += 1
            if row.get("Filter") != "PASS":
                continue
            if variant_type == "SNP":
                metrics.update(
                    {
                        "precision_snp": _safe_float(row.get("METRIC.Precision", 0)),
                        "recall_snp": _safe_float(row.get("METRIC.Recall", 0)),
                        "f1_snp": _safe_float(row.get("METRIC.F1_Score", 0)),
                        "truth_total_snp": _safe_float(row.get("TRUTH.TOTAL", 0)),
                        "tp_snp": _safe_float(row.get("TRUTH.TP", 0)),
                        "fn_snp": _safe_float(row.get("TRUTH.FN", 0)),
                        "query_total_snp": _safe_float(row.get("QUERY.TOTAL", 0)),
                        "fp_snp": _safe_float(row.get("QUERY.FP", 0)),
                        "query_unk_snp": _safe_float(row.get("QUERY.UNK", 0)),
                        "frac_na_snp": _safe_float(row.get("METRIC.Frac_NA", 0)),
                        "titv_truth_snp": _safe_float(row.get("TRUTH.TOTAL.TiTv_ratio", 0)),
                        "titv_query_snp": _safe_float(row.get("QUERY.TOTAL.TiTv_ratio", 0)),
                        "hethom_truth_snp": _safe_float(row.get("TRUTH.TOTAL.het_hom_ratio", 0)),
                        "hethom_query_snp": _safe_float(row.get("QUERY.TOTAL.het_hom_ratio", 0)),
                    }
                )
            else:
                metrics.update(
                    {
                        "precision_indel": _safe_float(row.get("METRIC.Precision", 0)),
                        "recall_indel": _safe_float(row.get("METRIC.Recall", 0)),
                        "f1_indel": _safe_float(row.get("METRIC.F1_Score", 0)),
                        "truth_total_indel": _safe_float(row.get("TRUTH.TOTAL", 0)),
                        "tp_indel": _safe_float(row.get("TRUTH.TP", 0)),
                        "fn_indel": _safe_float(row.get("TRUTH.FN", 0)),
                        "query_total_indel": _safe_float(row.get("QUERY.TOTAL", 0)),
                        "fp_indel": _safe_float(row.get("QUERY.FP", 0)),
                        "query_unk_indel": _safe_float(row.get("QUERY.UNK", 0)),
                        "frac_na_indel": _safe_float(row.get("METRIC.Frac_NA", 0)),
                        "hethom_truth_indel": _safe_float(row.get("TRUTH.TOTAL.het_hom_ratio", 0)),
                        "hethom_query_indel": _safe_float(row.get("QUERY.TOTAL.het_hom_ratio", 0)),
                    }
                )

    if rows_parsed == 0:
        raise HappyOutputError(
            f"hap.py summary CSV {summary_csv} contained no SNP/INDEL rows; the comparison "
            "produced no usable output"
        )

    for key in (
        "f1_snp",
        "f1_indel",
        "precision_snp",
        "recall_snp",
        "precision_indel",
        "recall_indel",
    ):
        metrics.setdefault(key, 0.0)
    metrics["weighted_f1"] = 0.7 * metrics["f1_snp"] + 0.3 * metrics["f1_indel"]
    return metrics


def parse_assessed_only_metrics(happy_vcf: Path) -> dict[str, Any] | None:
    """Stage 3 — query totals and ratios over ASSESSED variants only.

    ``None`` when the annotated VCF is absent, exactly as upstream, in which case the summary
    values stand. In this engine's flow the mutation-only stage then fails closed on the same
    missing file, so an absent VCF can never quietly become a scored evaluation.
    """
    if happy_vcf.is_symlink() or not happy_vcf.is_file():
        return None

    stats = dict.fromkeys(
        (
            "query_total_snp",
            "query_total_indel",
            "ti_query",
            "tv_query",
            "ti_truth",
            "tv_truth",
            "het_query_snp",
            "hom_query_snp",
            "het_truth_snp",
            "hom_truth_snp",
            "het_query_indel",
            "hom_query_indel",
            "het_truth_indel",
            "hom_truth_indel",
        ),
        0,
    )

    try:
        records = list(_variant_lines(happy_vcf))
    except _UNREADABLE:
        return None

    for _fields, fmt_truth, fmt_query in records:
        if fmt_query.get("BD", ".") in ("TP", "FP"):
            vtype = fmt_query.get("BVT", ".")
            if vtype == "SNP":
                stats["query_total_snp"] += 1
                if fmt_query.get("BI", ".") == "ti":
                    stats["ti_query"] += 1
                elif fmt_query.get("BI", ".") == "tv":
                    stats["tv_query"] += 1
                if fmt_query.get("BLT", ".") == "het":
                    stats["het_query_snp"] += 1
                elif fmt_query.get("BLT", ".") == "homalt":
                    stats["hom_query_snp"] += 1
            elif vtype == "INDEL":
                stats["query_total_indel"] += 1
                if fmt_query.get("BLT", ".") == "het":
                    stats["het_query_indel"] += 1
                elif fmt_query.get("BLT", ".") == "homalt":
                    stats["hom_query_indel"] += 1

        if fmt_truth.get("BD", ".") in ("TP", "FN"):
            vtype = fmt_truth.get("BVT", ".")
            if vtype == "SNP":
                if fmt_truth.get("BI", ".") == "ti":
                    stats["ti_truth"] += 1
                elif fmt_truth.get("BI", ".") == "tv":
                    stats["tv_truth"] += 1
                if fmt_truth.get("BLT", ".") == "het":
                    stats["het_truth_snp"] += 1
                elif fmt_truth.get("BLT", ".") == "homalt":
                    stats["hom_truth_snp"] += 1
            elif vtype == "INDEL":
                if fmt_truth.get("BLT", ".") == "het":
                    stats["het_truth_indel"] += 1
                elif fmt_truth.get("BLT", ".") == "homalt":
                    stats["hom_truth_indel"] += 1

    result: dict[str, Any] = {
        "query_total_snp": stats["query_total_snp"],
        "query_total_indel": stats["query_total_indel"],
        "frac_na_snp": 0.0,
        "frac_na_indel": 0.0,
    }
    # ratios are emitted ONLY when their denominator is non-zero, exactly as upstream: a missing
    # key leaves the summary value in place rather than fabricating a 0.0 ratio.
    if stats["tv_query"] > 0:
        result["titv_query_snp"] = stats["ti_query"] / stats["tv_query"]
    if stats["tv_truth"] > 0:
        result["titv_truth_snp"] = stats["ti_truth"] / stats["tv_truth"]
    if stats["hom_query_snp"] > 0:
        result["hethom_query_snp"] = stats["het_query_snp"] / stats["hom_query_snp"]
    if stats["hom_truth_snp"] > 0:
        result["hethom_truth_snp"] = stats["het_truth_snp"] / stats["hom_truth_snp"]
    if stats["hom_query_indel"] > 0:
        result["hethom_query_indel"] = stats["het_query_indel"] / stats["hom_query_indel"]
    if stats["hom_truth_indel"] > 0:
        result["hethom_truth_indel"] = stats["het_truth_indel"] / stats["hom_truth_indel"]
    return result


def compute_mutation_only_metrics(
    happy_vcf: Path,
    mutations_vcf: Path,
    *,
    position_tolerance: int = INDEL_POSITION_TOLERANCE,
) -> dict[str, Any]:
    """Stage 4a — recount against the target mutations only.

    SNPs match on exact ``(chrom, pos, ref, alt)``; INDELs match on position within tolerance,
    which absorbs left-alignment differences between callers.
    """
    if happy_vcf.is_symlink() or not happy_vcf.is_file():
        raise HappyOutputError(f"hap.py annotated VCF is missing or unsafe: {happy_vcf}")
    if mutations_vcf.is_symlink() or not mutations_vcf.is_file():
        raise HappyOutputError(f"target mutations VCF is missing or unsafe: {mutations_vcf}")

    target_snps: set[tuple[str, int, str, str]] = set()
    target_indels: list[tuple[str, int]] = []
    try:
        with _open_text(mutations_vcf) as handle:
            for line in handle:
                if line.startswith("#"):
                    continue
                parts = line.strip().split("\t")
                if len(parts) < 5:
                    continue
                chrom, pos, ref, alt = parts[0], int(parts[1]), parts[3], parts[4]
                if len(ref) != len(alt):
                    target_indels.append((chrom, pos))
                else:
                    target_snps.add((chrom, pos, ref, alt))
    except _UNREADABLE as exc:
        raise HappyOutputError(
            f"target mutations VCF {mutations_vcf} could not be read: {exc}"
        ) from exc

    if not target_snps and not target_indels:
        raise HappyOutputError(
            f"no target mutations found in {mutations_vcf}; the mutation-only metrics that the "
            "score is computed from cannot be derived"
        )

    counts = dict.fromkeys(("tp_snp", "fp_snp", "fn_snp", "tp_indel", "fp_indel", "fn_indel"), 0)

    def _match_snp(chrom: str, pos: int, ref: str, alt: str) -> bool:
        return (chrom, pos, ref, alt) in target_snps

    def _match_indel(chrom: str, pos: int) -> bool:
        return any(chrom == c and abs(pos - p) <= position_tolerance for c, p in target_indels)

    try:
        records = list(_variant_lines(happy_vcf))
    except _UNREADABLE as exc:
        raise HappyOutputError(
            f"hap.py annotated VCF {happy_vcf} could not be read: {exc}"
        ) from exc

    for fields, fmt_truth, fmt_query in records:
        chrom, pos, ref, alt = fields[0], int(fields[1]), fields[3], fields[4]
        bd_truth = fmt_truth.get("BD", ".")
        if bd_truth in ("TP", "FN"):
            is_snp = fmt_truth.get("BVT", ".") == "SNP"
            matched = _match_snp(chrom, pos, ref, alt) if is_snp else _match_indel(chrom, pos)
            if matched:
                prefix = "tp" if bd_truth == "TP" else "fn"
                counts[f"{prefix}_{'snp' if is_snp else 'indel'}"] += 1
        if fmt_query.get("BD", ".") == "FP":
            is_snp = fmt_query.get("BVT", ".") == "SNP"
            matched = _match_snp(chrom, pos, ref, alt) if is_snp else _match_indel(chrom, pos)
            if matched:
                counts[f"fp_{'snp' if is_snp else 'indel'}"] += 1

    def _f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        return f1, precision, recall

    tp_s, fp_s, fn_s = counts["tp_snp"], counts["fp_snp"], counts["fn_snp"]
    tp_i, fp_i, fn_i = counts["tp_indel"], counts["fp_indel"], counts["fn_indel"]
    f1_snp, precision_snp, recall_snp = _f1(tp_s, fp_s, fn_s)
    f1_indel, precision_indel, recall_indel = _f1(tp_i, fp_i, fn_i)

    return {
        "f1_snp": f1_snp,
        "precision_snp": precision_snp,
        "recall_snp": recall_snp,
        "f1_indel": f1_indel,
        "precision_indel": precision_indel,
        "recall_indel": recall_indel,
        "tp_snp": float(tp_s),
        "fp_snp": float(fp_s),
        "fn_snp": float(fn_s),
        "tp_indel": float(tp_i),
        "fp_indel": float(fp_i),
        "fn_indel": float(fn_i),
        "truth_total_snp": float(tp_s + fn_s),
        "truth_total_indel": float(tp_i + fn_i),
        "query_total_snp": float(tp_s + fp_s),
        "query_total_indel": float(tp_i + fp_i),
        "frac_na_snp": 0.0,
        "frac_na_indel": 0.0,
        "weighted_f1": 0.7 * f1_snp + 0.3 * f1_indel,
    }


def parse_region_overcall_metrics(
    happy_vcf: Path, synthetic_truth_total: float, synthetic_snp_truth_total: float
) -> dict[str, Any] | None:
    """Stage 4b — the full-region false-positive guardrail.

    A caller that sprays variants across the whole region can win on the target mutations while
    being wrong everywhere else; this counts region-wide FPs and converts a sufficiently extreme
    ratio into the score penalty upstream applies.
    """
    if happy_vcf.is_symlink() or not happy_vcf.is_file():
        return None

    region_fp_snp = 0
    region_fp_indel = 0
    try:
        records = list(_variant_lines(happy_vcf))
    except _UNREADABLE:
        return None

    for _fields, _fmt_truth, fmt_query in records:
        if fmt_query.get("BD", ".") == "FP":
            if fmt_query.get("BVT", ".") == "SNP":
                region_fp_snp += 1
            elif fmt_query.get("BVT", ".") == "INDEL":
                region_fp_indel += 1

    region_fp_total = region_fp_snp + region_fp_indel
    fp_per_target = region_fp_total / max(float(synthetic_truth_total), 1.0)
    snp_fp_per_target = region_fp_snp / max(float(synthetic_snp_truth_total), 1.0)
    if fp_per_target > 10.0 and snp_fp_per_target > 6.0:
        overcall_penalty = min(45.0, (fp_per_target - 10.0) * 4.0)
    else:
        overcall_penalty = 0.0

    return {
        "region_fp_snp": float(region_fp_snp),
        "region_fp_indel": float(region_fp_indel),
        "region_fp_total": float(region_fp_total),
        "fp_per_target": fp_per_target,
        "snp_fp_per_target": snp_fp_per_target,
        "overcall_penalty": overcall_penalty,
    }


def parse_happy_outputs(output_prefix: Path, mutations_vcf: Path) -> ParsedHappyMetrics:
    """The whole parse, in the audited upstream order. Raises on unusable output."""
    base = parse_summary_csv(Path(f"{output_prefix}.summary.csv"))
    happy_vcf = Path(f"{output_prefix}.vcf.gz")

    assessed = parse_assessed_only_metrics(happy_vcf) or {}
    merged: dict[str, Any] = dict(base)
    merged.update(assessed)

    mutation_only = compute_mutation_only_metrics(happy_vcf, mutations_vcf)
    merged.update(mutation_only)

    overcall = (
        parse_region_overcall_metrics(
            happy_vcf,
            float(mutation_only["truth_total_snp"]) + float(mutation_only["truth_total_indel"]),
            float(mutation_only["truth_total_snp"]),
        )
        or {}
    )
    merged.update(overcall)

    return ParsedHappyMetrics(
        happy_metrics=merged,
        mutation_only_metrics=mutation_only,
        assessed_only_metrics=assessed,
        overcall=overcall,
    )
