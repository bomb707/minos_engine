"""Layer 1 EMITTED-FEATURE truth relevance (window-level) — consumes actual output.

Unlike the BAM-intrinsic observability oracle (direct pileup), this reads the
serialized Layer 1 window-profile-v1 output and tests whether Layer 1's *emitted*
window features (candidate SNP/indel densities, depth, MQ, NM, clipping, entropy)
explain offline per-window truth labels. Truth is joined only here, offline; it is
never passed to Layer 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pyarrow.parquet as pq
import pysam


@dataclass
class EmittedRelevance:
    n_windows: int
    n_sampled: int
    spearman_snp: float
    spearman_indel: float
    auroc_snp_window: float
    auprc_snp_window: float
    top_decile_lift_snp: float
    windows_with_truth: int
    per_window: list[dict[str, Any]] = field(default_factory=list)


def _spearman(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n and v[order[j]] == v[order[i]]:
                j += 1
            avg = (i + j - 1) / 2.0 + 1
            for k in range(i, j):
                r[order[k]] = avg
            i = j
        return r

    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def _auroc(scores: list[float], labels: list[int]) -> tuple[float, float]:
    pos = [s for s, y in zip(scores, labels, strict=True) if y == 1]
    neg = [s for s, y in zip(scores, labels, strict=True) if y == 0]
    if not pos or not neg:
        return float("nan"), float("nan")
    lab = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    n = len(lab)
    i = 0
    rs = 0.0
    while i < n:
        j = i
        while j < n and lab[j][0] == lab[i][0]:
            j += 1
        avg = (i + 1 + j) / 2.0
        rs += sum(avg for k in range(i, j) if lab[k][1] == 1)
        i = j
    auroc = (rs - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))
    alls = sorted(set(scores), reverse=True)
    ap = 0.0
    prev = 0.0
    for thr in alls:
        tp = sum(1 for s in pos if s >= thr)
        fp = sum(1 for s in neg if s >= thr)
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        rec = tp / len(pos)
        ap += (rec - prev) * prec
        prev = rec
    return auroc, ap


def window_truth_labels(
    truth_vcf: str, contig: str, windows: list[tuple[int, int]]
) -> dict[int, dict[str, int]]:
    vf = pysam.VariantFile(truth_vcf)
    labels = {i: {"snp": 0, "ins": 0, "del": 0, "total": 0} for i in range(len(windows))}
    for i, (w0, w1) in enumerate(windows):
        for rec in vf.fetch(contig, w0, w1):
            p0 = rec.pos - 1
            if p0 < w0 or p0 >= w1:
                continue
            alt = rec.alts[0] if rec.alts else ""
            if len(rec.ref) == 1 and len(alt) == 1:
                labels[i]["snp"] += 1
            elif len(alt) > len(rec.ref):
                labels[i]["ins"] += 1
            elif len(alt) < len(rec.ref):
                labels[i]["del"] += 1
            labels[i]["total"] += 1
    vf.close()
    return labels


def evaluate(windows_parquet: str, truth_vcf: str, contig: str) -> EmittedRelevance:
    tbl = pq.read_table(windows_parquet).to_pylist()
    windows = [(r["start0"], r["end0"]) for r in tbl]
    labels = window_truth_labels(truth_vcf, contig, windows)
    # emitted-feature evaluation over SAMPLED windows (where evidence features are populated)
    sampled_idx = [i for i, r in enumerate(tbl) if r["sampled"]]
    snp_density = [tbl[i]["candidate_snp_density_per_base"] for i in sampled_idx]
    indel_density = [tbl[i]["candidate_indel_density_per_base"] for i in sampled_idx]
    truth_snp_pb = [labels[i]["snp"] / max(1, tbl[i]["length_bp"]) for i in sampled_idx]
    truth_indel_pb = [
        (labels[i]["ins"] + labels[i]["del"]) / max(1, tbl[i]["length_bp"]) for i in sampled_idx
    ]
    snp_label = [1 if labels[i]["snp"] > 0 else 0 for i in sampled_idx]
    auroc, auprc = _auroc(snp_density, snp_label)
    # top-decile lift: mean truth-snp density in top-10% emitted-density windows vs overall
    lift = 0.0
    if len(sampled_idx) >= 10:
        order = sorted(range(len(sampled_idx)), key=lambda k: snp_density[k], reverse=True)
        top = order[: max(1, len(order) // 10)]
        top_mean = sum(truth_snp_pb[k] for k in top) / len(top)
        overall = sum(truth_snp_pb) / len(truth_snp_pb) if truth_snp_pb else 0.0
        lift = top_mean / overall if overall > 0 else 0.0
    per_window = [
        {
            "window_id": tbl[i]["window_id"],
            "sampled": tbl[i]["sampled"],
            "candidate_snp_density": tbl[i]["candidate_snp_density_per_base"],
            "candidate_indel_density": tbl[i]["candidate_indel_density_per_base"],
            "truth_snp": labels[i]["snp"],
            "truth_indel": labels[i]["ins"] + labels[i]["del"],
            "length_bp": tbl[i]["length_bp"],
            "depth_mean": tbl[i]["depth_mean_reads_per_base"],
        }
        for i in sampled_idx
    ]
    return EmittedRelevance(
        n_windows=len(tbl),
        n_sampled=len(sampled_idx),
        spearman_snp=_spearman(snp_density, truth_snp_pb),
        spearman_indel=_spearman(indel_density, truth_indel_pb),
        auroc_snp_window=auroc,
        auprc_snp_window=auprc,
        top_decile_lift_snp=lift,
        windows_with_truth=sum(1 for i in range(len(tbl)) if labels[i]["total"] > 0),
        per_window=per_window,
    )
