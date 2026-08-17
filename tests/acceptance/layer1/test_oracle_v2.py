"""CI-safe tests for the corrected v2 acceptance framework (synthetic only)."""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.qualification import classify_v2 as CL
from scripts.qualification import emitted_features, oracle_v2, reconcile
from tests.layer1_fixtures import ReadSpec, simple_reads, write_bam, write_reference

from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest
from minos_engine.layer1.service import Layer1Service

_FORBIDDEN = (
    "minos_engine.layer1.scan",
    "minos_engine.layer1.coverage",
    "minos_engine.layer1.pileup",
    "minos_engine.layer1.reference_profile",
    "minos_engine.layer1.aggregators",
    "minos_engine.layer1.difficulty",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_v2_oracle_independent_of_production_calc():
    for mod in (oracle_v2, emitted_features, reconcile):
        assert not (_imports(Path(mod.__file__)) & set(_FORBIDDEN))


def test_v2_complete_field_coverage_no_unvalidated_l2(tmp_path):
    import json

    ref = tmp_path / "chr1.fa"
    write_reference(ref, "chr1", "A" * 4000)
    reads = simple_reads(4000, n_pairs=120)
    for i in range(30):  # add alt-bearing reads so variant_evidence is populated
        reads.append(
            ReadSpec(f"alt{i}", 500, [(0, 80)], "C" * 80, [35] * 80, is_paired=False, nm=80)
        )
    write_bam(tmp_path / "input.bam", "chr1", 4000, reads)
    cfg = load_layer1_config()
    req = ProfileRequest(
        round_id="v2",
        bam_path=str(tmp_path / "input.bam"),
        bai_path=str(tmp_path / "input.bam.bai"),
        reference_path=str(ref),
        fai_path=str(ref) + ".fai",
        region_source="chr1:0-4000",
        region_coordinate_convention="zero_based_half_open",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )
    out = tmp_path / "o"
    Layer1Service(require_prerequisite=False).analyze(req, out)
    import pyarrow.parquet as pq

    prof = json.loads((out / "bam-profile-v1.json").read_text())
    ov = oracle_v2.compute(
        str(tmp_path / "input.bam"), str(tmp_path / "input.bam.bai"), str(ref), "chr1", 0, 4000
    )
    tbl = pq.read_table(str(out / "window-profile-v1.parquet")).to_pylist()
    sw = [(r["start0"], r["end0"]) for r in tbl if r["sampled"]]
    ov.update(
        oracle_v2.variant_evidence_over_windows(
            str(tmp_path / "input.bam"),
            str(tmp_path / "input.bam.bai"),
            str(ref),
            "chr1",
            sw,
            region_len=4000,
        )
    )
    recs = CL.build_records(prof, ov)
    n_sampled = sum(1 for r in tbl if r["sampled"])
    analyzed = sum(r["end0"] - r["start0"] for r in tbl if r["sampled"])
    CL.finalize_records(
        recs,
        prof,
        {
            "spatial.primary_window_count": len(tbl),
            "spatial.sampled_window_count": n_sampled,
            "spatial.analyzed_bases": analyzed,
        },
    )
    unvalidated = [r.path for r in recs if r.l2_eligible and r.status == "NOT_TESTED"]
    fails = [r.path for r in recs if r.status == "FAIL"]
    # every serialized leaf classified into exactly one category
    assert all(r.classification for r in recs)
    assert unvalidated == [], unvalidated
    assert fails == [], fails


def test_v2_categories_are_disjoint_and_complete():
    # classification returns a known category for representative paths
    assert CL.classify("filter_counts.observed")[0] == CL.EXACT
    assert CL.classify("reference_context.gc_fraction")[0] == CL.FLOAT_STRICT
    assert CL.classify("variant_evidence.mismatch_fraction")[0] == CL.SAMPLED
    assert CL.classify("coverage.fragment_primary.mean_depth_reads_per_base")[0] == CL.APPROX
    assert CL.classify("mapping_quality.quantiles.P50")[0] == CL.APPROX
    assert CL.classify("difficulty.mapping_risk")[0] == CL.DERIVED
    assert CL.classify("provenance.pysam_version")[0] == CL.IDENTIFIER
    assert CL.classify("stage_timings[]")[0] == CL.OPERATIONAL


def test_spearman_and_auroc_helpers():
    assert emitted_features._spearman([1, 2, 3, 4], [1, 2, 3, 4]) > 0.99
    assert emitted_features._spearman([1, 2, 3, 4], [4, 3, 2, 1]) < -0.99
    auroc, _ = emitted_features._auroc([0.9, 0.8, 0.7], [1, 1, 0])
    assert 0.0 <= auroc <= 1.0


def test_reconcile_minimal_representation():
    assert reconcile._minimal("A", "G") == ("A", "G")
    assert reconcile._minimal("AT", "A") == ("AT", "A")
    assert reconcile._minimal("CAT", "CT") == ("CA", "C")
