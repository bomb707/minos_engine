"""Per-dataset v2 acceptance worker: complete-field numerical validation + emitted-
feature truth relevance + BAM-intrinsic observability + reconciliation + isolation +
3-run determinism + corpus robustness. Subprocess-isolated per chromosome.
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

import pyarrow.parquet as pq
from scripts.qualification import (
    classify_v2 as CL,
)
from scripts.qualification import (
    emitted_features,
    oracle_v2,
    reconcile,
    truth_relevance,
)

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest
from minos_engine.layer1.service import Layer1Service

DS = "/home/hr/bittensor/minos_subnet/datasets"
_VOL = {"stage_timings", "runtime_complexity", "degradation"}


def _sha(path: str) -> str:
    import hashlib

    d = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            d.update(b)
    return d.hexdigest()


def _content_hash(pdict: dict[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes({k: v for k, v in pdict.items() if k not in _VOL}))


def _req(rid: str, contig: str, s0: int, e0: int, cfg: Any, out: Path) -> ProfileRequest:
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
        budget_seconds=300,
        cpu_limit=2,
        memory_limit_bytes=8_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )


def run(chrom: str, rid: str, s0: int, e0: int) -> dict[str, Any]:  # noqa: C901
    cfg = load_layer1_config()
    contig = chrom
    bam = f"{DS}/practice/round_{rid}/input.bam"
    bai = bam + ".bai"
    ref = f"{DS}/reference/{contig}/{contig}.fa"
    truth = f"{DS}/practice/round_{rid}/truth.vcf.gz"
    mut = f"{DS}/practice/round_{rid}/mutations.vcf.gz"
    svc = Layer1Service(require_prerequisite=False)
    res: dict[str, Any] = {
        "chromosome": chrom,
        "dataset_id": rid,
        "region": {"contig": contig, "start0": s0, "end0": e0, "length_bp": e0 - s0},
    }
    res["input_hashes"] = {
        "bam_sha256": _sha(bam),
        "bam_size_bytes": os.path.getsize(bam),
        "bai_sha256": _sha(bai),
        "reference_sha256": _sha(ref),
        "truth_vcf_sha256": _sha(truth),
        "mutations_sha256": _sha(mut),
    }

    # 3-run determinism; keep run-1 artifacts
    fps, chs, fams, warns, times = [], [], [], [], []
    outdir = Path(tempfile.mkdtemp(prefix="l1v2_"))
    win_parquet = None
    prof_dict = None
    for i in range(3):
        t = time.monotonic()
        d = outdir / f"r{i}"
        svc.analyze(_req(rid, contig, s0, e0, cfg, d), d)
        times.append(time.monotonic() - t)
        pj = json.loads((d / "bam-profile-v1.json").read_text())
        fps.append(json.loads((d / "profile-manifest-v1.json").read_text())["fingerprint_hash"])
        chs.append(_content_hash(pj))
        fams.append(tuple(pj["completion"]["completed_families"]))
        warns.append(tuple(pj["warnings"]))
        if i == 0:
            prof_dict = pj
            win_parquet = str(d / "window-profile-v1.parquet")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    res["determinism"] = {
        "runs": 3,
        "fingerprint_equal": len(set(fps)) == 1,
        "content_hash_equal": len(set(chs)) == 1,
        "families_equal": len(set(fams)) == 1,
        "warnings_equal": len(set(warns)) == 1,
        "elapsed_seconds": [round(x, 2) for x in times],
        "peak_rss_mb": round(peak, 1),
        "status": prof_dict["status"],
    }

    # oracle (global) + variant_evidence over the EXACT sampled windows
    ovals = oracle_v2.compute(bam, bai, ref, contig, s0, e0)
    tbl = pq.read_table(win_parquet).to_pylist()
    sampled_windows = [(r["start0"], r["end0"]) for r in tbl if r["sampled"]]
    ve = oracle_v2.variant_evidence_over_windows(
        bam, bai, ref, contig, sampled_windows, region_len=e0 - s0
    )
    ovals.update(ve)

    # per-field records
    recs = CL.build_records(prof_dict, ovals)
    sp = prof_dict["spatial"]
    n_sampled_rows = sum(1 for r in tbl if r["sampled"])
    analyzed_expected = sum(r["end0"] - r["start0"] for r in tbl if r["sampled"])
    spatial_expected = {
        "spatial.primary_window_count": len(tbl),
        "spatial.sampled_window_count": n_sampled_rows,
        "spatial.analyzed_bases": analyzed_expected,
    }
    CL.finalize_records(recs, prof_dict, spatial_expected)

    # spatial invariants (explicit)
    spatial_inv = {
        "primary_window_count_matches_rows": sp["primary_window_count"] == len(tbl),
        "sampled_window_count_matches_rows": sp["sampled_window_count"] == n_sampled_rows,
        "analyzed_bases_matches_sampled": sp["analyzed_bases"] == analyzed_expected,
        "refined_le_primary": sp["refined_window_count"] <= sp["primary_window_count"],
        "sampled_le_refined": sp["sampled_window_count"] <= sp["refined_window_count"],
    }
    res["spatial_invariants"] = spatial_inv

    # category tally
    cats: dict[str, dict[str, int]] = {}
    unvalidated_l2: list[str] = []
    for rec in recs:
        c = cats.setdefault(
            rec.classification, {"total": 0, "pass": 0, "fail": 0, "not_tested": 0, "excluded": 0}
        )
        c["total"] += 1
        if rec.status == "PASS":
            c["pass"] += 1
        elif rec.status == "FAIL":
            c["fail"] += 1
        elif rec.status == "EXCLUDED":
            c["excluded"] += 1
        else:
            c["not_tested"] += 1
        if rec.l2_eligible and rec.status in ("NOT_TESTED",):
            unvalidated_l2.append(rec.path)
    res["field_records"] = [rec.__dict__ for rec in recs]
    res["category_tally"] = cats
    res["unvalidated_l2_fields"] = unvalidated_l2

    # emitted-feature truth relevance (window-level, actual Layer 1 output)
    em = emitted_features.evaluate(win_parquet, truth, contig)
    res["emitted_feature_truth_relevance"] = {
        "n_windows": em.n_windows,
        "n_sampled": em.n_sampled,
        "spearman_snp_density_vs_truth": em.spearman_snp,
        "spearman_indel_density_vs_truth": em.spearman_indel,
        "auroc_window_has_truth_snp": em.auroc_snp_window,
        "auprc_window_has_truth_snp": em.auprc_snp_window,
        "top_decile_lift_snp": em.top_decile_lift_snp,
        "windows_with_truth": em.windows_with_truth,
        "per_window": em.per_window,
    }

    # BAM-intrinsic observability (secondary, separate)
    tr, _t, _c = truth_relevance.analyze(bam, bai, ref, truth, contig, s0, e0)
    res["bam_intrinsic_observability"] = {
        "evaluable_truth": tr.evaluable_truth,
        "sensitivity_overall": tr.sensitivity_overall,
        "sensitivity_by_class": tr.sensitivity_by_class,
        "background_rate": tr.background_rate,
        "enrichment": tr.enrichment,
        "enrichment_ci95": list(tr.enrichment_ci95),
        "auroc": tr.auroc,
        "auprc": tr.auprc,
        "control_count": tr.control_count,
    }

    # mutation/truth reconciliation
    rc = reconcile.reconcile(mut, truth, contig, s0, e0)
    res["reconciliation"] = rc.__dict__

    # truth isolation: sanitized symlink dir (no truth) vs original (truth present)
    san = Path(tempfile.mkdtemp(prefix="l1v2san_"))
    for src, name in (
        (bam, "input.bam"),
        (bai, "input.bam.bai"),
        (ref, f"{contig}.fa"),
        (ref + ".fai", f"{contig}.fa.fai"),
    ):
        os.symlink(src, san / name)
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
    res["truth_isolation"] = {
        "sanitized_only_files": sorted(p.name for p in san.iterdir()),
        "fingerprint_equal": b_san.fingerprint.fingerprint_hash == fps[0],
        "content_hash_equal": _content_hash(b_san.profile.model_dump(mode="json")) == chs[0],
    }
    res["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrom", required=True)
    ap.add_argument("--rid", required=True)
    ap.add_argument("--start0", type=int, required=True)
    ap.add_argument("--end0", type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = run(a.chrom, a.rid, a.start0, a.end0)
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    cat = res["category_tally"]
    fails = {k: v["fail"] for k, v in cat.items() if v.get("fail")}
    em = res["emitted_feature_truth_relevance"]
    print(
        f"{a.chrom} {a.rid}: unvalidated_l2={len(res['unvalidated_l2_fields'])} "
        f"fails={fails} det={res['determinism']['fingerprint_equal']} "
        f"iso={res['truth_isolation']['content_hash_equal']} "
        f"emit_spearman_snp={em['spearman_snp_density_vs_truth']:.3f} "
        f"emit_auroc={em['auroc_window_has_truth_snp']} -> {a.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
