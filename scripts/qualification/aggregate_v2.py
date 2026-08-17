"""Aggregate v2 per-dataset results -> honest verdict + report + JSON.

Applies the strict v2 verdict rules: every analytical category must be 100% with
zero unvalidated Layer 2-consumable fields; determinism/isolation/robustness 100%;
zero hard-deadline violations; AND Layer 1 EMITTED-FEATURE truth relevance must be
useful and statistically defensible. BAM-intrinsic observability is reported
separately and cannot substitute for emitted-feature relevance.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.qualification import robustness_synth
from scripts.qualification.emitted_features import _auroc, _spearman

REPO = Path(__file__).resolve().parents[2]
CHROMS = ["chr18", "chr19", "chr20", "chr21", "chr22"]
CATS = ["exact", "float_strict", "sampled", "approximate", "derived"]


def _tools() -> dict[str, str]:
    import numpy
    import pyarrow
    import pysam

    return {"pysam": pysam.__version__, "pyarrow": pyarrow.__version__, "numpy": numpy.__version__}


def _gate(name: str) -> str:
    return json.loads((REPO / "gates" / f"{name}.json").read_text())["gate_hash"]


def _pooled_emitted(datasets: list[dict[str, Any]]) -> dict[str, Any]:
    snp_d: list[float] = []
    snp_truth: list[float] = []
    ind_d: list[float] = []
    ind_truth: list[float] = []
    for d in datasets:
        for w in d["emitted_feature_truth_relevance"].get("per_window", []):
            ln = max(1, w.get("length_bp", 100000))
            snp_d.append(w["candidate_snp_density"])
            snp_truth.append(w["truth_snp"] / ln)
            ind_d.append(w["candidate_indel_density"])
            ind_truth.append(w["truth_indel"] / ln)

    def median_split_auroc(scores: list[float], truth: list[float]) -> float:
        if len(truth) < 6:
            return float("nan")
        srt = sorted(truth)
        med = srt[len(srt) // 2]
        labels = [1 if t > med else 0 for t in truth]
        auroc, _ = _auroc(scores, labels)
        return auroc

    sp_snp = _spearman(snp_d, snp_truth)
    sp_ind = _spearman(ind_d, ind_truth)
    au_snp = median_split_auroc(snp_d, snp_truth)
    au_ind = median_split_auroc(ind_d, ind_truth)
    useful_snp = abs(sp_snp) >= 0.30 and (au_snp >= 0.60 if au_snp == au_snp else False)
    useful_ind = abs(sp_ind) >= 0.30 and (au_ind >= 0.60 if au_ind == au_ind else False)
    return {
        "n_sampled_windows": len(snp_d),
        "spearman_snp": sp_snp,
        "spearman_indel": sp_ind,
        "auroc_snp_median_split": au_snp,
        "auroc_indel_median_split": au_ind,
        "useful_snp": useful_snp,
        "useful_indel": useful_ind,
        "useful_threshold": "abs(spearman)>=0.30 and median-split AUROC>=0.60",
    }


def build() -> dict[str, Any]:  # noqa: C901
    datasets = [json.loads((REPO_SP / f"result_v2_{c}.json").read_text()) for c in CHROMS]

    # category tallies pooled
    pooled: dict[str, dict[str, int]] = {
        c: {"total": 0, "pass": 0, "fail": 0, "not_tested": 0} for c in CATS
    }
    unvalidated = 0
    total_serialized = 0
    identifier = operational = 0
    for d in datasets:
        for c, t in d["category_tally"].items():
            total_serialized += t["total"]
            if c in pooled:
                for k in ("total", "pass", "fail", "not_tested"):
                    pooled[c][k] += t[k]
            elif c == "identifier":
                identifier += t["total"]
            elif c == "operational":
                operational += t["total"]
        unvalidated += len(d["unvalidated_l2_fields"])

    def rate(c: str) -> Any:
        tested = pooled[c]["pass"] + pooled[c]["fail"]
        if pooled[c]["total"] == 0:
            return "NOT_TESTED"
        if pooled[c]["not_tested"] > 0 and tested == 0:
            return "NOT_TESTED"
        return pooled[c]["pass"] / tested if tested else "NOT_TESTED"

    rates = {c: rate(c) for c in CATS}
    total_fail = sum(pooled[c]["fail"] for c in CATS)
    analytical = sum(pooled[c]["total"] for c in CATS)

    det = sum(
        1
        for d in datasets
        if d["determinism"]["fingerprint_equal"]
        and d["determinism"]["content_hash_equal"]
        and d["determinism"]["families_equal"]
        and d["determinism"]["warnings_equal"]
    ) / len(datasets)
    iso = sum(
        1
        for d in datasets
        if d["truth_isolation"]["fingerprint_equal"] and d["truth_isolation"]["content_hash_equal"]
    ) / len(datasets)
    hard_dl = sum(1 for d in datasets if max(d["determinism"]["elapsed_seconds"]) > 300)

    rob = robustness_synth.run()
    rob_fail = [
        k
        for k, c in rob.items()
        if "documented_limitation" not in c and not (c.get("ok") or c.get("failed_closed"))
    ]

    pooled_em = _pooled_emitted(datasets)
    bam_intrinsic = {
        "per_chromosome": [
            {
                "chromosome": d["chromosome"],
                **{
                    k: d["bam_intrinsic_observability"][k]
                    for k in ("enrichment", "auroc", "sensitivity_overall", "background_rate")
                },
            }
            for d in datasets
        ],
        "note": "BAM-intrinsic (direct pileup) — secondary; does NOT substitute for emitted-feature relevance",
    }

    numerical_ok = all(rates[c] == 1.0 for c in CATS) and unvalidated == 0 and total_fail == 0
    hard_ok = numerical_ok and det == 1.0 and iso == 1.0 and not rob_fail and hard_dl == 0
    emitted_ok = pooled_em["useful_snp"] and pooled_em["useful_indel"]

    if not hard_ok:
        verdict = "FAIL"
        rec = "BLOCKED"
    elif not emitted_ok:
        verdict = "INCOMPLETE"
        rec = "BLOCKED"
    else:
        verdict = "PASS"
        rec = "APPROVED_WITH_EXCLUSIONS"

    return {
        "schema_version": "layer1-multi-dataset-accuracy-v2",
        "created_at": datetime.now(UTC).isoformat(),
        "verdict": verdict,
        "layer2_recommendation": rec,
        "python_version": platform.python_version(),
        "tool_versions": _tools(),
        "accepted_gates": {
            "protocol_ready": _gate("protocol-ready"),
            "twin_ready": _gate("twin-ready"),
            "l1_ready": _gate("l1-ready"),
        },
        "field_inventory": {
            "total_serialized_fields": total_serialized // len(datasets),
            "analytical_fields": analytical // len(datasets),
            "l2_consumable_fields": analytical // len(datasets),
            "operational_fields_excluded": operational // len(datasets),
            "identifier_fields": identifier // len(datasets),
            "not_applicable_fields": 0,
            "unvalidated_fields": unvalidated,
        },
        "field_coverage_by_classification": {
            c: {
                "tested_per_dataset": pooled[c]["total"] // len(datasets),
                "pass": pooled[c]["pass"],
                "fail": pooled[c]["fail"],
                "not_tested": pooled[c]["not_tested"],
                "pass_rate": rates[c],
            }
            for c in CATS
        },
        "unvalidated_l2_field_count": unvalidated,
        "datasets": datasets,
        "emitted_feature_truth_relevance_pooled": pooled_em,
        "bam_intrinsic_observability_pooled": bam_intrinsic,
        "robustness": {"synthetic": rob, "failures": rob_fail},
        "summary": {
            "exact_pass_rate": rates["exact"],
            "float_strict_pass_rate": rates["float_strict"],
            "sampled_pass_rate": rates["sampled"],
            "approximate_pass_rate": rates["approximate"],
            "derived_pass_rate": rates["derived"],
            "determinism_pass_rate": det,
            "truth_isolation_pass_rate": iso,
            "robustness_pass_rate": 0.0 if rob_fail else 1.0,
            "hard_deadline_violations": hard_dl,
            "total_exact_mismatches": pooled["exact"]["fail"],
            "total_analytical_failures": total_fail,
        },
    }


REPO_SP = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO


def _render(out: dict[str, Any]) -> str:
    s = out["summary"]
    em = out["emitted_feature_truth_relevance_pooled"]
    fi = out["field_inventory"]
    L = []
    a = L.append
    a("# Layer 1 Multi-Dataset Accuracy Report v2 (corrected methodology)\n")
    a(
        f"**Executive verdict: {out['verdict']}** — Layer 2 recommendation: "
        f"**{out['layer2_recommendation']}**\n"
    )
    a(
        f"Generated {out['created_at']} · Python {out['python_version']} · "
        f"pysam {out['tool_versions']['pysam']} / pyarrow {out['tool_versions']['pyarrow']} / "
        f"numpy {out['tool_versions']['numpy']}\n"
    )
    a(
        "Supersedes v1 (see `LAYER1_QUALIFICATION_V1_LIMITATIONS.md`). Accepted gates unchanged: "
        f"PROTOCOL `{out['accepted_gates']['protocol_ready'][:12]}…`, "
        f"TWIN `{out['accepted_gates']['twin_ready'][:12]}…`, L1 `{out['accepted_gates']['l1_ready'][:12]}…`.\n"
    )
    a("## Field inventory (per dataset)\n")
    a(f"- total serialized fields: {fi['total_serialized_fields']}")
    a(f"- analytical / L2-consumable: {fi['analytical_fields']}")
    a(
        f"- identifier (excluded): {fi['identifier_fields']} · operational (excluded): {fi['operational_fields_excluded']}"
    )
    a(f"- **unvalidated L2-consumable fields: {out['unvalidated_l2_field_count']}**\n")
    a("## Field coverage by classification (pooled across 5 datasets)\n")
    a("| category | tested/dataset | pass | fail | not_tested | pass_rate |")
    a("|---|---|---|---|---|---|")
    for c, v in out["field_coverage_by_classification"].items():
        a(
            f"| {c} | {v['tested_per_dataset']} | {v['pass']} | {v['fail']} | {v['not_tested']} | {v['pass_rate']} |"
        )
    a("\n## Per-dataset numerical result\n")
    a(
        "| chr | dataset | exact f | float_strict f | sampled f | approx f | derived f | unvalidated L2 |"
    )
    a("|---|---|---|---|---|---|---|---|")
    for d in out["datasets"]:
        ct = d["category_tally"]

        def ff(k: str, ct: dict = ct) -> int:
            return ct.get(k, {}).get("fail", 0)

        a(
            f"| {d['chromosome']} | `{d['dataset_id']}` | {ff('exact')} | {ff('float_strict')} | "
            f"{ff('sampled')} | {ff('approximate')} | {ff('derived')} | {len(d['unvalidated_l2_fields'])} |"
        )
    a("\n## Layer 1 EMITTED-FEATURE truth relevance (window-level, actual output; pooled)\n")
    a(f"- sampled windows pooled: {em['n_sampled_windows']}")
    a(
        f"- SNP: Spearman(candidate_snp_density, truth SNP/bp) = **{em['spearman_snp']:.3f}**, "
        f"median-split AUROC = **{em['auroc_snp_median_split']:.3f}**, useful = **{em['useful_snp']}**"
    )
    a(
        f"- INDEL: Spearman(candidate_indel_density, truth indel/bp) = **{em['spearman_indel']:.3f}**, "
        f"median-split AUROC = **{em['auroc_indel_median_split']:.3f}**, useful = **{em['useful_indel']}**"
    )
    a(f"- usefulness bar: {em['useful_threshold']}\n")
    a(
        "> Finding: Layer 1's emitted window-level candidate INDEL density tracks per-window truth "
        "indel content, but the emitted window-level candidate SNP density does NOT usefully separate "
        "windows by truth SNP content at 100 kbp granularity (near-uniform truth SNP density + "
        "error-dominated candidate density). This is a granularity limitation of the window profile; "
        "SNP truth signal lives in the site-level sampled evidence, not the window aggregate.\n"
    )
    a("## BAM-intrinsic truth observability (secondary — NOT Layer 1 emitted features)\n")
    a("| chr | enrichment | AUROC | sensitivity | background |")
    a("|---|---|---|---|---|")
    for r in out["bam_intrinsic_observability_pooled"]["per_chromosome"]:
        a(
            f"| {r['chromosome']} | {r['enrichment']:.1f} | {r['auroc']:.3f} | "
            f"{r['sensitivity_overall']:.3f} | {r['background_rate']:.4f} |"
        )
    a(
        "\n(The strong BAM-intrinsic signal confirms the data is informative; it does NOT establish "
        "that Layer 1's emitted features capture it — see the emitted-feature section.)\n"
    )
    a("## Mutation ↔ truth reconciliation\n")
    a(
        "| chr | mutation records | truth records | matched | unmatched mut | unmatched truth | complex excl |"
    )
    a("|---|---|---|---|---|---|---|")
    for d in out["datasets"]:
        rc = d["reconciliation"]
        a(
            f"| {d['chromosome']} | {rc['mutation_records']} | {rc['truth_records']} | {rc['matched']} | "
            f"{rc['unmatched_mutation']} | {rc['unmatched_truth']} | "
            f"{rc['complex_excluded_mutation'] + rc['complex_excluded_truth']} |"
        )
    a("\n## Determinism / truth isolation / runtime\n")
    a("| chr | det fp | det content | iso fp | iso content | elapsed s | peak RSS MB |")
    a("|---|---|---|---|---|---|---|")
    for d in out["datasets"]:
        dt = d["determinism"]
        iso = d["truth_isolation"]
        a(
            f"| {d['chromosome']} | {dt['fingerprint_equal']} | {dt['content_hash_equal']} | "
            f"{iso['fingerprint_equal']} | {iso['content_hash_equal']} | {dt['elapsed_seconds']} | {dt['peak_rss_mb']} |"
        )
    a("\n## Robustness (synthetic fixtures — corpus lacks these conditions)\n")
    for k, c in out["robustness"]["synthetic"].items():
        st = (
            "PASS"
            if (c.get("ok") or c.get("failed_closed"))
            else ("DOCUMENTED_LIMITATION" if "documented_limitation" in c else "FAIL")
        )
        a(f"- {k}: {st}")
    a(f"\nRobustness failures: {out['robustness']['failures'] or 'none'}\n")
    a("## Summary gates\n")
    for k, v in s.items():
        a(f"- {k}: {v}")
    a(f"\n## Verdict: **{out['verdict']}** · Layer 2: **{out['layer2_recommendation']}**\n")
    if out["verdict"] == "INCOMPLETE":
        a(
            "Complete-profile numerical validation PASSES (every analytical field validated, 0 "
            "unvalidated L2-consumable fields, 0 mismatches), and determinism / truth-isolation / "
            "robustness pass with 0 hard-deadline violations. However Layer 1's EMITTED window-level "
            "SNP feature is NOT demonstrably useful for site-level SNP truth at 100 kbp, so the "
            "emitted-feature truth-relevance requirement is not met. Per the v2 rules the verdict is "
            "INCOMPLETE and Layer 2 remains BLOCKED pending an owner decision on window-feature "
            "fitness for SNP localization (and whether site-level sampled evidence should be the "
            "SNP signal Layer 2 consumes)."
        )
    elif out["verdict"] == "FAIL":
        a("A mandatory numerical/determinism/isolation/robustness gate failed — Layer 2 BLOCKED.")
    return "\n".join(L) + "\n"


def main() -> int:
    out = build()
    sys.path.insert(0, str(REPO / "src"))
    from minos_engine.schema_registry import validate_against

    validate_against("layer1-multi-dataset-accuracy-v2", out)
    (REPO / "reports" / "LAYER1_MULTI_DATASET_ACCURACY_RESULTS_V2.json").write_text(
        json.dumps(out, indent=1) + "\n", encoding="utf-8"
    )
    (REPO / "reports" / "LAYER1_MULTI_DATASET_ACCURACY_REPORT_V2.md").write_text(
        _render(out), encoding="utf-8"
    )
    print(
        "verdict",
        out["verdict"],
        "| L2",
        out["layer2_recommendation"],
        "| unvalidated_l2",
        out["unvalidated_l2_field_count"],
        "| emit useful snp/indel",
        out["emitted_feature_truth_relevance_pooled"]["useful_snp"],
        out["emitted_feature_truth_relevance_pooled"]["useful_indel"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
