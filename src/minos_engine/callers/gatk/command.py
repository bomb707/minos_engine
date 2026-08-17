"""GATK CLI-flag metadata — mapping only, NO execution.

Stage 0 does not run GATK. This module records the canonical CONFIG-key ->
HaplotypeCaller CLI flag mapping and can render an argument list from an already
canonicalized effective CONFIG (pure string building). It never spawns a
process, opens a container, or touches the filesystem.
"""

from __future__ import annotations

from typing import Any

from minos_engine.common.errors import ConfigValidationError

from .parameter_registry import REGISTRY

__all__ = ["CLI_FLAGS", "render_flag_args"]

# CONFIG key -> HaplotypeCaller CLI flag (verified against the reference subnet).
CLI_FLAGS: dict[str, str] = {
    "min_base_quality_score": "--min-base-quality-score",
    "min_mapping_quality_score": "--minimum-mapping-quality",
    "base_quality_score_threshold": "--base-quality-score-threshold",
    "standard_min_confidence_threshold_for_calling": "--standard-min-confidence-threshold-for-calling",
    "emit_ref_confidence": "--emit-ref-confidence",
    "pcr_indel_model": "--pcr-indel-model",
    "min_pruning": "--min-pruning",
    "max_alternate_alleles": "--max-alternate-alleles",
    "min_dangling_branch_length": "--min-dangling-branch-length",
    "recover_all_dangling_branches": "--recover-all-dangling-branches",
    "max_num_haplotypes_in_population": "--max-num-haplotypes-in-population",
    "adaptive_pruning_initial_error_rate": "--adaptive-pruning-initial-error-rate",
    "pruning_lod_threshold": "--pruning-lod-threshold",
    "active_probability_threshold": "--active-probability-threshold",
    "min_assembly_region_size": "--min-assembly-region-size",
    "max_assembly_region_size": "--max-assembly-region-size",
    "assembly_region_padding": "--assembly-region-padding",
    "pair_hmm_gap_continuation_penalty": "--pair-hmm-gap-continuation-penalty",
    "phred_scaled_global_read_mismapping_rate": "--phred-scaled-global-read-mismapping-rate",
    "heterozygosity": "--heterozygosity",
    "indel_heterozygosity": "--indel-heterozygosity",
    "sample_ploidy": "--sample-ploidy",
    "contamination_fraction_to_filter": "--contamination-fraction-to-filter",
    "max_reads_per_alignment_start": "--max-reads-per-alignment-start",
    "dont_use_soft_clipped_bases": "--dont-use-soft-clipped-bases",
}


def render_flag_args(effective_config: dict[str, Any]) -> list[str]:
    """Render ``[flag, value, ...]`` from an effective CONFIG (no execution).

    Booleans render as ``--flag true`` / ``--flag false`` to mirror GATK's
    explicit boolean handling. Raises if a key has no known flag.
    """
    args: list[str] = []
    for name in REGISTRY.names():
        if name not in effective_config:
            continue
        flag = CLI_FLAGS.get(name)
        if flag is None:  # pragma: no cover - registry/flag table drift guard
            raise ConfigValidationError(f"no CLI flag registered for parameter {name!r}")
        value = effective_config[name]
        rendered = "true" if value is True else "false" if value is False else str(value)
        args.extend([flag, rendered])
    return args
