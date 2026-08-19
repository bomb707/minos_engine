"""``minos-engine`` command-line entry point (thin composition root).

Subcommands:
  doctor                       environment + readiness report
  protocol snapshot            build a RoundProtocolSnapshot from a fixture
  config validate              validate + canonicalize a GATK CONFIG
  manifest build               build a release manifest from a snapshot fixture
  gate verify                  verify a stage-gate artifact

Every command supports ``--json`` for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from minos_engine import __version__
from minos_engine.common.errors import MinosEngineError

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_INPUT_ERROR = 2
EXIT_INTERNAL = 3


def _print(obj: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(human if human is not None else json.dumps(obj, indent=2, sort_keys=True))


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import build_doctor_report

    report = build_doctor_report()
    if args.json:
        _print(report, True)
    else:
        lines = [
            f"MINOS_ENGINE doctor — stage {report['engine']['stage']} v{report['engine']['package_version']}",
            f"  python           : {report['engine']['python_version']}",
            f"  runtime policy   : {report['runtime']['supported']} "
            f"(current supported: {report['runtime']['is_supported']})",
            f"  git sha          : {report['engine']['git_sha'] or '(unavailable)'}",
            f"  active caller    : {report['caller']['active']} (gatk-only: {report['caller']['gatk_only_policy']})",
            f"  gatk registry    : {report['gatk_registry']['parameter_count']}/{report['gatk_registry']['expected']} params",
            f"  schemas          : {report['schemas']['count']} available",
            f"  upstream ident   : {report['provenance']['upstream_minos_identity_status']}",
            f"  scorer ident     : {report['provenance']['scorer_identity_status']}",
            f"  reference reg    : {report['reference_registry']['status']}",
            f"  layer 1          : implemented ({report['layer1']['profiler_version']})",
            f"  l1-ready gate    : {report['stage_gates']['l1_ready_gate_present']}",
            f"  layer 2 blocked  : {report['stage_gates']['layer2_blocked']}",
            f"  overall health   : {report['overall_health']}",
        ]
        _print(report, False, "\n".join(lines))
    return EXIT_OK


def _cmd_protocol_snapshot(args: argparse.Namespace) -> int:
    from .snapshot import snapshot_from_fixture

    snap = snapshot_from_fixture(args.fixture)
    if args.json:
        _print(snap, True)
    else:
        human = (
            f"snapshot_id        : {snap['snapshot_id']}\n"
            f"round_id           : {snap['round_id']} ({snap['round_status']})\n"
            f"region             : {snap['exact_region']['contig']}:"
            f"{snap['exact_region']['start0']}-{snap['exact_region']['end0_exclusive']} (0-based half-open)\n"
            f"parameter_space    : {snap['parameter_space_hash']}\n"
            f"scorer_hash        : {snap['scorer_hash']}\n"
            f"upstream_commit    : {snap['minos_upstream_commit']}\n"
            f"stale              : {snap['stale']}"
        )
        _print(snap, False, human)
    return EXIT_OK


def _cmd_config_validate(args: argparse.Namespace) -> int:
    from minos_engine.callers.gatk.config import canonicalize_config
    from minos_engine.protocol.parameter_ranges import parse_parameter_space

    with open(args.config, encoding="utf-8") as fh:
        requested = json.load(fh)

    parameter_space = None
    if args.parameter_space:
        with open(args.parameter_space, encoding="utf-8") as fh:
            ps_raw = json.load(fh)
        parameter_space = parse_parameter_space(
            ps_raw,
            retrieved_at=ps_raw.get("retrieved_at", "1970-01-01T00:00:00+00:00"),
            stale=bool(ps_raw.get("stale", False)),
        )

    result = canonicalize_config(requested, parameter_space=parameter_space)
    out = result.model_dump(mode="json")
    if args.json:
        _print(out, True)
    else:
        _print(
            out,
            False,
            f"CONFIG valid.\n  config_hash        : {result.config_hash}\n"
            f"  effective params   : {len(result.effective_config)}\n"
            f"  parameter_space    : {result.parameter_space_hash or '(documented defaults)'}",
        )
    return EXIT_OK


def _cmd_manifest_build(args: argparse.Namespace) -> int:
    from minos_engine.manifests.builder import build_release_manifest
    from minos_engine.protocol.client import FixtureProtocolClient

    if not args.fixture:
        raise MinosEngineError(
            "manifest build requires --fixture (a protocol snapshot supplies the "
            "required upstream/scorer/parameter-space identities)"
        )
    snapshot = FixtureProtocolClient(args.fixture).load_snapshot()
    created_at = args.created_at or datetime.now(UTC).isoformat()
    manifest = build_release_manifest(snapshot, created_at=created_at)
    out = manifest.model_dump(mode="json")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    _print(out, args.json, json.dumps(out, indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_git_history_check(args: argparse.Namespace) -> int:
    from pathlib import Path

    from minos_engine.qualification.git_history import check_repository_history

    base = Path(args.base_dir).resolve() if args.base_dir else Path.cwd()
    result = check_repository_history(
        base,
        protocol_gate=Path(args.protocol_gate),
        twin_gate=Path(args.twin_gate),
    )
    out = result.model_dump(mode="json")
    _print(out, args.json, json.dumps(out, indent=2, sort_keys=True))
    return EXIT_OK if result.ok else EXIT_VERIFY_FAILED


def _cmd_gate_verify_integrity(args: argparse.Namespace) -> int:
    from minos_engine.gates.verifier import verify_gate_file

    result = verify_gate_file(args.gate, base_dir=args.base_dir)
    out = result.model_dump(mode="json")
    _print(out, args.json, json.dumps(out, indent=2, sort_keys=True))
    return EXIT_OK if result.ok else EXIT_VERIFY_FAILED


def _cmd_gate_require_pass(args: argparse.Namespace) -> int:
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.verifier import GateVerification, load_gate, require_gate_pass

    try:
        gate = load_gate(args.gate)
    except Exception as exc:  # noqa: BLE001 - a missing/invalid gate cannot be promoted
        result = GateVerification(
            ok=False,
            gate_name="<unloadable>",
            status=GateStatus.REJECT,
            gate_hash="",
            mode="promotion",
            reasons=(str(exc),),
        )
    else:
        result = require_gate_pass(gate, base_dir=args.base_dir)
    out = result.model_dump(mode="json")
    _print(out, args.json, json.dumps(out, indent=2, sort_keys=True))
    return EXIT_OK if result.ok else EXIT_VERIFY_FAILED


def _cmd_qualify(args: argparse.Namespace) -> int:  # pragma: no cover - subprocess orchestration
    from pathlib import Path

    from minos_engine.gates.contracts import GateStatus
    from minos_engine.qualification.runner import qualify, write_outputs

    root = Path(args.root).resolve() if args.root else Path.cwd()
    result = qualify(root)
    gate_path, report_path = write_outputs(result, root)
    summary = {
        "status": result.gate.status.value,
        "gate_hash": result.gate.gate_hash,
        "gate_path": str(gate_path.relative_to(root)),
        "report_path": str(report_path.relative_to(root)),
        "tests": result.accounting.as_report_row(),
        "coverage_percent": result.coverage.line_coverage_percent,
    }
    _print(summary, args.json, json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK if result.gate.status is GateStatus.PASS else EXIT_VERIFY_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minos-engine", description="MINOS_ENGINE Stage 0 CLI")
    parser.add_argument("--version", action="version", version=f"minos-engine {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="environment and readiness report")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_protocol = sub.add_parser("protocol", help="protocol operations")
    protocol_sub = p_protocol.add_subparsers(dest="protocol_command", required=True)
    p_snap = protocol_sub.add_parser("snapshot", help="build a snapshot from a fixture")
    p_snap.add_argument("--fixture", required=True)
    p_snap.add_argument("--json", action="store_true")
    p_snap.set_defaults(func=_cmd_protocol_snapshot)

    p_config = sub.add_parser("config", help="CONFIG operations")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_validate = config_sub.add_parser("validate", help="validate and canonicalize a GATK CONFIG")
    p_validate.add_argument("--config", required=True)
    p_validate.add_argument("--parameter-space", default=None)
    p_validate.add_argument("--json", action="store_true")
    p_validate.set_defaults(func=_cmd_config_validate)

    p_manifest = sub.add_parser("manifest", help="release manifest operations")
    manifest_sub = p_manifest.add_subparsers(dest="manifest_command", required=True)
    p_build = manifest_sub.add_parser("build", help="build a release manifest")
    p_build.add_argument("--fixture", default=None, help="protocol snapshot fixture")
    p_build.add_argument("--created-at", default=None)
    p_build.add_argument("--output", default=None)
    p_build.add_argument("--json", action="store_true")
    p_build.set_defaults(func=_cmd_manifest_build)

    p_gate = sub.add_parser("gate", help="stage-gate operations")
    gate_sub = p_gate.add_subparsers(dest="gate_command", required=True)
    # `verify` is an ALIAS for integrity verification (structural soundness only;
    # a valid HOLD/REJECT passes). Use `require-pass` to authorize promotion.
    for name, helptext in (
        ("verify", "integrity verification (alias of verify-integrity)"),
        ("verify-integrity", "verify schema, canonical hash, and evidence hashes"),
    ):
        p = gate_sub.add_parser(name, help=helptext)
        p.add_argument("--gate", required=True)
        p.add_argument("--base-dir", default=None, help="root for re-hashing evidence")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=_cmd_gate_verify_integrity)
    p_require = gate_sub.add_parser(
        "require-pass", help="require integrity AND status PASS AND required checks"
    )
    p_require.add_argument("--gate", required=True)
    p_require.add_argument("--base-dir", default=None, help="root for re-hashing evidence")
    p_require.add_argument("--json", action="store_true")
    p_require.set_defaults(func=_cmd_gate_require_pass)

    p_qualify = sub.add_parser("qualify", help="run Stage 0 qualification and write gate + report")
    p_qualify.add_argument("--root", default=None, help="repository root (default: cwd)")
    p_qualify.add_argument("--json", action="store_true")
    p_qualify.set_defaults(func=_cmd_qualify)

    p_history = sub.add_parser("git-history", help="git-history preflight for committed gates")
    history_sub = p_history.add_subparsers(dest="history_command", required=True)
    p_hcheck = history_sub.add_parser(
        "check", help="verify qualified commits/trees/evidence objects exist locally"
    )
    p_hcheck.add_argument("--protocol-gate", default="gates/protocol-ready.json")
    p_hcheck.add_argument("--twin-gate", default="gates/twin-ready.json")
    p_hcheck.add_argument("--base-dir", default=None)
    p_hcheck.add_argument("--json", action="store_true")
    p_hcheck.set_defaults(func=_cmd_git_history_check)

    from .twin_commands import add_twin_subparser

    add_twin_subparser(sub)

    from .layer1_commands import add_layer1_subparser, add_profile_command

    add_layer1_subparser(sub)
    add_profile_command(sub)

    from .layer2_db_commands import add_layer2_subparser

    add_layer2_subparser(sub)

    from .intake_commands import register_intake_commands

    register_intake_commands(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except MinosEngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception as exc:  # noqa: BLE001 - top-level guard, report as internal
        print(f"internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
