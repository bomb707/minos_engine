"""Comprehensive independent Layer 1 oracle v2 (pysam + direct traversal only).

Extends oracle.py to cover every analytical bam-profile-v1 field: full MAPQ / BQ /
read-length / insert-size distributions (min/max/mean/stddev/quantiles/threshold
fractions), pairing (read1/read2, orientation, cross-contig, singleton, insert),
alignment (CIGAR event AND base counts + NM), both coverage views (duplicate-
including via difference array; fragment_primary via an INDEPENDENT fragment-level
mate-union oracle), reference context, and variant evidence over the EXACT sampled
windows Layer 1 used. Imports NO production Layer 1 calculation module.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pysam

_ALIGNED = frozenset({0, 7, 8})
_REF_ONLY = frozenset({2, 3})
_QUERY_CONSUMING = frozenset({0, 1, 4, 7, 8})
_ACGT = frozenset("ACGT")
PCTS = (1, 5, 10, 25, 50, 75, 90, 95, 99)


class _Hist:
    """Fixed integer histogram with exact mean/stddev/min/max/quantiles."""

    def __init__(self, lo: int, hi: int) -> None:
        self.lo, self.hi = lo, hi
        self.counts = [0] * (hi - lo + 1)
        self.below = self.above = self.total = 0

    def add(self, v: int) -> None:
        self.total += 1
        if v < self.lo:
            self.below += 1
        elif v > self.hi:
            self.above += 1
        else:
            self.counts[v - self.lo] += 1

    def _values_stats(self) -> tuple[float, float, float, float]:
        if self.total == 0:
            return 0.0, 0.0, 0.0, 0.0
        s = self.lo * self.below + self.hi * self.above
        mn = self.lo if self.below else None
        mx = self.hi if self.above else None
        for i, c in enumerate(self.counts):
            if c:
                v = self.lo + i
                s += v * c
                if mn is None:
                    mn = v
                mx = v
        mean = s / self.total
        var = ((self.lo - mean) ** 2) * self.below + ((self.hi - mean) ** 2) * self.above
        for i, c in enumerate(self.counts):
            if c:
                var += ((self.lo + i) - mean) ** 2 * c
        return mean, math.sqrt(var / self.total), float(mn or 0), float(mx or 0)

    def quantile(self, p: float) -> float:
        if self.total == 0:
            return 0.0
        rank = p * (self.total - 1)
        target = math.floor(rank) + 1
        cum = self.below
        if cum >= target:
            return float(self.lo)
        for i, c in enumerate(self.counts):
            cum += c
            if cum >= target:
                return float(self.lo + i)
        return float(self.hi)

    def qmap(self) -> dict[str, float]:
        return {f"P{p:02d}": self.quantile(p / 100.0) for p in PCTS}

    def count_le(self, thr: int) -> int:
        if thr < self.lo:
            return self.below
        upper = min(thr, self.hi)
        return self.below + sum(self.counts[: upper - self.lo + 1])


def _blocks(read: pysam.AlignedSegment) -> list[tuple[int, int]]:
    out = []
    pos = read.reference_start
    for op, length in read.cigartuples or []:
        if op in _ALIGNED:
            out.append((pos, pos + length))
            pos += length
        elif op in _REF_ONLY:
            pos += length
    return out


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals.sort()
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def compute(
    bam: str,
    bai: str,
    ref: str,
    contig: str,
    s0: int,
    e0: int,
    *,
    bq_low: int = 20,
    mq_low: int = 20,
) -> dict[str, Any]:  # noqa: C901 - exhaustive single-pass oracle
    af = pysam.AlignmentFile(bam, "rb", index_filename=bai)
    fa = pysam.FastaFile(ref)
    L = e0 - s0

    observed = mapped = unmapped = duplicate = secondary = supplementary = qcfail = 0
    paired = proper = reverse = mate_unmapped = read1 = read2 = 0
    singleton = cross_contig = 0
    excl = dict.fromkeys(
        ("unmapped", "secondary", "supplementary", "duplicate", "qcfail", "below_mapq"), 0
    )
    included = 0
    mqh = _Hist(0, 255)
    bqh = _Hist(0, 93)
    rlh = _Hist(0, 2000)
    insh = _Hist(0, 5000)
    mq0 = mq_lt = bq_lt = 0
    bases_obs = bases_q = 0
    aligned = soft = hard = ins_b = del_b = skip = qcons = 0
    soft_reads = indel_reads = ins_events = del_events = clip_events = 0
    nm_sum = nm_reads = nm_aligned = 0
    readlens: set[int] = set()
    eligible_paired = improper = completed = overlapping = 0
    fwd = rev = 0  # strand of included reads
    diff_dup = np.zeros(L + 1, dtype=np.int64)
    diff_frag = np.zeros(L + 1, dtype=np.int64)
    mate_end: dict[str, int] = {}
    frag_buf: dict[str, list[tuple[int, int]]] = {}
    since = 0

    for r in af.fetch(contig, s0, e0):
        observed += 1
        if r.is_unmapped:
            unmapped += 1
        else:
            mapped += 1
            if r.is_reverse:
                reverse += 1
        if r.is_duplicate:
            duplicate += 1
        if r.is_secondary:
            secondary += 1
        if r.is_supplementary:
            supplementary += 1
        if r.is_qcfail:
            qcfail += 1
        if r.is_paired:
            paired += 1
            if r.is_read1:
                read1 += 1
            if r.is_read2:
                read2 += 1
            if r.mate_is_unmapped:
                mate_unmapped += 1
                singleton += 1
            elif r.next_reference_id != r.reference_id:
                cross_contig += 1
        if r.is_proper_pair:
            proper += 1

        cov_elig = not (r.is_unmapped or r.is_secondary or r.is_supplementary or r.is_qcfail)
        if cov_elig:
            bl = _blocks(r)
            for a, b in bl:
                cs, ce = max(a, s0) - s0, min(b, e0) - s0
                if ce > cs:
                    diff_dup[cs] += 1
                    diff_dup[ce] -= 1
            if not r.is_duplicate:
                done, ov = _frag_accumulate(r, bl, diff_frag, s0, e0, mate_end, frag_buf)
                if done:
                    completed += 1
                    if ov:
                        overlapping += 1
                since += 1
                if since >= 4096:
                    since = 0
                    floor = r.reference_start - 20000
                    for k in [k for k, v in mate_end.items() if v < floor]:
                        del mate_end[k]
                        frag_buf.pop(k, None)

        reason = _reason(r)
        if reason:
            excl[reason] += 1
            continue
        included += 1
        if r.is_reverse:
            rev += 1
        else:
            fwd += 1
        mq = r.mapping_quality
        mqh.add(mq)
        if mq == 0:
            mq0 += 1
        if mq < mq_low:
            mq_lt += 1
        qlen = r.query_length or 0
        if qlen:
            rlh.add(qlen)
            readlens.add(qlen)
        bases_obs += qlen
        quals = r.query_qualities
        if quals is not None:
            bases_q += len(quals)
            for q in quals:
                bqh.add(q)
                if q < bq_low:
                    bq_lt += 1
        a = s = h = i_ = d_ = n_ = 0
        for op, length in r.cigartuples or []:
            if op in _ALIGNED:
                a += length
            if op in _QUERY_CONSUMING:
                qcons += length
            if op == 4:
                s += length
            elif op == 5:
                h += length
            elif op == 1:
                i_ += length
            elif op == 2:
                d_ += length
            elif op == 3:
                n_ += length
        aligned += a
        soft += s
        hard += h
        ins_b += i_
        del_b += d_
        skip += n_
        if s:
            soft_reads += 1
            clip_events += sum(1 for op, _ in (r.cigartuples or []) if op == 4)
        if i_ or d_:
            indel_reads += 1
        ins_events += sum(1 for op, _ in (r.cigartuples or []) if op == 1)
        del_events += sum(1 for op, _ in (r.cigartuples or []) if op == 2)
        if r.has_tag("NM"):
            nm_sum += int(r.get_tag("NM"))
            nm_reads += 1
            nm_aligned += a
        if r.is_paired:
            eligible_paired += 1
            if not r.is_proper_pair:
                improper += 1
            tlen = int(r.template_length)
            if r.is_proper_pair and tlen > 0:
                insh.add(abs(tlen))
    af.close()

    depth_dup = np.cumsum(diff_dup[:-1])
    depth_frag = np.cumsum(diff_frag[:-1])
    seq = fa.fetch(contig, s0, e0).upper()
    fa.close()

    obs = observed or 1
    inc = included or 1
    v: dict[str, Any] = {}
    # filter + raw
    v["filter_counts.observed"] = observed
    v["filter_counts.included"] = included
    for k in ("unmapped", "secondary", "supplementary", "duplicate", "qcfail", "below_mapq"):
        v[f"filter_counts.excluded_{k}"] = excl[k]
    v["reads.total_observed_alignments"] = observed
    v["reads.included_primary_alignments"] = included
    v["reads.mapped_fraction"] = mapped / obs
    v["reads.unmapped_fraction"] = unmapped / obs
    v["reads.duplicate_fraction"] = duplicate / obs
    v["reads.secondary_fraction"] = secondary / obs
    v["reads.supplementary_fraction"] = supplementary / obs
    v["reads.qcfail_fraction"] = qcfail / obs
    v["reads.paired_fraction"] = paired / obs
    v["reads.proper_pair_fraction"] = proper / (paired or 1)
    v["reads.mate_unmapped_fraction"] = mate_unmapped / (paired or 1)
    v["reads.reverse_strand_fraction"] = reverse / (mapped or 1)
    # MAPQ
    mean, sd, mn, mx = mqh._values_stats()
    v["mapping_quality.count"] = included
    v["mapping_quality.mean"] = mean
    v["mapping_quality.mean_mapping_quality_phred"] = mean
    v["mapping_quality.stddev"] = sd
    v["mapping_quality.minimum"] = mn
    v["mapping_quality.maximum"] = mx
    v["mapping_quality.mq0_fraction"] = mq0 / inc
    v["mapping_quality.mq_lt20_fraction"] = mq_lt / inc
    for k, val in mqh.qmap().items():
        v[f"mapping_quality.quantiles.{k}"] = val
    # BQ
    bmean, bsd, _, _ = bqh._values_stats()
    v["base_quality.bases_observed"] = bases_obs
    v["base_quality.bases_with_quality"] = bases_q
    v["base_quality.mean_base_quality_phred"] = bmean
    v["base_quality.stddev_base_quality_phred"] = bsd
    v["base_quality.bq_lt20_fraction"] = bq_lt / (bqh.total or 1)
    v["base_quality.missing_quality_fraction"] = max(0.0, 1 - bases_q / (bases_obs or 1))
    for k, val in bqh.qmap().items():
        v[f"base_quality.quantiles_phred.{k}"] = val
    # read length
    rmean, rsd, rmn, rmx = rlh._values_stats()
    v["read_length.count"] = included
    v["read_length.mean"] = rmean
    v["read_length.stddev"] = rsd
    v["read_length.minimum"] = rmn
    v["read_length.maximum"] = rmx
    v["read_length.variable_read_length"] = len(readlens) > 1
    for k, val in rlh.qmap().items():
        v[f"read_length.quantiles.{k}"] = val
    # pairing / insert
    imean, isd, _, _ = insh._values_stats()
    v["pairing.eligible_pair_count"] = completed
    v["pairing.mean_insert_size_bp"] = imean
    v["pairing.stddev_insert_size_bp"] = isd
    v["pairing.insert_size_mad_bp"] = _hist_mad(insh)
    v["pairing.overlapping_mate_fraction"] = overlapping / (completed or 1)
    v["pairing.abnormal_pair_fraction"] = improper / (eligible_paired or 1)
    for k, val in insh.qmap().items():
        v[f"pairing.quantiles_bp.{k}"] = val
    v["_aux.read1"] = read1
    v["_aux.read2"] = read2
    v["_aux.cross_contig"] = cross_contig
    v["_aux.singleton"] = singleton
    v["_aux.overlapping_pairs"] = overlapping  # note: computed inside frag accumulate below
    # alignment / cigar
    v["alignment.aligned_query_bases"] = aligned
    v["alignment.soft_clipped_bases"] = soft
    v["alignment.hard_clipped_bases"] = hard
    v["alignment.inserted_bases"] = ins_b
    v["alignment.deleted_bases"] = del_b
    v["alignment.skipped_bases"] = skip
    v["alignment.query_consuming_bases"] = qcons
    v["alignment.soft_clipped_read_fraction"] = soft_reads / inc
    v["alignment.soft_clipped_base_fraction"] = soft / (qcons or 1)
    v["alignment.indel_bearing_read_fraction"] = indel_reads / inc
    v["alignment.nm_per_aligned_base"] = nm_sum / (nm_aligned or 1)
    v["alignment.nm_availability_fraction"] = nm_reads / inc
    v["alignment.cigar_ins_del_burden"] = (ins_b + del_b) / (aligned or 1)
    v["_aux.ins_events"] = ins_events
    v["_aux.del_events"] = del_events
    v["_aux.clip_events"] = clip_events
    # coverage views
    v.update(_cov_view("coverage.duplicate_including", depth_dup, L))
    v.update(_cov_view("coverage.fragment_primary", depth_frag, L))
    v["coverage.eligible_region_bases"] = L
    v["region.start0"] = s0
    v["region.end0_exclusive"] = e0
    v["region.length_bp"] = L
    v["_aux.covered_bases_dup"] = int((depth_dup > 0).sum())
    v["_aux.uncovered_bases_dup"] = int((depth_dup == 0).sum())
    # reference context
    v.update(_refctx(seq))
    return v


def _reason(r: pysam.AlignedSegment) -> str | None:
    if r.is_unmapped:
        return "unmapped"
    if r.is_secondary:
        return "secondary"
    if r.is_supplementary:
        return "supplementary"
    if r.is_duplicate:
        return "duplicate"
    if r.is_qcfail:
        return "qcfail"
    return None


def _frag_accumulate(r, blocks, diff, s0, e0, mate_end, frag_buf):  # type: ignore[no-untyped-def]
    """Independent fragment-level oracle: union a proper pair's mate blocks once.

    Returns (completed_pair, overlapped) for pairing diagnostics.
    """
    if r.is_paired and r.is_proper_pair and r.reference_id == r.next_reference_id:
        name = r.query_name
        if name in frag_buf:
            first = frag_buf.pop(name)
            mate_end.pop(name, None)
            # overlap iff the union has fewer bases than the sum (blocks intersect)
            sum_bases = sum(b - a for a, b in first) + sum(b - a for a, b in blocks)
            union = _merge(first + list(blocks))
            union_bases = sum(b - a for a, b in union)
            overlapped = union_bases < sum_bases
            for a, b in union:
                cs, ce = max(a, s0) - s0, min(b, e0) - s0
                if ce > cs:
                    diff[cs] += 1
                    diff[ce] -= 1
            return True, overlapped
        frag_buf[name] = list(blocks)
        mate_end[name] = r.reference_end
        return False, False
    for a, b in blocks:
        cs, ce = max(a, s0) - s0, min(b, e0) - s0
        if ce > cs:
            diff[cs] += 1
            diff[ce] -= 1
    return False, False


def _cov_view(prefix: str, depth: np.ndarray, L: int) -> dict[str, Any]:
    n = int(depth.size) or 1
    d = depth.astype(np.float64)
    mean = float(d.mean()) if depth.size else 0.0
    std = float(d.std()) if depth.size else 0.0
    med = float(np.median(d)) if depth.size else 0.0
    mad = float(np.median(np.abs(d - med))) if depth.size else 0.0
    cv = std / mean if mean > 0 else 0.0
    q = {f"P{p:02d}": float(np.quantile(d, p / 100.0, method="linear")) for p in PCTS}

    def fr(mask: np.ndarray) -> float:
        return float(mask.sum()) / n

    out = {
        f"{prefix}.mean_depth_reads_per_base": mean,
        f"{prefix}.median_depth_reads_per_base": med,
        f"{prefix}.stddev_depth": std,
        f"{prefix}.coefficient_of_variation": cv,
        f"{prefix}.depth_mad": mad,
        f"{prefix}.max_depth": int(depth.max()) if depth.size else 0,
        f"{prefix}.zero_depth_fraction": fr(depth == 0),
        f"{prefix}.depth_lt5_fraction": fr(depth < 5),
        f"{prefix}.depth_lt10_fraction": fr(depth < 10),
        f"{prefix}.depth_lt20_fraction": fr(depth < 20),
        f"{prefix}.depth_gt50_fraction": fr(depth > 50),
        f"{prefix}.depth_gt100_fraction": fr(depth > 100),
        f"{prefix}.depth_gt200_fraction": fr(depth > 200),
        f"{prefix}.callable_base_fraction": fr(depth >= 10),
    }
    for k, val in q.items():
        out[f"{prefix}.depth_quantiles.{k}"] = val
    return out


def _refctx(seq: str) -> dict[str, Any]:
    counts = dict.fromkeys("ACGT", 0)
    n = 0
    for ch in seq:
        if ch in counts:
            counts[ch] += 1
        else:
            n += 1
    acgt = sum(counts.values())
    gc = (counts["G"] + counts["C"]) / acgt if acgt else 0.0
    ent = 0.0
    for b in "ACGT":
        if acgt and counts[b]:
            p = counts[b] / acgt
            ent -= p * math.log2(p)
    homo = 0
    homo_hist: dict[int, int] = {}
    i = 0
    while i < len(seq):
        j = i + 1
        while j < len(seq) and seq[j] == seq[i]:
            j += 1
        run = j - i
        if seq[i] in _ACGT and run >= 4:
            homo += run
            homo_hist[run] = homo_hist.get(run, 0) + 1
        i = j
    dinuc = _dinuc(seq)
    ln = len(seq) or 1
    return {
        "reference_context.gc_fraction": gc,
        "reference_context.n_fraction": n / ln,
        "reference_context.entropy_bits": ent,
        "reference_context.homopolymer_base_fraction": homo / ln,
        "reference_context.dinucleotide_repeat_fraction": dinuc / ln,
        "_aux.homopolymer_length_histogram": {str(k): v for k, v in sorted(homo_hist.items())},
    }


def _dinuc(s: str, min_units: int = 3) -> int:
    n = len(s)
    covered = 0
    i = 0
    while i + 1 < n:
        a, b = s[i], s[i + 1]
        if a not in _ACGT or b not in _ACGT or a == b:
            i += 1
            continue
        units = 1
        j = i + 2
        while j + 1 < n and s[j] == a and s[j + 1] == b:
            units += 1
            j += 2
        if units >= min_units:
            covered += units * 2
            i = j
        else:
            i += 1
    return covered


def variant_evidence_over_windows(
    bam: str,
    bai: str,
    ref: str,
    contig: str,
    windows: list[tuple[int, int]],
    *,
    max_depth: int = 20000,
    support=(2, 3, 5, 8, 10),
    afs=(0.05, 0.10, 0.20, 0.30, 0.40),
    low_bq: int = 20,
    region_len: int = 0,
) -> dict[str, Any]:
    """Independent variant_evidence aggregate over the EXACT sampled windows (spec criteria)."""
    af = pysam.AlignmentFile(bam, "rb", index_filename=bai)
    fa = pysam.FastaFile(ref)
    total_base = alt_base = snp = ins = dele = fwd = rev = low = capped = columns = (
        callable_cols
    ) = 0
    support_counts = dict.fromkeys(support, 0)
    af_counts = dict.fromkeys(afs, 0)
    analyzed = 0
    for w0, w1 in windows:
        analyzed += w1 - w0
        rseq = fa.fetch(contig, w0, w1).upper()
        for col in af.pileup(
            contig,
            w0,
            w1,
            truncate=True,
            min_base_quality=0,
            min_mapping_quality=0,
            ignore_overlaps=True,
            compute_baq=False,
            max_depth=max_depth,
            stepper="samtools",
        ):
            pos = col.reference_pos
            if pos < w0 or pos >= w1:
                continue
            columns += 1
            rb = rseq[pos - w0] if 0 <= pos - w0 < len(rseq) else "N"
            depth = alt = f = r = lq = ins_s = del_s = 0
            altbase: dict[str, int] = {}
            for pr in col.pileups:
                if pr.indel > 0:
                    ins_s += 1
                elif pr.indel < 0:
                    del_s += 1
                if pr.is_del or pr.is_refskip or pr.query_position is None:
                    continue
                b = pr.alignment.query_sequence[pr.query_position].upper()
                depth += 1
                if b != rb and b in _ACGT and rb in _ACGT:
                    alt += 1
                    altbase[b] = altbase.get(b, 0) + 1
                    if pr.alignment.is_reverse:
                        r += 1
                    else:
                        f += 1
                    q = pr.alignment.query_qualities
                    if q is not None and q[pr.query_position] < low_bq:
                        lq += 1
            if depth >= max_depth:
                capped += 1
            total_base += depth
            alt_base += alt
            fwd += f
            rev += r
            low += lq
            if rb in _ACGT:
                callable_cols += 1
                top = max(altbase.values()) if altbase else 0
                frac = top / depth if depth else 0.0
                for t in support:
                    if top >= t:
                        support_counts[t] += 1
                for t in afs:
                    if frac >= t:
                        af_counts[t] += 1
                if top >= support[0] and frac >= afs[0]:
                    snp += 1
            if ins_s >= support[0]:
                ins += 1
            if del_s >= support[0]:
                dele += 1
    af.close()
    fa.close()
    analyzed = analyzed or 1
    alt_denom = (fwd + rev) or 1
    out: dict[str, Any] = {
        "variant_evidence.analyzed_callable_bases": callable_cols,
        "variant_evidence.eligible_region_bases": region_len,
        "variant_evidence.mismatch_fraction": alt_base / (total_base or 1),
        "variant_evidence.candidate_snp_density_per_base": snp / analyzed,
        "variant_evidence.candidate_insertion_density_per_base": ins / analyzed,
        "variant_evidence.candidate_deletion_density_per_base": dele / analyzed,
        "variant_evidence.forward_alt_fraction": fwd / alt_denom,
        "variant_evidence.reverse_alt_fraction": rev / alt_denom,
        "variant_evidence.low_quality_alt_fraction": low / (alt_base or 1),
        "variant_evidence.columns_reaching_max_depth": capped,
        "variant_evidence.max_depth_capped_fraction": capped / (columns or 1),
    }
    for t, c in support_counts.items():
        out[f"variant_evidence.support_threshold_site_counts.support_ge_{t}"] = c
    for t, c in af_counts.items():
        out[
            f"variant_evidence.allele_fraction_threshold_site_counts.af_ge_{int(round(t * 100)):02d}"
        ] = c
    return out


def _hist_mad(h: _Hist) -> float:
    """Median absolute deviation from an integer histogram (independent)."""
    if h.total == 0:
        return 0.0
    med = h.quantile(0.5)
    dev = _Hist(0, h.hi - h.lo)
    for i, c in enumerate(h.counts):
        if c:
            dev_val = int(abs((h.lo + i) - med))
            for _ in range(c):
                dev.add(dev_val)
    if h.below:
        for _ in range(h.below):
            dev.add(int(abs(h.lo - med)))
    if h.above:
        for _ in range(h.above):
            dev.add(int(abs(h.hi - med)))
    return dev.quantile(0.5)
