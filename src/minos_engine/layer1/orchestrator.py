"""Orchestrator — the only module that sequences stages and owns the deadline /
degradation state machine (Layer 1 spec §4).

Sequences: one-pass alignment scan → finalize windows/coverage → reference
profiling → cost-model choice → FULL or ADAPTIVE (or skipped) pileup → derive
descriptive features → confidence/completion → assemble the profile, window rows,
and fingerprint. Every stage checks the same monotonic :class:`Deadline` at
bounded work units; when time runs short the run degrades deterministically and
records the reason rather than silently dropping features or overrunning.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from minos_engine.common.clock import Clock, Deadline
from minos_engine.common.hashing import canonical_hash

from .adapters.pysam_adapter import pysam_version
from .config import Layer1Config
from .contracts import (
    BamProfile,
    CompletionReport,
    ContextFingerprint,
    CoverageMetrics,
    DegradationRecord,
    DegradationStage,
    PileupMode,
    ProfilerProvenance,
    ProfileStatus,
    RuntimeComplexity,
    SpatialMetrics,
    StageTiming,
    WindowRow,
)
from .cost_model import choose_mode, predict_pileup_seconds
from .coverage import DUP_INCLUDING, FRAGMENT_PRIMARY, coverage_view
from .difficulty import completion_report, confidence_report, difficulty_vector
from .fingerprint import build_fingerprint
from .pileup import WindowEvidence, aggregate_evidence, profile_pileup_window
from .reference_profile import profile_reference_sequence
from .sampling import SamplingPlan, WindowFeature, select_windows
from .scan import OnePassScanner
from .validation import ValidatedInputs
from .windows import generate_windows

__all__ = ["ProfileBundle", "run_profile"]

_CORE_FAMILIES = (
    "reads",
    "coverage",
    "mapping_quality",
    "base_quality",
    "read_length",
    "pairing",
    "alignment",
    "reference_context",
    "spatial",
)


@dataclass
class ProfileBundle:
    profile: BamProfile
    windows: list[WindowRow]
    fingerprint: ContextFingerprint


def run_profile(
    *,
    profile_id: str,
    inputs: ValidatedInputs,
    config: Layer1Config,
    clock: Clock,
    deadline: Deadline,
) -> ProfileBundle:
    region = inputs.region
    timings: list[StageTiming] = []
    degradations: list[DegradationRecord] = []
    reserve = config.budget.serialization_reserve_seconds

    from .filters import ReadFilterPolicy

    policy = ReadFilterPolicy.from_config(config.filters)
    scanner = OnePassScanner(
        region.start0,
        region.end0_exclusive,
        config.window.primary_bp,
        policy,
    )

    # --- Stage: one-pass alignment scan (mandatory) ------------------------- #
    t0 = clock.monotonic()
    scan_truncated = False
    for n, read in enumerate(
        inputs.alignment.fetch(region.contig, region.start0, region.end0_exclusive), start=1
    ):
        scanner.observe(read)
        if n % 200000 == 0 and deadline.remaining_seconds() <= reserve:
            scan_truncated = True
            break
    timings.append(StageTiming(stage="stream_alignments", elapsed_seconds=clock.monotonic() - t0))

    # --- Stage: finalize windows + coverage --------------------------------- #
    t0 = clock.monotonic()
    win_specs = generate_windows(region, config.window.primary_bp)
    cov = scanner.coverage
    frag_depth = cov.depth(FRAGMENT_PRIMARY)
    dup_depth = cov.depth(DUP_INCLUDING)
    q_ps = config.coverage.quantiles
    coverage_metrics = CoverageMetrics(
        fragment_primary=coverage_view(
            frag_depth,
            view_name="fragment_primary",
            depth_semantics="fragment_depth_excl_dup_overlap_corrected",
            quantile_ps=q_ps,
        ),
        duplicate_including=coverage_view(
            dup_depth,
            view_name="duplicate_including",
            depth_semantics="read_depth_incl_dup",
            quantile_ps=q_ps,
        ),
        eligible_region_bases=region.length_bp,
    )
    timings.append(
        StageTiming(stage="finalize_windows_coverage", elapsed_seconds=clock.monotonic() - t0)
    )

    # --- Stage: reference profiling (full exact interval) ------------------- #
    t0 = clock.monotonic()
    full_seq = inputs.fasta.fetch(region.contig, region.start0, region.end0_exclusive).upper()
    homo_min = int(config.reference["homopolymer_min_run"])
    dinuc_min = int(config.reference["dinucleotide_min_run"])
    reference_metrics = profile_reference_sequence(
        full_seq, homopolymer_min_run=homo_min, dinucleotide_min_run=dinuc_min
    )
    timings.append(StageTiming(stage="profile_reference", elapsed_seconds=clock.monotonic() - t0))

    # --- Derive per-window scan features ------------------------------------ #
    win_features: list[WindowFeature] = []
    win_ref: dict[int, tuple[float, float, float]] = {}
    for spec in win_specs:
        wa = scanner.windows[spec.window_id]
        wdepth = frag_depth[spec.start0 - region.start0 : spec.end0 - region.start0]
        depth_mean = float(wdepth.mean()) if wdepth.size else 0.0
        mq_mean = wa.mq_sum / wa.mq_count if wa.mq_count else 0.0
        bq_mean = wa.bq_sum / wa.bq_count if wa.bq_count else 0.0
        softclip_frac = wa.softclip_reads / wa.read_count if wa.read_count else 0.0
        nm_per_base = wa.nm_sum / wa.nm_aligned_bases if wa.nm_aligned_bases else 0.0
        indel_burden = wa.indel_bases / wa.aligned_bases if wa.aligned_bases else 0.0
        wseq = full_seq[spec.start0 - region.start0 : spec.end0 - region.start0]
        wref = profile_reference_sequence(
            wseq, homopolymer_min_run=homo_min, dinucleotide_min_run=dinuc_min
        )
        win_ref[spec.window_id] = (
            wref.gc_fraction,
            wref.entropy_bits,
            wref.homopolymer_base_fraction,
        )
        win_features.append(
            WindowFeature(
                window_id=spec.window_id,
                depth_mean=depth_mean,
                mq_mean=mq_mean,
                bq_mean=bq_mean,
                softclip_read_fraction=softclip_frac,
                nm_per_base=nm_per_base,
                indel_burden=indel_burden,
                entropy_bits=wref.entropy_bits,
                homopolymer_fraction=wref.homopolymer_base_fraction,
                is_boundary=(spec.window_id == 0 or spec.window_id == len(win_specs) - 1),
            )
        )

    # --- Sampling plan ------------------------------------------------------ #
    tiebreak_seed = canonical_hash(
        {
            "bam_sha256": inputs.identity.bam_sha256,
            "region": region.model_dump(mode="json"),
            "profiler_version": config.profiler_config_version,
            "config_hash": config.config_hash,
        }
    )
    plan = select_windows(
        win_features,
        _stratum_thresholds(config),
        per_stratum_quota=int(config.sampling["per_stratum_quota"]),
        total_quota=int(config.sampling["refinement_window_quota"]),
        tiebreak_seed=tiebreak_seed,
    )

    # --- Cost model + pileup mode ------------------------------------------- #
    cigar = scanner.cigar_metrics()
    predicted = predict_pileup_seconds(
        config.cost_model,
        region_bp=region.length_bp,
        read_count=scanner.included,
        mean_depth=coverage_metrics.fragment_primary.mean_depth_reads_per_base,
        max_depth_proxy=float(coverage_metrics.fragment_primary.max_depth),
        clipping_rate=cigar.soft_clipped_read_fraction,
        cigar_complexity=cigar.indel_bearing_read_fraction + cigar.nm_per_aligned_base,
    )
    mode = choose_mode(
        predicted,
        pileup_soft_seconds=config.pileup.soft_seconds,
        remaining_seconds=deadline.remaining_seconds(),
        serialization_reserve_seconds=reserve,
    )
    if scan_truncated:
        mode = PileupMode.SKIPPED

    # --- Pileup ------------------------------------------------------------- #
    t0 = clock.monotonic()
    if mode is PileupMode.FULL:
        target_ids = [s.window_id for s in win_specs]
    elif mode is PileupMode.ADAPTIVE:
        target_ids = list(plan.selected)
    else:
        target_ids = []

    evidences: list[WindowEvidence] = []
    per_window_evidence: dict[int, WindowEvidence] = {}
    pileup_truncated = False
    support = tuple(config.thresholds["support"])
    afs = tuple(config.thresholds["allele_fraction"])
    low_bq = int(config.thresholds["base_quality_low"])
    for wid in target_ids:
        if deadline.remaining_seconds() <= reserve:
            pileup_truncated = True
            break
        spec = win_specs[wid]
        ev = profile_pileup_window(
            inputs.alignment,
            inputs.fasta,
            region.contig,
            spec.start0,
            spec.end0,
            max_depth=config.pileup.max_depth,
            stepper=config.pileup.stepper,
            support_thresholds=support,
            af_thresholds=afs,
            low_alt_bq=low_bq,
        )
        evidences.append(ev)
        per_window_evidence[wid] = ev
    timings.append(StageTiming(stage="pileup", elapsed_seconds=clock.monotonic() - t0))

    analyzed_bases = sum(e.length_bp for e in evidences)
    variant_evidence = aggregate_evidence(
        evidences,
        eligible_region_bases=region.length_bp,
        analyzed_bases=analyzed_bases,
    )

    # --- Spatial, difficulty, confidence, completion ------------------------ #
    alignment_metrics = scanner.alignment_metrics()
    mq = scanner.mapping_quality_metrics()
    bq = scanner.base_quality_metrics()
    rl = scanner.read_length_metrics()
    frag = scanner.fragment_metrics()

    spatial = SpatialMetrics(
        primary_window_count=len(win_specs),
        refined_window_count=len(target_ids),
        sampled_window_count=len(per_window_evidence),
        analyzed_bases=analyzed_bases,
        interval_fraction_analyzed=(analyzed_bases / region.length_bp) if region.length_bp else 0.0,
        stratum_window_counts=plan.stratum_counts,
        sampling_uncertainty=_sampling_uncertainty(len(per_window_evidence), len(win_specs)),
    )
    difficulty = difficulty_vector(
        alignment=alignment_metrics,
        mapping_quality=mq,
        base_quality=bq,
        cigar=cigar,
        coverage=coverage_metrics.fragment_primary,
        reference=reference_metrics,
    )

    pileup_ran = bool(per_window_evidence)
    ve_completion = (
        variant_evidence.analyzed_callable_bases / region.length_bp if region.length_bp else 0.0
    )
    completed = list(_CORE_FAMILIES) + ["difficulty"]
    if pileup_ran and not pileup_truncated:
        completed.append("variant_evidence")

    status = ProfileStatus.COMPLETE
    if scan_truncated:
        status = ProfileStatus.PARTIAL
        degradations.append(
            _degradation(
                DegradationStage.CORE_ONLY,
                "scan deadline pressure",
                "stream_alignments",
                omitted=("variant_evidence",),
                completed=tuple(completed),
                deadline=deadline,
                clock=clock,
                usable=True,
            )
        )
    elif mode is PileupMode.SKIPPED:
        status = ProfileStatus.PARTIAL
        degradations.append(
            _degradation(
                DegradationStage.REDUCED_PILEUP,
                "insufficient time for pileup",
                "cost_model",
                omitted=("variant_evidence",),
                completed=tuple(completed),
                deadline=deadline,
                clock=clock,
                usable=True,
            )
        )
    elif mode is PileupMode.ADAPTIVE:
        stage = (
            DegradationStage.REDUCED_WINDOWS
            if pileup_truncated
            else DegradationStage.REDUCED_PILEUP
        )
        if pileup_truncated:
            status = ProfileStatus.PARTIAL
        degradations.append(
            _degradation(
                stage,
                "adaptive pileup (cost model)",
                "cost_model",
                omitted=() if not pileup_truncated else ("variant_evidence",),
                completed=tuple(completed),
                deadline=deadline,
                clock=clock,
                usable=True,
            )
        )

    confidence = confidence_report(
        integrity=1.0,
        completeness=1.0
        if (mode is PileupMode.FULL and not pileup_truncated)
        else max(0.0, ve_completion),
        availability=_availability(
            cigar.nm_availability_fraction,
            reference_metrics.reference_available,
            inputs.identity.index_status.value == "available",
            bq.missing_quality_fraction,
        ),
        consistency=1.0 - min(1.0, variant_evidence.max_depth_capped_fraction),
        high_min=float(config.confidence["high_min"]),
        medium_min=float(config.confidence["medium_min"]),
    )
    completion: CompletionReport = completion_report(tuple(completed), ve_completion, status)

    runtime_complexity = RuntimeComplexity(
        predicted_pileup_seconds=predicted,
        actual_pileup_seconds=next(
            (t.elapsed_seconds for t in timings if t.stage == "pileup"), 0.0
        ),
        chosen_pileup_mode=mode,
    )

    provenance = ProfilerProvenance(
        profiler_version=config.profiler_config_version,
        config_version=config.schema_version,
        config_hash=config.config_hash,
        pysam_version=pysam_version(),
        schema_version="bam-profile-v1",
    )

    profile = BamProfile(
        profile_id=profile_id,
        status=status,
        provenance=provenance,
        identity=inputs.identity,
        region=region,
        header=inputs.header,
        filter_counts=scanner.filter_counts(),
        reads=alignment_metrics,
        coverage=coverage_metrics,
        mapping_quality=mq,
        base_quality=bq,
        read_length=rl,
        pairing=frag,
        alignment=cigar,
        variant_evidence=variant_evidence,
        reference_context=reference_metrics,
        spatial=spatial,
        difficulty=difficulty,
        runtime_complexity=runtime_complexity,
        confidence=confidence,
        completion=completion,
        stage_timings=tuple(timings),
        degradation=tuple(degradations),
        warnings=inputs.warnings,
    )

    windows = _build_window_rows(
        profile_id=profile_id,
        win_specs=win_specs,
        scanner=scanner,
        frag_depth=frag_depth,
        region_start0=region.start0,
        win_ref=win_ref,
        plan=plan,
        per_window_evidence=per_window_evidence,
    )
    fingerprint = build_fingerprint(
        profile,
        sampling_plan_hash=plan.plan_hash,
        read_filter_policy_hash=policy.policy_hash(),
    )
    return ProfileBundle(profile=profile, windows=windows, fingerprint=fingerprint)


def _stratum_thresholds(config: Layer1Config) -> dict[str, float]:
    s = config.sampling["strata"]
    return {
        "low_coverage_depth": float(s["low_coverage_depth"]),
        "high_coverage_depth": float(s["high_coverage_depth"]),
        "low_mapping_quality_mean": 40.0,
        "low_base_quality_mean": 30.0,
        "clipping_read_fraction": float(s["clipping_read_fraction"]),
        "nm_per_base": float(s["nm_per_base"]),
        "indel_burden": float(s["indel_burden"]),
        "low_entropy_bits": float(s["low_entropy_bits"]),
        "homopolymer_burden": float(s["homopolymer_burden"]),
    }


def _sampling_uncertainty(sampled: int, total: int) -> float:
    if total == 0 or sampled >= total:
        return 0.0
    frac = sampled / total
    return float((1.0 - frac) ** 0.5)


def _availability(nm_avail: float, ref_avail: bool, index_avail: bool, missing_bq: float) -> float:
    signals = [
        1.0 if nm_avail > 0 else 0.0,
        1.0 if ref_avail else 0.0,
        1.0 if index_avail else 0.0,
        1.0 - min(1.0, missing_bq),
    ]
    return sum(signals) / len(signals)


def _degradation(
    stage: DegradationStage,
    reason: str,
    trigger: str,
    *,
    omitted: tuple[str, ...],
    completed: tuple[str, ...],
    deadline: Deadline,
    clock: Clock,
    usable: bool,
) -> DegradationRecord:
    return DegradationRecord(
        stage=stage,
        reason=reason,
        trigger=trigger,
        omitted_features=omitted,
        completed_features=completed,
        elapsed_seconds=max(0.0, deadline.budget_seconds - deadline.remaining_seconds()),
        remaining_seconds=deadline.remaining_seconds(),
        usable=usable,
        status=ProfileStatus.PARTIAL if omitted else ProfileStatus.COMPLETE,
    )


def _build_window_rows(
    *,
    profile_id: str,
    win_specs: tuple[Any, ...],
    scanner: OnePassScanner,
    frag_depth: Any,
    region_start0: int,
    win_ref: dict[int, tuple[float, float, float]],
    plan: SamplingPlan,
    per_window_evidence: dict[int, WindowEvidence],
) -> list[WindowRow]:
    import numpy as np

    rows: list[WindowRow] = []
    for spec in win_specs:
        wa = scanner.windows[spec.window_id]
        wdepth = frag_depth[spec.start0 - region_start0 : spec.end0 - region_start0]
        depth_mean = float(wdepth.mean()) if wdepth.size else 0.0
        depth_median = float(np.median(wdepth)) if wdepth.size else 0.0
        gc, entropy, homo = win_ref[spec.window_id]
        ev = per_window_evidence.get(spec.window_id)
        snp_density = ev.snp_density() if ev else 0.0
        indel_density = ev.indel_density() if ev else 0.0
        rows.append(
            WindowRow(
                profile_id=profile_id,
                window_id=spec.window_id,
                contig=spec.contig,
                start0=spec.start0,
                end0=spec.end0,
                length_bp=spec.length_bp,
                stratum=plan.primary_stratum[spec.window_id],
                read_count=wa.read_count,
                depth_mean_reads_per_base=depth_mean,
                depth_median_reads_per_base=depth_median,
                mq_mean_phred=wa.mq_sum / wa.mq_count if wa.mq_count else 0.0,
                bq_mean_phred=wa.bq_sum / wa.bq_count if wa.bq_count else 0.0,
                duplicate_fraction=wa.raw_dup / wa.raw_reads if wa.raw_reads else 0.0,
                soft_clipped_read_fraction=wa.softclip_reads / wa.read_count
                if wa.read_count
                else 0.0,
                nm_per_aligned_base=wa.nm_sum / wa.nm_aligned_bases if wa.nm_aligned_bases else 0.0,
                cigar_ins_del_burden=wa.indel_bases / wa.aligned_bases if wa.aligned_bases else 0.0,
                gc_fraction=gc,
                entropy_bits=entropy,
                homopolymer_base_fraction=homo,
                candidate_snp_density_per_base=snp_density,
                candidate_indel_density_per_base=indel_density,
                difficult_flags=plan.difficult_flags[spec.window_id],
                sampled=spec.window_id in per_window_evidence,
                selection_probability=plan.selection_probability[spec.window_id],
                analysis_weight=plan.analysis_weight[spec.window_id],
            )
        )
    return rows


_ = time  # reserved for future wall-clock stage annotations (kept off content identity)
