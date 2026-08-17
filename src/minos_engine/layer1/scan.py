"""One-pass, bounded-memory alignment scan (Layer 1 spec §8–§10).

A single ``bam.fetch(contig, start0, end0)`` pass feeds every read-derived family:
raw flag counts, the analysis-eligible filter accounting, mapping/base-quality and
read-length distributions, CIGAR/clipping/NM sums, fragment/insert-size and
overlapping-mate statistics, per-window accumulators, and the two coverage views.
Nothing retains all reads — only integer counters, Welford accumulators, fixed
histograms, per-window sums, and difference arrays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .aggregators import IntHistogram, Welford, percentile_keys
from .contracts import (
    AlignmentMetrics,
    BaseQualityMetrics,
    CigarMetrics,
    FilterCounts,
    FragmentMetrics,
    MappingQualityMetrics,
    ReadLengthMetrics,
)
from .coverage import DUP_INCLUDING, FRAGMENT_PRIMARY, CoverageAccumulator
from .filters import ReadFilterPolicy

__all__ = ["WindowAccumulator", "OnePassScanner"]

# CIGAR op codes (pysam): 0=M 1=I 2=D 3=N 4=S 5=H 6=P 7=(=) 8=X
_QUERY_CONSUMING = frozenset({0, 1, 4, 7, 8})
_ALIGNED = frozenset({0, 7, 8})
_MATE_EVICT_MARGIN = 20000


@dataclass
class WindowAccumulator:
    read_count: int = 0
    mapped_primary: int = 0
    duplicate: int = 0
    raw_reads: int = 0  # coverage-eligible reads (dups included) starting in window
    raw_dup: int = 0
    softclip_reads: int = 0
    mq_sum: float = 0.0
    mq_count: int = 0
    bq_sum: float = 0.0
    bq_count: int = 0
    nm_sum: int = 0
    nm_aligned_bases: int = 0
    nm_reads: int = 0
    indel_bases: int = 0
    aligned_bases: int = 0


@dataclass
class OnePassScanner:
    region_start0: int
    region_end0: int
    primary_bp: int
    policy: ReadFilterPolicy
    max_read_len: int = 1000
    max_insert: int = 2000
    max_mapq: int = 255
    max_bq: int = 93

    # raw flag counters (all observed)
    observed: int = 0
    mapped: int = 0
    unmapped: int = 0
    duplicate: int = 0
    secondary: int = 0
    supplementary: int = 0
    qcfail: int = 0
    paired: int = 0
    proper_pair: int = 0
    reverse: int = 0
    mate_unmapped: int = 0

    # filter accounting
    included: int = 0
    excl_unmapped: int = 0
    excl_secondary: int = 0
    excl_supplementary: int = 0
    excl_duplicate: int = 0
    excl_qcfail: int = 0
    excl_below_mapq: int = 0

    # cigar/nm sums over eligible reads
    aligned_query_bases: int = 0
    soft_clipped_bases: int = 0
    hard_clipped_bases: int = 0
    inserted_bases: int = 0
    deleted_bases: int = 0
    skipped_bases: int = 0
    query_consuming_bases: int = 0
    soft_clipped_reads: int = 0
    indel_reads: int = 0
    nm_sum: int = 0
    nm_reads: int = 0
    nm_aligned_bases: int = 0

    # base quality
    bases_observed: int = 0
    bases_with_quality: int = 0

    # header-observed tag presence
    saw_nm: bool = False
    saw_md: bool = False

    # pairing/overlap
    eligible_paired: int = 0
    eligible_paired_improper: int = 0
    completed_pairs: int = 0
    overlapping_pairs: int = 0

    def __post_init__(self) -> None:
        self._mq = Welford()
        self._mq_hist = IntHistogram(0, self.max_mapq)
        self._bq_hist = IntHistogram(0, self.max_bq)
        self._rl = Welford()
        self._rl_hist = IntHistogram(0, self.max_read_len)
        self._tlen_hist = IntHistogram(0, self.max_insert)
        self._readlen_seen: set[int] = set()
        n_windows = max(1, -(-(self.region_end0 - self.region_start0) // self.primary_bp))
        self._windows = [WindowAccumulator() for _ in range(n_windows)]
        self._cov = CoverageAccumulator(self.region_start0, self.region_end0 - self.region_start0)
        self._mate_first_end: dict[str, int] = {}
        self._since_evict = 0

    # -- window index -------------------------------------------------------- #
    def _window_index(self, ref_start: int) -> int:
        idx = (ref_start - self.region_start0) // self.primary_bp
        if idx < 0:
            return 0
        if idx >= len(self._windows):
            return len(self._windows) - 1
        return idx

    @property
    def coverage(self) -> CoverageAccumulator:
        return self._cov

    @property
    def windows(self) -> list[WindowAccumulator]:
        return self._windows

    # -- main entry ---------------------------------------------------------- #
    def observe(self, read: Any) -> None:
        self.observed += 1
        if read.is_unmapped:
            self.unmapped += 1
        else:
            self.mapped += 1
        if read.is_duplicate:
            self.duplicate += 1
        if read.is_secondary:
            self.secondary += 1
        if read.is_supplementary:
            self.supplementary += 1
        if read.is_qcfail:
            self.qcfail += 1
        if read.is_paired:
            self.paired += 1
            if read.mate_is_unmapped:
                self.mate_unmapped += 1
        if read.is_proper_pair:
            self.proper_pair += 1
        if (not read.is_unmapped) and read.is_reverse:
            self.reverse += 1

        reason = self.policy.classify(read)
        # coverage-eligible = mapped primary non-secondary/supp/qcfail (dup allowed)
        cov_eligible = not (
            read.is_unmapped or read.is_secondary or read.is_supplementary or read.is_qcfail
        )
        if cov_eligible:
            rw = self._windows[self._window_index(read.reference_start)]
            rw.raw_reads += 1
            if read.is_duplicate:
                rw.raw_dup += 1
            self._accumulate_coverage(read)

        if reason is not None:
            self._count_exclusion(reason)
            return

        self.included += 1
        self._observe_eligible(read)

    def _count_exclusion(self, reason: str) -> None:
        attr = {
            "unmapped": "excl_unmapped",
            "secondary": "excl_secondary",
            "supplementary": "excl_supplementary",
            "duplicate": "excl_duplicate",
            "qcfail": "excl_qcfail",
            "below_mapq": "excl_below_mapq",
        }[reason]
        setattr(self, attr, getattr(self, attr) + 1)

    # -- coverage + mate overlap -------------------------------------------- #
    def _accumulate_coverage(self, read: Any) -> None:
        blocks = read.get_blocks()  # aligned ref intervals, splits at D/N, excludes I/S
        for s, e in blocks:
            self._cov.add(DUP_INCLUDING, s, e)
        if read.is_duplicate:
            return
        clip_floor: int | None = None
        if read.is_paired and read.is_proper_pair and read.reference_id == read.next_reference_id:
            name = read.query_name
            prior = self._mate_first_end.get(name)
            if prior is None:
                self._mate_first_end[name] = read.reference_end
            else:
                del self._mate_first_end[name]
                self.completed_pairs += 1
                clip_floor = prior
                if read.reference_start < prior:
                    self.overlapping_pairs += 1
            self._maybe_evict(read.reference_start)
        for s, e in blocks:
            cs = max(s, clip_floor) if clip_floor is not None else s
            if cs < e:
                self._cov.add(FRAGMENT_PRIMARY, cs, e)

    def _maybe_evict(self, ref_start: int) -> None:
        self._since_evict += 1
        if self._since_evict < 4096:
            return
        self._since_evict = 0
        floor = ref_start - _MATE_EVICT_MARGIN
        stale = [k for k, v in self._mate_first_end.items() if v < floor]
        for k in stale:
            del self._mate_first_end[k]

    # -- eligible-read families --------------------------------------------- #
    def _observe_eligible(self, read: Any) -> None:
        w = self._windows[self._window_index(read.reference_start)]
        w.read_count += 1
        w.mapped_primary += 1
        if read.is_duplicate:  # eligible dup only if policy keeps duplicates
            w.duplicate += 1

        mq = int(read.mapping_quality)
        self._mq.observe(mq)
        self._mq_hist.observe(mq)
        w.mq_sum += mq
        w.mq_count += 1

        qlen = int(read.query_length or 0)
        if qlen:
            self._rl.observe(qlen)
            self._rl_hist.observe(qlen)
            self._readlen_seen.add(qlen)

        quals = read.query_qualities
        self.bases_observed += qlen
        if quals is not None:
            n = len(quals)
            self.bases_with_quality += n
            self._bq_hist.observe_many([int(q) for q in quals])
            if n:
                w.bq_sum += float(sum(quals))
                w.bq_count += n

        self._observe_cigar(read, w)
        self._observe_pairing(read)

    def _observe_cigar(self, read: Any, w: WindowAccumulator) -> None:
        cigar = read.cigartuples
        if not cigar:
            return
        aligned = soft = hard = ins = dele = skip = qcons = 0
        for op, length in cigar:
            if op in _ALIGNED:
                aligned += length
            if op in _QUERY_CONSUMING:
                qcons += length
            if op == 4:
                soft += length
            elif op == 5:
                hard += length
            elif op == 1:
                ins += length
            elif op == 2:
                dele += length
            elif op == 3:
                skip += length
        self.aligned_query_bases += aligned
        self.soft_clipped_bases += soft
        self.hard_clipped_bases += hard
        self.inserted_bases += ins
        self.deleted_bases += dele
        self.skipped_bases += skip
        self.query_consuming_bases += qcons
        if soft:
            self.soft_clipped_reads += 1
            w.softclip_reads += 1
        if ins or dele:
            self.indel_reads += 1
        w.aligned_bases += aligned
        w.indel_bases += ins + dele

        if read.has_tag("NM"):
            self.saw_nm = True
            nm = int(read.get_tag("NM"))
            self.nm_sum += nm
            self.nm_reads += 1
            self.nm_aligned_bases += aligned
            w.nm_sum += nm
            w.nm_reads += 1
            w.nm_aligned_bases += aligned
        if read.has_tag("MD"):
            self.saw_md = True

    def _observe_pairing(self, read: Any) -> None:
        if not read.is_paired:
            return
        self.eligible_paired += 1
        if not read.is_proper_pair:
            self.eligible_paired_improper += 1
        tlen = int(read.template_length)
        if read.is_proper_pair and tlen > 0:  # leftmost mate: count each pair once
            self._tlen_hist.observe(abs(tlen))

    # -- finalizers ---------------------------------------------------------- #
    def filter_counts(self) -> FilterCounts:
        return FilterCounts(
            observed=self.observed,
            included=self.included,
            excluded_unmapped=self.excl_unmapped,
            excluded_secondary=self.excl_secondary,
            excluded_supplementary=self.excl_supplementary,
            excluded_duplicate=self.excl_duplicate,
            excluded_qcfail=self.excl_qcfail,
            excluded_below_mapq=self.excl_below_mapq,
        )

    def alignment_metrics(self) -> AlignmentMetrics:
        obs = self.observed or 1
        paired = self.paired or 1
        mapped_primary = self.included or 1
        return AlignmentMetrics(
            total_observed_alignments=self.observed,
            included_primary_alignments=self.included,
            mapped_fraction=self.mapped / obs,
            unmapped_fraction=self.unmapped / obs,
            duplicate_fraction=self.duplicate / obs,
            secondary_fraction=self.secondary / obs,
            supplementary_fraction=self.supplementary / obs,
            qcfail_fraction=self.qcfail / obs,
            paired_fraction=self.paired / obs,
            proper_pair_fraction=self.proper_pair / paired,
            reverse_strand_fraction=self.reverse / mapped_primary,
            mate_unmapped_fraction=self.mate_unmapped / paired,
        )

    def mapping_quality_metrics(self) -> MappingQualityMetrics:
        n = self._mq.n or 1
        mq0 = self._mq_hist.counts[0] if self._mq_hist.counts else 0
        return MappingQualityMetrics(
            count=self._mq.n,
            mean=self._mq.mean,
            stddev=self._mq.stddev,
            minimum=self._mq.min_or_zero,
            maximum=self._mq.max_or_zero,
            quantiles=self._mq_hist.quantile_map(),
            mean_mapping_quality_phred=self._mq.mean,
            mq0_fraction=mq0 / n,
            mq_lt20_fraction=self._mq_hist.count_at_or_below(19) / n,
        )

    def base_quality_metrics(self) -> BaseQualityMetrics:
        total = self._bq_hist.total or 1
        observed = self.bases_observed or 1
        # mean/stddev from the histogram (bounded, deterministic)
        mean, var = _hist_mean_var(self._bq_hist)
        return BaseQualityMetrics(
            bases_observed=self.bases_observed,
            bases_with_quality=self.bases_with_quality,
            mean_base_quality_phred=mean,
            stddev_base_quality_phred=var**0.5,
            quantiles_phred=self._bq_hist.quantile_map(),
            bq_lt20_fraction=self._bq_hist.count_at_or_below(19) / total,
            missing_quality_fraction=max(0.0, 1.0 - self.bases_with_quality / observed),
        )

    def read_length_metrics(self) -> ReadLengthMetrics:
        return ReadLengthMetrics(
            count=self._rl.n,
            mean=self._rl.mean,
            stddev=self._rl.stddev,
            minimum=self._rl.min_or_zero,
            maximum=self._rl.max_or_zero,
            quantiles=self._rl_hist.quantile_map(),
            variable_read_length=len(self._readlen_seen) > 1,
        )

    def fragment_metrics(self) -> FragmentMetrics:
        paired = self.eligible_paired or 1
        pairs = self.completed_pairs or 1
        mean, var = _hist_mean_var(self._tlen_hist)
        return FragmentMetrics(
            eligible_pair_count=self.completed_pairs,
            template_length_policy="abs_tlen_one_mate_per_proper_pair",
            mean_insert_size_bp=mean,
            stddev_insert_size_bp=var**0.5,
            insert_size_mad_bp=self._tlen_hist.mad(),
            quantiles_bp=self._tlen_hist.quantile_map(),
            overlapping_mate_fraction=self.overlapping_pairs / pairs,
            abnormal_pair_fraction=self.eligible_paired_improper / paired,
        )

    def cigar_metrics(self) -> CigarMetrics:
        aligned = self.aligned_query_bases or 1
        qcons = self.query_consuming_bases or 1
        eligible = self.included or 1
        nm_aligned = self.nm_aligned_bases or 1
        return CigarMetrics(
            aligned_query_bases=self.aligned_query_bases,
            soft_clipped_bases=self.soft_clipped_bases,
            hard_clipped_bases=self.hard_clipped_bases,
            inserted_bases=self.inserted_bases,
            deleted_bases=self.deleted_bases,
            skipped_bases=self.skipped_bases,
            query_consuming_bases=self.query_consuming_bases,
            soft_clipped_read_fraction=self.soft_clipped_reads / eligible,
            soft_clipped_base_fraction=self.soft_clipped_bases / qcons,
            indel_bearing_read_fraction=self.indel_reads / eligible,
            nm_per_aligned_base=self.nm_sum / nm_aligned,
            nm_availability_fraction=self.nm_reads / eligible,
            cigar_ins_del_burden=(self.inserted_bases + self.deleted_bases) / aligned,
        )


def _hist_mean_var(hist: IntHistogram) -> tuple[float, float]:
    total = hist.total
    if total == 0:
        return 0.0, 0.0
    s = 0.0
    for i, c in enumerate(hist.counts):
        s += (hist.lo + i) * c
    s += hist.lo * hist.below + hist.hi * hist.above
    mean = s / total
    var = 0.0
    for i, c in enumerate(hist.counts):
        var += ((hist.lo + i) - mean) ** 2 * c
    var += ((hist.lo - mean) ** 2) * hist.below + ((hist.hi - mean) ** 2) * hist.above
    return mean, var / total


# ensure percentile key helper is importable alongside the scanner
_ = percentile_keys
