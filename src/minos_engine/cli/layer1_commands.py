"""Layer 1 CLI commands (thin composition roots).

``layer1 validate`` (integrity + region only), ``layer1 profile`` (write the three
artifacts), ``layer1 qualify-real`` (real-BAM two-run qualification → sanitized
integration report), ``layer1 qualify`` (L1-READY qualification, or ``--check``),
and ``layer1 gate require-pass``. The public Appendix-A ``minos-engine profile``
command maps to ``layer1 profile``. No implicit truth access, no automatic network
download, machine-readable JSON, clear nonzero exits, and no secrets in output.
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from minos_engine.layer1.config import Layer1Config
    from minos_engine.layer1.contracts import ProfileRequest

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_INPUT_ERROR = 2
EXIT_INTERNAL = 3


def _emit(obj: Any, as_json: bool, human: str) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True) if as_json else human)


def _config() -> Layer1Config:
    from minos_engine.layer1.config import load_layer1_config

    return load_layer1_config()


def _request(args: argparse.Namespace) -> ProfileRequest:
    from minos_engine.layer1.contracts import ProfileRequest

    cfg = _config()
    return ProfileRequest(
        round_id=args.round_id,
        bam_path=args.bam,
        bai_path=args.bai or (args.bam + ".bai"),
        reference_path=args.reference,
        fai_path=args.fai or (args.reference + ".fai"),
        region_source=args.region,
        region_coordinate_convention=args.coordinate_system,
        budget_seconds=float(args.budget_seconds),
        cpu_limit=int(args.cpu_limit),
        memory_limit_bytes=int(args.memory_limit_bytes),
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )


def _cmd_layer1_validate(args: argparse.Namespace) -> int:
    from minos_engine.layer1.adapters.pysam_adapter import PysamAdapter
    from minos_engine.layer1.validation import Layer1InputError, validate_inputs

    try:
        inputs = validate_inputs(
            bam_path=args.bam,
            bai_path=args.bai or (args.bam + ".bai"),
            reference_path=args.reference,
            fai_path=args.fai or (args.reference + ".fai"),
            region_source=args.region,
            region_convention=args.coordinate_system,
            adapter=PysamAdapter(),
        )
    except Layer1InputError as exc:
        _emit({"ok": False, "error": str(exc)}, args.json, f"INVALID: {exc}")
        return EXIT_INPUT_ERROR
    out = {
        "ok": True,
        "region": inputs.region.model_dump(mode="json"),
        "coordinate_sorted": inputs.header.coordinate_sorted,
        "sample_names": list(inputs.header.sample_names),
        "verification_strength": inputs.identity.verification_strength.value,
        "bam_sha256": inputs.identity.bam_sha256,
        "warnings": list(inputs.warnings),
    }
    inputs.alignment.close()
    inputs.fasta.close()
    _emit(
        out,
        args.json,
        f"OK region={inputs.region.contig}:{inputs.region.start0}-{inputs.region.end0_exclusive}",
    )
    return EXIT_OK


def _cmd_layer1_profile(args: argparse.Namespace) -> int:
    from minos_engine.layer1.service import Layer1Service

    svc = Layer1Service(require_prerequisite=not args.skip_prerequisite)
    result = svc.analyze(_request(args), args.output_dir)
    out = result.model_dump(mode="json")
    _emit(out, args.json, f"{result.status.value} -> {result.profile_path}")
    if result.status.value == "FAILED":
        return (
            EXIT_INPUT_ERROR if result.failure_code == "INPUT_VALIDATION_FAILED" else EXIT_INTERNAL
        )
    return EXIT_OK


def _cmd_layer1_qualify_real(args: argparse.Namespace) -> int:
    from minos_engine.layer1.adapters.pysam_adapter import PysamAdapter
    from minos_engine.layer1.integration import IntegrationReport
    from minos_engine.layer1.service import Layer1Service

    cfg = _config()
    adapter = PysamAdapter()
    bam = args.bam
    bai = args.bai or (args.bam + ".bai")
    reference = args.reference
    fai = args.fai or (args.reference + ".fai")

    bam_sha, bam_size = adapter.stream_sha256(bam)
    bai_sha, _ = adapter.stream_sha256(bai)
    ref_sha, _ = adapter.stream_sha256(reference)
    fai_sha, _ = adapter.stream_sha256(fai)

    svc = Layer1Service(require_prerequisite=not args.skip_prerequisite)
    req = _request(args)

    runs = []
    fingerprints = []
    for _ in range(2):
        t = time.monotonic()
        bundle = svc.profile(req)
        elapsed = time.monotonic() - t
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        runs.append((elapsed, rss_mb))
        fingerprints.append(bundle.fingerprint.fingerprint_hash)

    hard = cfg.budget.hard_seconds
    p = bundle.profile
    report = IntegrationReport(
        dataset_id=args.dataset_id,
        bam_sha256=bam_sha,
        bam_size_bytes=bam_size,
        bai_sha256=bai_sha,
        reference_sha256=ref_sha,
        fai_sha256=fai_sha,
        region_source=req.region_source,
        region_contig=p.region.contig,
        region_start0=p.region.start0,
        region_end0=p.region.end0_exclusive,
        profiler_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
        profile_schema_hash=p.provenance.schema_version,
        fingerprint_hash=fingerprints[0],
        first_run_elapsed_seconds=runs[0][0],
        first_run_peak_rss_mb=runs[0][1],
        second_run_elapsed_seconds=runs[1][0],
        second_run_peak_rss_mb=runs[1][1],
        repeat_run_fingerprint_equal=(fingerprints[0] == fingerprints[1]),
        degradation_status=p.status.value,
        pileup_mode=p.runtime_complexity.chosen_pileup_mode.value,
        completed_families=p.completion.completed_families,
        warnings=p.warnings,
        hard_limit_seconds=hard,
        hard_limit_met=(max(runs[0][0], runs[1][0]) <= hard),
        real_bam_qualified=(
            fingerprints[0] == fingerprints[1] and max(runs[0][0], runs[1][0]) <= hard
        ),
    )
    payload = report.model_dump(mode="json")
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _emit(
        payload,
        args.json,
        f"real_bam_qualified={report.real_bam_qualified} "
        f"run1={runs[0][0]:.1f}s run2={runs[1][0]:.1f}s hard={hard}s -> {out_path}",
    )
    return EXIT_OK if report.real_bam_qualified else EXIT_VERIFY_FAILED


def _cmd_layer1_qualify(args: argparse.Namespace) -> int:
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.qualification.layer1_runner import (
        qualify_layer1,
        verify_l1_ready_gate,
        write_layer1_outputs,
    )

    root = Path(args.root).resolve() if args.root else Path.cwd()
    if args.check:
        base = Path(args.base_dir).resolve() if args.base_dir else root
        gate = Path(args.gate) if args.gate else (base / "gates" / "l1-ready.json")
        result = verify_l1_ready_gate(base, gate, require_descends=not args.no_descends)
        out = result.model_dump(mode="json")
        _emit(out, args.json, json.dumps(out, indent=2, sort_keys=True))
        return EXIT_OK if result.ok else EXIT_VERIFY_FAILED

    qresult = qualify_layer1(root)
    gate_path, report_path = write_layer1_outputs(qresult, root)
    summary = {
        "status": qresult.gate.status.value,
        "gate_hash": qresult.gate.gate_hash,
        "real_bam_qualified": qresult.real_bam_qualified,
        "gate_path": str(gate_path.relative_to(root)),
        "report_path": str(report_path.relative_to(root)),
        "coverage_percent": qresult.coverage.line_coverage_percent,
        "tests": qresult.accounting.as_report_row(),
    }
    _emit(summary, args.json, json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK if qresult.gate.status is GateStatus.PASS else EXIT_VERIFY_FAILED


def _cmd_layer1_gate_require_pass(args: argparse.Namespace) -> int:
    from minos_engine.qualification.layer1_runner import verify_l1_ready_gate

    base = Path(args.base_dir).resolve() if args.base_dir else Path.cwd()
    result = verify_l1_ready_gate(base, Path(args.gate), require_descends=False)
    out = result.model_dump(mode="json")
    _emit(out, args.json, json.dumps(out, indent=2, sort_keys=True))
    return EXIT_OK if result.ok else EXIT_VERIFY_FAILED


def _add_input_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bam", required=True)
    p.add_argument("--bai", default=None)
    p.add_argument("--reference", required=True)
    p.add_argument("--fai", default=None)
    p.add_argument("--region", required=True)
    p.add_argument("--coordinate-system", default="one_based_inclusive")
    p.add_argument("--round-id", default="round")
    p.add_argument("--budget-seconds", default=300)
    p.add_argument("--cpu-limit", default=2)
    p.add_argument("--memory-limit-bytes", default=8_000_000_000)


def add_layer1_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_layer1 = sub.add_parser(
        "layer1",
        help="Layer 1 — truth-free BAM/reference profiling",
        description=(
            "Layer 1 truth-free profiling. Reads only the explicit BAM/BAI/FASTA/FAI "
            "paths given; never enumerates a round directory or reads truth data."
        ),
    )
    l1 = p_layer1.add_subparsers(dest="layer1_command", required=True)

    p_val = l1.add_parser("validate", help="validate BAM/BAI/reference/region integrity")
    _add_input_args(p_val)
    p_val.add_argument("--json", action="store_true")
    p_val.set_defaults(func=_cmd_layer1_validate)

    p_prof = l1.add_parser("profile", help="profile and write the three artifacts")
    _add_input_args(p_prof)
    p_prof.add_argument("--output-dir", required=True, dest="output_dir")
    p_prof.add_argument("--skip-prerequisite", action="store_true")
    p_prof.add_argument("--json", action="store_true")
    p_prof.set_defaults(func=_cmd_layer1_profile)

    p_real = l1.add_parser(
        "qualify-real", help="real-BAM two-run qualification -> integration report"
    )
    _add_input_args(p_real)
    p_real.add_argument("--dataset-id", required=True, help="sanitized dataset identifier")
    p_real.add_argument("--output", default="reports/LAYER1_REAL_BAM_REPORT.json")
    p_real.add_argument("--skip-prerequisite", action="store_true")
    p_real.add_argument("--json", action="store_true")
    p_real.set_defaults(func=_cmd_layer1_qualify_real)

    p_qual = l1.add_parser("qualify", help="run L1-READY qualification (or --check to verify only)")
    p_qual.add_argument("--root", default=None)
    p_qual.add_argument("--check", action="store_true")
    p_qual.add_argument("--gate", default=None)
    p_qual.add_argument("--base-dir", default=None)
    p_qual.add_argument("--no-descends", action="store_true")
    p_qual.add_argument("--json", action="store_true")
    p_qual.set_defaults(func=_cmd_layer1_qualify)

    p_gate = l1.add_parser("gate", help="L1-READY gate verification")
    gate_sub = p_gate.add_subparsers(dest="layer1_gate_command", required=True)
    p_req = gate_sub.add_parser("require-pass", help="verify a committed L1-READY gate")
    p_req.add_argument("--gate", required=True)
    p_req.add_argument("--base-dir", default=None)
    p_req.add_argument("--json", action="store_true")
    p_req.set_defaults(func=_cmd_layer1_gate_require_pass)


def add_profile_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Public Appendix-A `minos-engine profile` command (maps to layer1 profile)."""
    p = sub.add_parser("profile", help="Layer 1 profile (Appendix A public command)")
    _add_input_args(p)
    p.add_argument("--output-dir", required=True, dest="output_dir")
    p.add_argument("--skip-prerequisite", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_layer1_profile)
