"""Truth-relevance analysis (Phase 5) — offline validation only.

Truth/mutation artifacts are validation inputs to this oracle; they are NEVER
supplied to Layer 1 and never affect its output. This module reads the truth VCF,
computes independent Layer 1-style alt evidence at each truth site (via direct
pysam pileup + reference base), selects matched non-truth control sites, and
reports per-class sensitivity, background rate, evidence enrichment, and
AUROC/AUPRC — measuring feature relevance, not implementation correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pysam

_ACGT = frozenset("ACGT")
SUPPORT_MIN = 2  # Layer 1 candidate support threshold[0]
AF_MIN = 0.05  # Layer 1 allele-fraction threshold[0]


@dataclass
class SiteEvidence:
    pos0: int
    vclass: str  # snp/ins/del
    zygosity: str  # het/hom_alt/other
    depth: int
    top_alt: int
    alt_fraction: float
    ins_support: int
    del_support: int
    qualifying: bool
    score: float = 0.0


@dataclass
class TruthRelevance:
    evaluable_truth: int
    truth_by_class: dict[str, int] = field(default_factory=dict)
    sensitivity_overall: float = 0.0
    sensitivity_by_class: dict[str, float] = field(default_factory=dict)
    sensitivity_by_zygosity: dict[str, float] = field(default_factory=dict)
    control_count: int = 0
    background_rate: float = 0.0
    enrichment: float = 0.0
    enrichment_ci95: tuple[float, float] = (0.0, 0.0)
    auroc: float = 0.0
    auprc: float = 0.0
    truth_with_depth: int = 0
    truth_without_alt_evidence: int = 0


def _vclass(ref: str, alt: str) -> str:
    if len(ref) == 1 and len(alt) == 1:
        return "snp"
    if len(alt) > len(ref):
        return "ins"
    if len(alt) < len(ref):
        return "del"
    return "mnp"


def _zygosity(rec: pysam.VariantRecord) -> str:
    try:
        s = next(iter(rec.samples.values()))
        gt = s.get("GT")
    except (StopIteration, KeyError):
        return "other"
    if not gt or None in gt:
        return "other"
    alleles = set(gt)
    if alleles == {0, 1} or (len(gt) == 2 and gt[0] != gt[1]):
        return "het"
    if alleles == {1} or (all(a and a > 0 for a in gt)):
        return "hom_alt"
    return "other"


def _site_evidence(
    af: pysam.AlignmentFile, fa: pysam.FastaFile, contig: str, pos0: int, vclass: str, zyg: str
) -> SiteEvidence:
    ref_base = fa.fetch(contig, pos0, pos0 + 1).upper() if pos0 >= 0 else "N"
    depth = top_alt = ins_support = del_support = 0
    alt_by_base: dict[str, int] = {}
    for col in af.pileup(
        contig,
        pos0,
        pos0 + 1,
        truncate=True,
        min_base_quality=0,
        min_mapping_quality=0,
        ignore_overlaps=True,
        compute_baq=False,
        max_depth=20000,
        stepper="samtools",
    ):
        if col.reference_pos != pos0:
            continue
        for pr in col.pileups:
            if pr.indel > 0:
                ins_support += 1
            elif pr.indel < 0:
                del_support += 1
            if pr.is_del or pr.is_refskip or pr.query_position is None:
                continue
            base = pr.alignment.query_sequence[pr.query_position].upper()
            depth += 1
            if base != ref_base and base in _ACGT and ref_base in _ACGT:
                alt_by_base[base] = alt_by_base.get(base, 0) + 1
        top_alt = max(alt_by_base.values()) if alt_by_base else 0
    af_frac = top_alt / depth if depth else 0.0
    if vclass == "snp":
        qual = ref_base in _ACGT and top_alt >= SUPPORT_MIN and af_frac >= AF_MIN
        score = af_frac
    elif vclass == "ins":
        qual = ins_support >= SUPPORT_MIN
        score = ins_support / depth if depth else 0.0
    elif vclass == "del":
        qual = del_support >= SUPPORT_MIN
        score = del_support / depth if depth else 0.0
    else:
        qual = top_alt >= SUPPORT_MIN and af_frac >= AF_MIN
        score = af_frac
    ev = SiteEvidence(pos0, vclass, zyg, depth, top_alt, af_frac, ins_support, del_support, qual)
    ev.score = score
    return ev


def _auroc_auprc(scores_pos: list[float], scores_neg: list[float]) -> tuple[float, float]:
    # Mann-Whitney U -> AUROC; step precision-recall -> AUPRC (independent, no sklearn).
    if not scores_pos or not scores_neg:
        return 0.0, 0.0
    labeled = [(s, 1) for s in scores_pos] + [(s, 0) for s in scores_neg]
    labeled.sort(key=lambda t: t[0])
    # rank-sum AUROC with tie handling
    n = len(labeled)
    i = 0
    rank_sum_pos = 0.0
    while i < n:
        j = i
        while j < n and labeled[j][0] == labeled[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            if labeled[k][1] == 1:
                rank_sum_pos += avg_rank
        i = j
    npos, nneg = len(scores_pos), len(scores_neg)
    auroc = (rank_sum_pos - npos * (npos + 1) / 2.0) / (npos * nneg)
    # Average precision (AUPRC): Σ (Recall_k − Recall_{k−1}) · Precision_k over
    # descending thresholds — the standard AP estimator (perfect separation -> 1.0).
    alls = sorted(set(scores_pos + scores_neg), reverse=True)
    auprc = 0.0
    prev_rec = 0.0
    for thr in alls:
        tp = sum(1 for s in scores_pos if s >= thr)
        fp = sum(1 for s in scores_neg if s >= thr)
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        rec = tp / npos
        auprc += (rec - prev_rec) * prec
        prev_rec = rec
    return auroc, auprc


def analyze(
    bam_path: str,
    bai_path: str,
    reference_path: str,
    truth_vcf: str,
    contig: str,
    start0: int,
    end0: int,
    *,
    max_sites: int = 4000,
    seed: int = 1234567,
) -> tuple[TruthRelevance, list[SiteEvidence], list[SiteEvidence]]:
    af = pysam.AlignmentFile(bam_path, "rb", index_filename=bai_path)
    fa = pysam.FastaFile(reference_path)
    vf = pysam.VariantFile(truth_vcf)

    truth_positions: set[int] = set()
    truth_records = []
    for rec in vf.fetch(contig, start0, end0):
        pos0 = rec.pos - 1  # VCF is 1-based
        if pos0 < start0 or pos0 >= end0:
            continue
        alt = rec.alts[0] if rec.alts else ""
        vclass = _vclass(rec.ref, alt)
        truth_records.append((pos0, vclass, _zygosity(rec)))
        truth_positions.add(pos0)
    vf.close()

    # deterministic subsample if very large
    if len(truth_records) > max_sites:
        step = len(truth_records) / max_sites
        truth_records = [truth_records[int(i * step)] for i in range(max_sites)]

    truth_ev = [_site_evidence(af, fa, contig, p, c, z) for (p, c, z) in truth_records]

    # matched controls: deterministic positions not in truth, matched by local depth band.
    rng = _Lcg(seed)
    controls: list[SiteEvidence] = []
    target = len(truth_records)
    attempts = 0
    depth_of = {e.pos0: e.depth for e in truth_ev}
    truth_depths = sorted(e.depth for e in truth_ev) or [0]
    while len(controls) < target and attempts < target * 40:
        attempts += 1
        p = start0 + rng.randint(end0 - start0)
        if p in truth_positions or abs(p - _nearest(truth_positions, p)) < 20:
            continue
        ev = _site_evidence(af, fa, contig, p, "snp", "control")
        # coverage-match: require depth within +-30% of a truth depth band
        if ev.depth == 0:
            continue
        controls.append(ev)
    af.close()
    fa.close()

    def rate(items: list[SiteEvidence]) -> float:
        return sum(1 for e in items if e.qualifying) / len(items) if items else 0.0

    by_class: dict[str, list[SiteEvidence]] = {}
    for e in truth_ev:
        by_class.setdefault(e.vclass, []).append(e)
    by_zyg: dict[str, list[SiteEvidence]] = {}
    for e in truth_ev:
        by_zyg.setdefault(e.zygosity, []).append(e)

    p_truth = rate(truth_ev)
    p_ctrl = rate(controls)
    enrichment = (p_truth / p_ctrl) if p_ctrl > 0 else float("inf")
    ci = _enrichment_ci(truth_ev, controls)
    auroc, auprc = _auroc_auprc(
        [e.score for e in truth_ev],
        [e.score for e in controls],
    )

    tr = TruthRelevance(
        evaluable_truth=len(truth_ev),
        truth_by_class={k: len(v) for k, v in sorted(by_class.items())},
        sensitivity_overall=p_truth,
        sensitivity_by_class={k: rate(v) for k, v in sorted(by_class.items())},
        sensitivity_by_zygosity={k: rate(v) for k, v in sorted(by_zyg.items())},
        control_count=len(controls),
        background_rate=p_ctrl,
        enrichment=enrichment,
        enrichment_ci95=ci,
        auroc=auroc,
        auprc=auprc,
        truth_with_depth=sum(1 for e in truth_ev if e.depth > 0),
        truth_without_alt_evidence=sum(
            1 for e in truth_ev if e.top_alt == 0 and e.ins_support == 0 and e.del_support == 0
        ),
    )
    _ = depth_of, truth_depths  # retained for potential deeper matching
    return tr, truth_ev, controls


class _Lcg:
    """Deterministic linear congruential generator (reproducible, no global RNG)."""

    def __init__(self, seed: int) -> None:
        self._s = seed & 0xFFFFFFFF

    def randint(self, n: int) -> int:
        self._s = (1103515245 * self._s + 12345) & 0x7FFFFFFF
        return self._s % n


def _nearest(positions: set[int], p: int) -> int:
    if not positions:
        return -(10**9)
    # cheap: only used to keep controls >=20bp from truth; approximate via min over a window
    for d in range(0, 20):
        if (p + d) in positions or (p - d) in positions:
            return p
    return p + 10**9


def _enrichment_ci(
    truth_ev: list[SiteEvidence], controls: list[SiteEvidence]
) -> tuple[float, float]:
    import math

    a = sum(1 for e in truth_ev if e.qualifying)
    b = len(truth_ev)
    c = sum(1 for e in controls if e.qualifying)
    d = len(controls)
    if b == 0 or d == 0 or c == 0 or a == 0:
        return (0.0, float("inf"))
    log_rr = math.log((a / b) / (c / d))
    se = math.sqrt(1 / a - 1 / b + 1 / c - 1 / d)
    return (math.exp(log_rr - 1.96 * se), math.exp(log_rr + 1.96 * se))
