"""Real-BAM integration report contract (prompt §4, §19 group L).

A sanitized, machine-readable record of a real-BAM qualification run: content
identities (never private absolute paths), region, profiler identity, fingerprint,
timing, peak memory, degradation status, warnings, feature-family completion, and
the repeat-run fingerprint equality. It records that real-BAM qualification was
performed (``real_bam_qualified``) so the L1-READY gate can never claim real-BAM
qualification when only synthetic fixtures were used.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["IntegrationReport"]


class IntegrationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "layer1-integration-report-v1"
    dataset_id: str = Field(min_length=1)
    bam_sha256: str = Field(min_length=64, max_length=64)
    bam_size_bytes: int = Field(gt=0)
    bai_sha256: str = Field(min_length=64, max_length=64)
    reference_sha256: str = Field(min_length=64, max_length=64)
    fai_sha256: str = Field(min_length=64, max_length=64)
    region_source: str = Field(min_length=1)
    region_contig: str = Field(min_length=1)
    region_start0: int = Field(ge=0)
    region_end0: int = Field(gt=0)
    profiler_version: str = Field(min_length=1)
    profiler_config_hash: str = Field(min_length=1)
    profile_schema_hash: str = Field(min_length=1)
    fingerprint_hash: str = Field(min_length=64, max_length=64)
    first_run_elapsed_seconds: float = Field(ge=0)
    first_run_peak_rss_mb: float = Field(ge=0)
    second_run_elapsed_seconds: float = Field(ge=0)
    second_run_peak_rss_mb: float = Field(ge=0)
    repeat_run_fingerprint_equal: bool
    degradation_status: str = Field(min_length=1)
    pileup_mode: str = Field(min_length=1)
    completed_families: tuple[str, ...]
    warnings: tuple[str, ...]
    hard_limit_seconds: float = Field(gt=0)
    hard_limit_met: bool
    real_bam_qualified: bool
