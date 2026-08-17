"""Atomic, deterministic serialization of the three Layer 1 artifacts (spec §2, §17).

``bam-profile-v1.json`` and ``profile-manifest-v1.json`` are canonical JSON;
``window-profile-v1.parquet`` uses a fixed, dictionary-free Arrow schema. Each
file is written to a ``.tmp`` sibling, fsynced, validated, then atomically
renamed. The manifest is written last and carries the sha256 of the first two
outputs. If a final rename fails, a :class:`SerializationError` is raised and no
apparently-complete output set is left behind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

from .contracts import BamProfile, ContextFingerprint, ProfileManifest, WindowRow

__all__ = [
    "SerializationError",
    "WINDOW_ARROW_SCHEMA",
    "serialize_profile",
    "write_windows_parquet",
]


class SerializationError(MinosEngineError):
    """A Layer 1 artifact could not be atomically and validly written."""


WINDOW_ARROW_SCHEMA = pa.schema(
    [
        ("profile_id", pa.string()),
        ("window_id", pa.int32()),
        ("contig", pa.string()),
        ("start0", pa.int64()),
        ("end0", pa.int64()),
        ("length_bp", pa.int64()),
        ("stratum", pa.string()),
        ("read_count", pa.int64()),
        ("depth_mean_reads_per_base", pa.float64()),
        ("depth_median_reads_per_base", pa.float64()),
        ("mq_mean_phred", pa.float64()),
        ("bq_mean_phred", pa.float64()),
        ("duplicate_fraction", pa.float64()),
        ("soft_clipped_read_fraction", pa.float64()),
        ("nm_per_aligned_base", pa.float64()),
        ("cigar_ins_del_burden", pa.float64()),
        ("gc_fraction", pa.float64()),
        ("entropy_bits", pa.float64()),
        ("homopolymer_base_fraction", pa.float64()),
        ("candidate_snp_density_per_base", pa.float64()),
        ("candidate_indel_density_per_base", pa.float64()),
        ("difficult_flags", pa.string()),
        ("sampled", pa.bool_()),
        ("selection_probability", pa.float64()),
        ("analysis_weight", pa.float64()),
    ]
)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp, path)
    except OSError as exc:  # pragma: no cover - platform rename failure
        tmp.unlink(missing_ok=True)
        raise SerializationError(f"atomic rename failed for {path}: {exc}") from exc


def write_windows_parquet(rows: list[WindowRow], path: Path) -> str:
    """Write the fixed-schema window Parquet deterministically; return its sha256."""
    columns: dict[str, list[object]] = {name: [] for name in WINDOW_ARROW_SCHEMA.names}
    for r in rows:
        columns["profile_id"].append(r.profile_id)
        columns["window_id"].append(r.window_id)
        columns["contig"].append(r.contig)
        columns["start0"].append(r.start0)
        columns["end0"].append(r.end0)
        columns["length_bp"].append(r.length_bp)
        columns["stratum"].append(r.stratum)
        columns["read_count"].append(r.read_count)
        columns["depth_mean_reads_per_base"].append(r.depth_mean_reads_per_base)
        columns["depth_median_reads_per_base"].append(r.depth_median_reads_per_base)
        columns["mq_mean_phred"].append(r.mq_mean_phred)
        columns["bq_mean_phred"].append(r.bq_mean_phred)
        columns["duplicate_fraction"].append(r.duplicate_fraction)
        columns["soft_clipped_read_fraction"].append(r.soft_clipped_read_fraction)
        columns["nm_per_aligned_base"].append(r.nm_per_aligned_base)
        columns["cigar_ins_del_burden"].append(r.cigar_ins_del_burden)
        columns["gc_fraction"].append(r.gc_fraction)
        columns["entropy_bits"].append(r.entropy_bits)
        columns["homopolymer_base_fraction"].append(r.homopolymer_base_fraction)
        columns["candidate_snp_density_per_base"].append(r.candidate_snp_density_per_base)
        columns["candidate_indel_density_per_base"].append(r.candidate_indel_density_per_base)
        columns["difficult_flags"].append(";".join(r.difficult_flags))
        columns["sampled"].append(r.sampled)
        columns["selection_probability"].append(r.selection_probability)
        columns["analysis_weight"].append(r.analysis_weight)

    table = pa.table(columns, schema=WINDOW_ARROW_SCHEMA)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(
        table,
        tmp,
        compression="none",
        write_statistics=False,
        version="2.6",
    )
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    data = tmp.read_bytes()
    try:
        os.replace(tmp, path)
    except OSError as exc:  # pragma: no cover
        tmp.unlink(missing_ok=True)
        raise SerializationError(f"atomic rename failed for {path}: {exc}") from exc
    return sha256_hex(data)


def serialize_profile(
    *,
    profile: BamProfile,
    windows: list[WindowRow],
    fingerprint: ContextFingerprint,
    output_dir: Path,
    created_at: str,
) -> tuple[Path, Path, Path]:
    """Write profile JSON, window Parquet, then the manifest — atomically."""
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_path = output_dir / "bam-profile-v1.json"
    windows_path = output_dir / "window-profile-v1.parquet"
    manifest_path = output_dir / "profile-manifest-v1.json"

    profile_bytes = canonical_json_bytes(profile.model_dump(mode="json"))
    # Validate by round-tripping through the contract before committing bytes.
    BamProfile.model_validate_json(profile_bytes)
    _atomic_write_bytes(profile_path, profile_bytes)
    profile_sha = sha256_hex(profile_bytes)

    windows_sha = write_windows_parquet(windows, windows_path)

    manifest = ProfileManifest(
        profile_id=profile.profile_id,
        created_at=created_at,
        profiler_version=profile.provenance.profiler_version,
        profiler_config_hash=profile.provenance.config_hash,
        region_contig=profile.region.contig,
        region_start0=profile.region.start0,
        region_end0=profile.region.end0_exclusive,
        status=profile.status,
        profile_sha256=profile_sha,
        windows_sha256=windows_sha,
        windows_row_count=len(windows),
        fingerprint_hash=fingerprint.fingerprint_hash,
    )
    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
    _atomic_write_bytes(manifest_path, manifest_bytes)
    return profile_path, windows_path, manifest_path
