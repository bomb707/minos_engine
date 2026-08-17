"""CI-safe tests for the independent acceptance oracle (Commit E framework).

Runs on synthetic pysam fixtures only (no real dataset). Proves the oracle agrees
exactly with production Layer 1 on known-answer inputs, that the oracle imports NO
production Layer 1 calculation module, and that the AUROC/AUPRC + variant-class
helpers are correct on known vectors.
"""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.qualification import compare, oracle, truth_relevance
from tests.layer1_fixtures import build_dataset, simple_reads

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


def test_oracle_does_not_import_production_layer1_calc():
    oracle_path = Path(oracle.__file__)
    tr_path = Path(truth_relevance.__file__)
    for path in (oracle_path, tr_path):
        mods = _imports(path)
        assert not (mods & set(_FORBIDDEN)), f"{path} imports forbidden calc module(s)"


def test_oracle_matches_layer1_exactly_on_synthetic(tmp_path):
    ds = build_dataset(tmp_path, simple_reads(4000, n_pairs=80), contig="chr1", contig_len=4000)
    cfg = load_layer1_config()
    s0, e0 = 0, 4000
    req = ProfileRequest(
        round_id="oracle-test",
        bam_path=str(ds.bam),
        bai_path=str(ds.bai),
        reference_path=str(ds.reference),
        fai_path=str(ds.fai),
        region_source=f"chr1:{s0}-{e0}",
        region_coordinate_convention="zero_based_half_open",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )
    prof = Layer1Service(require_prerequisite=False).profile(req).profile
    orc = oracle.compute(str(ds.bam), str(ds.bai), str(ds.reference), "chr1", s0, e0)
    obs = compare.observed_from_profile(prof)
    results = compare.compare_all(obs, orc.values)
    exact = [r for r in results if r.kind == "exact"]
    fstrict = [r for r in results if r.kind == "float_strict"]
    assert exact, "no exact fields compared"
    assert all(r.ok for r in exact), [r.key for r in exact if not r.ok]
    assert all(r.ok for r in fstrict), [r.key for r in fstrict if not r.ok]
    # spot-check a couple of exact identities
    assert obs["filter.observed"] == orc.values["filter.observed"] == 160


def test_auroc_auprc_known_vectors():
    # perfectly separable: all positives above all negatives -> AUROC 1.0
    auroc, auprc = truth_relevance._auroc_auprc([0.9, 0.8, 0.7], [0.1, 0.2, 0.3])
    assert abs(auroc - 1.0) < 1e-9
    assert auprc > 0.99
    # random-equivalent: identical distributions -> AUROC ~0.5
    auroc2, _ = truth_relevance._auroc_auprc([0.5, 0.5], [0.5, 0.5])
    assert abs(auroc2 - 0.5) < 1e-9


def test_variant_class_and_zygosity_helpers():
    assert truth_relevance._vclass("A", "G") == "snp"
    assert truth_relevance._vclass("A", "AT") == "ins"
    assert truth_relevance._vclass("AT", "A") == "del"


def test_compare_tolerances_flag_failures():
    r_ok = compare.compare_field("ref.gc_fraction", 0.5000000001, 0.5)
    assert r_ok.ok and r_ok.kind == "float_strict"
    r_bad = compare.compare_field("filter.observed", 100, 101)
    assert not r_bad.ok and r_bad.kind == "exact"
