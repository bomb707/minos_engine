"""Per-dataset Layer 1 acceptance worker (run one chromosome, subprocess-isolated).

Runs numerical comparison vs the independent oracle, truth-relevance, truth
isolation, three-run determinism, cross-region/boundary, and robustness for one
selected dataset, writing a JSON result. A parent loop runs it once per
chromosome so peak RSS is isolated per dataset. Uses only explicit BAM/BAI/
FASTA/FAI paths — truth/mutation files are never passed to Layer 1.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.qualification import compare, oracle, truth_relevance

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest
from minos_engine.layer1.service import Layer1Service

DS = "/home/hr/bittensor/minos_subnet/datasets"
_VOLATILE = {"stage_timings", "runtime_complexity", "degradation"}


def _sha_file(path: str) -> str:
    import hashlib

    d = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            d.update(b)
    return d.hexdigest()


def _content_hash(profile: Any) -> str:
    dumped = {k: v for k, v in profile.model_dump(mode="json").items() if k not in _VOLATILE}
    return sha256_hex(canonical_json_bytes(dumped))


def _req(
    rid: str, contig: str, s0: int, e0: int, cfg: Any, budget: float = 300.0
) -> ProfileRequest:
    bam = f"{DS}/practice/round_{rid}/input.bam"
    ref = f"{DS}/reference/{contig}/{contig}.fa"
    return ProfileRequest(
        round_id=rid,
        bam_path=bam,
        bai_path=bam + ".bai",
        reference_path=ref,
        fai_path=ref + ".fai",
        region_source=f"{contig}:{s0}-{e0}",
        region_coordinate_convention="zero_based_half_open",
        budget_seconds=budget,
        cpu_limit=2,
        memory_limit_bytes=8_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )


def _paths(rid: str, contig: str) -> tuple[str, str, str, str]:
    bam = f"{DS}/practice/round_{rid}/input.bam"
    ref = f"{DS}/reference/{contig}/{contig}.fa"
    return bam, bam + ".bai", ref, ref + ".fai"


def _sanitized_dir(rid: str, contig: str) -> Path:
    """A temp dir with symlinks to ONLY bam/bai/ref/fai (no truth/mutation files)."""
    bam, bai, ref, fai = _paths(rid, contig)
    d = Path(tempfile.mkdtemp(prefix="l1san_"))
    for src, name in (
        (bam, "input.bam"),
        (bai, "input.bam.bai"),
        (ref, f"{contig}.fa"),
        (fai, f"{contig}.fa.fai"),
    ):
        os.symlink(src, d / name)
    return d


def run_dataset(chrom: str, rid: str, s0: int, e0: int) -> dict[str, Any]:
    cfg = load_layer1_config()
    contig = chrom
    bam, bai, ref, fai = _paths(rid, contig)
    svc = Layer1Service(require_prerequisite=False)
    result: dict[str, Any] = {
        "chromosome": chrom,
        "dataset_id": rid,
        "region": {"contig": contig, "start0": s0, "end0": e0, "length_bp": e0 - s0},
    }
    result["input_hashes"] = {
        "bam_sha256": _sha_file(bam),
        "bam_size_bytes": os.path.getsize(bam),
        "bai_sha256": _sha_file(bai),
        "reference_sha256": _sha_file(ref),
        "fai_sha256": _sha_file(fai),
        "truth_vcf_sha256": _sha_file(f"{DS}/practice/round_{rid}/truth.vcf.gz"),
        "mutations_sha256": _sha_file(f"{DS}/practice/round_{rid}/mutations.vcf.gz"),
    }

    # --- determinism: 3 runs on the official region ---
    fps, fvs, chs, fams, warns, times = [], [], [], [], [], []
    prof1 = None
    for i in range(3):
        t = time.monotonic()
        b = svc.profile(_req(rid, contig, s0, e0, cfg))
        times.append(time.monotonic() - t)
        if i == 0:
            prof1 = b.profile
        fps.append(b.fingerprint.fingerprint_hash)
        fvs.append(b.fingerprint.feature_values_hash)
        chs.append(_content_hash(b.profile))
        fams.append(tuple(b.profile.completion.completed_families))
        warns.append(tuple(b.profile.warnings))
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    result["determinism"] = {
        "runs": 3,
        "fingerprint_equal": len(set(fps)) == 1,
        "feature_values_equal": len(set(fvs)) == 1,
        "content_hash_equal": len(set(chs)) == 1,
        "families_equal": len(set(fams)) == 1,
        "warnings_equal": len(set(warns)) == 1,
        "fingerprint": fps[0],
        "elapsed_seconds": [round(x, 2) for x in times],
        "peak_rss_mb": round(peak_rss_mb, 1),
        "status": prof1.status.value,
    }

    # --- numerical comparison vs independent oracle ---
    orc = oracle.compute(bam, bai, ref, contig, s0, e0)
    obs = compare.observed_from_profile(prof1)
    fields = compare.compare_all(obs, orc.values)
    exact = [f for f in fields if f.kind == "exact"]
    fstrict = [f for f in fields if f.kind == "float_strict"]
    result["numerical"] = {
        "exact_fields": len(exact),
        "exact_mismatches": sum(1 for f in exact if not f.ok),
        "float_fields": len(fstrict),
        "float_failures": sum(1 for f in fstrict if not f.ok),
        "fields": [
            {
                "key": f.key,
                "observed": f.observed,
                "expected": f.expected,
                "kind": f.kind,
                "abs_error": f.abs_error,
                "rel_error": f.rel_error,
                "ok": f.ok,
            }
            for f in fields
        ],
    }

    # --- truth relevance ---
    truth = f"{DS}/practice/round_{rid}/truth.vcf.gz"
    tr, _te, _ce = truth_relevance.analyze(bam, bai, ref, truth, contig, s0, e0)
    result["truth_relevance"] = {
        "evaluable_truth": tr.evaluable_truth,
        "truth_by_class": tr.truth_by_class,
        "sensitivity_overall": tr.sensitivity_overall,
        "sensitivity_by_class": tr.sensitivity_by_class,
        "sensitivity_by_zygosity": tr.sensitivity_by_zygosity,
        "control_count": tr.control_count,
        "background_rate": tr.background_rate,
        "enrichment": tr.enrichment,
        "enrichment_ci95": list(tr.enrichment_ci95),
        "auroc": tr.auroc,
        "auprc": tr.auprc,
        "truth_with_depth": tr.truth_with_depth,
        "truth_without_alt_evidence": tr.truth_without_alt_evidence,
    }

    # --- truth isolation: sanitized (no truth) vs original dir (truth present) ---
    san = _sanitized_dir(rid, contig)
    try:
        req_san = ProfileRequest(
            round_id=rid,
            bam_path=str(san / "input.bam"),
            bai_path=str(san / "input.bam.bai"),
            reference_path=str(san / f"{contig}.fa"),
            fai_path=str(san / f"{contig}.fa.fai"),
            region_source=f"{contig}:{s0}-{e0}",
            region_coordinate_convention="zero_based_half_open",
            budget_seconds=300,
            cpu_limit=2,
            memory_limit_bytes=8_000_000_000,
            profiler_config_version=cfg.profiler_config_version,
            profiler_config_hash=cfg.config_hash,
        )
        b_san = svc.profile(req_san)
        result["truth_isolation"] = {
            "sanitized_only_files": sorted(p.name for p in san.iterdir()),
            "fingerprint_equal": b_san.fingerprint.fingerprint_hash == fps[0],
            "content_hash_equal": _content_hash(b_san.profile) == chs[0],
            "families_equal": tuple(b_san.profile.completion.completed_families) == fams[0],
            "warnings_equal": tuple(b_san.profile.warnings) == warns[0],
        }
    finally:
        for p in san.iterdir():
            p.unlink()
        san.rmdir()

    # --- cross-region / boundary (Phase 7) ---
    span = e0 - s0
    sub = 200_000
    subregions = {
        "official_region": (s0, e0),
        "start_boundary": (s0, min(s0 + sub, e0)),
        "end_boundary": (max(e0 - sub, s0), e0),
        "high_cov_subregion": (s0 + span // 2, min(s0 + span // 2 + sub, e0)),
        "low_cov_subregion": (s0, min(s0 + sub, e0)),  # dataset edges are lower coverage
    }
    cross = {}
    for name, (a, c) in subregions.items():
        if name == "official_region":
            cross[name] = {
                "ok": prof1.status.value in ("COMPLETE", "PARTIAL"),
                "length_bp": e0 - s0,
                "processed_length_bp": prof1.region.length_bp,
            }
            continue
        b = svc.profile(_req(rid, contig, a, c, cfg))
        # verify NaN/Inf-free serialization + exact processed length
        canonical_json_bytes(b.profile.model_dump(mode="json"))
        cross[name] = {
            "ok": b.profile.status.value in ("COMPLETE", "PARTIAL"),
            "length_bp": c - a,
            "processed_length_bp": b.profile.region.length_bp,
            "length_match": b.profile.region.length_bp == c - a,
        }
    result["cross_region"] = cross

    result["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    return result


def run_robustness(chrom: str, rid: str, s0: int, e0: int) -> dict[str, Any]:
    from minos_engine.common.errors import ContractValidationError, MinosEngineError
    from minos_engine.layer1.adapters.pysam_adapter import PysamAdapter
    from minos_engine.layer1.validation import Layer1InputError, validate_inputs

    contig = chrom
    bam, bai, ref, fai = _paths(rid, contig)
    ad = PysamAdapter()
    cases: dict[str, dict[str, Any]] = {}

    def _expect_fail(name: str, **kw: str) -> None:
        try:
            vi = validate_inputs(adapter=ad, region_convention="zero_based_half_open", **kw)  # type: ignore[arg-type]
            vi.alignment.close()
            vi.fasta.close()
            cases[name] = {"failed_closed": False, "note": "unexpectedly succeeded"}
        except (Layer1InputError, ContractValidationError, MinosEngineError) as exc:
            cases[name] = {"failed_closed": True, "error_type": type(exc).__name__}

    _expect_fail(
        "missing_bai",
        bam_path=bam,
        bai_path=bam + ".nope.bai",
        reference_path=ref,
        fai_path=fai,
        region_source=f"{contig}:{s0}-{s0 + 100000}",
    )
    # a reference for a DIFFERENT chromosome than this dataset's contig
    wrong_contig = "chr18" if contig != "chr18" else "chr19"
    _expect_fail(
        "wrong_reference_contig",
        bam_path=bam,
        bai_path=bai,
        reference_path=f"{DS}/reference/{wrong_contig}/{wrong_contig}.fa",
        fai_path=f"{DS}/reference/{wrong_contig}/{wrong_contig}.fa.fai",
        region_source=f"{contig}:{s0}-{s0 + 100000}",
    )
    _expect_fail(
        "unknown_contig",
        bam_path=bam,
        bai_path=bai,
        reference_path=ref,
        fai_path=fai,
        region_source="chrZ:1-1000",
    )
    _expect_fail(
        "out_of_range_region",
        bam_path=bam,
        bai_path=bai,
        reference_path=ref,
        fai_path=fai,
        region_source=f"{contig}:1-999999999",
    )
    _expect_fail(
        "empty_region",
        bam_path=bam,
        bai_path=bai,
        reference_path=ref,
        fai_path=fai,
        region_source=f"{contig}:{s0}-{s0}",
    )
    _expect_fail(
        "missing_reference",
        bam_path=bam,
        bai_path=bai,
        reference_path=ref + ".nope",
        fai_path=fai,
        region_source=f"{contig}:{s0}-{s0 + 100000}",
    )

    # restrictive deadline -> degraded, schema-valid, NaN/Inf-free
    cfg = load_layer1_config()

    class _Fast:
        def __init__(self) -> None:
            self._t = 0.0

        def monotonic(self) -> float:
            self._t += 40.0
            return self._t

    b = Layer1Service(clock=_Fast(), require_prerequisite=False).profile(
        _req(rid, contig, s0, e0, cfg)
    )
    canonical_json_bytes(b.profile.model_dump(mode="json"))
    cases["restrictive_deadline"] = {
        "status": b.profile.status.value,
        "degraded_or_partial": b.profile.status.value in ("PARTIAL", "FAILED"),
        "schema_valid_no_nan": True,
    }
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrom", required=True)
    ap.add_argument("--rid", required=True)
    ap.add_argument("--start0", type=int, required=True)
    ap.add_argument("--end0", type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = run_dataset(a.chrom, a.rid, a.start0, a.end0)
    res["robustness"] = run_robustness(a.chrom, a.rid, a.start0, a.end0)
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    n = res["numerical"]
    print(
        f"{a.chrom} {a.rid}: exact_mismatch={n['exact_mismatches']} float_fail={n['float_failures']} "
        f"det_fp_equal={res['determinism']['fingerprint_equal']} "
        f"iso_equal={res['truth_isolation']['content_hash_equal']} "
        f"enrich={res['truth_relevance']['enrichment']:.2f} "
        f"peak_rss={res['peak_rss_mb']}MB -> {a.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
