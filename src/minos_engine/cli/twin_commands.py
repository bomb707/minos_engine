"""Stage 1 Validator Twin CLI commands (thin composition roots).

Commands: ``twin plan`` (build a GATK execution plan; NEVER executes tools),
``twin replay`` (deterministic fixture replay; requires the Stage 0
PROTOCOL-READY gate; clearly labeled as fixture-backed — not a live validator),
``twin parity`` (compare expectation vs observation), ``twin qualify`` (run the
TWIN-READY qualification). Truth isolation: the Twin operates only on offline
comparison results; it never connects to a live validator.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_INPUT_ERROR = 2

_TRUTH_ISOLATION_NOTE = (
    "Fixture-backed only; not a live validator. Truth identities are used solely "
    "in offline comparison inputs and never enter production prediction features."
)


def _emit(obj: Any, as_json: bool, human: str) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True) if as_json else human)


def _cmd_twin_plan(args: argparse.Namespace) -> int:
    from minos_engine.twin.execution_plan import build_execution_plan
    from minos_engine.twin.fixtures import load_replay_fixture

    fixture = load_replay_fixture(args.request)
    plan = build_execution_plan(fixture.request)  # side-effect-free; no execution
    out = {
        "plan_hash": plan.plan_hash,
        "config_hash": plan.config_hash,
        "caller": plan.caller,
        "redacted_command": plan.invocation.redacted_command(),
        "declared_inputs": plan.invocation.declared_inputs,
        "declared_outputs": plan.invocation.declared_outputs,
        "note": "plan only — no tool executed",
    }
    _emit(
        out,
        args.json,
        f"GATK execution plan (no execution)\n"
        f"  plan_hash   : {plan.plan_hash}\n"
        f"  config_hash : {plan.config_hash}\n"
        f"  command     : {plan.invocation.redacted_command()}",
    )
    return EXIT_OK


def _cmd_twin_replay(args: argparse.Namespace) -> int:
    from minos_engine.common.hashing import sha256_hex
    from minos_engine.schema_registry import validate_against
    from minos_engine.twin.fixtures import load_replay_fixture
    from minos_engine.twin.service import TwinService

    fixture = load_replay_fixture(args.fixture)
    fixture_hash = sha256_hex(Path(args.fixture).read_bytes())
    now_iso = datetime.now(UTC).isoformat()
    service = TwinService()
    result = service.replay_fixture(fixture, now_iso=now_iso, fixture_hash=fixture_hash)

    comparison = result.comparison.model_dump(mode="json")
    score = result.score.model_dump(mode="json")
    validate_against("twin-comparison-result-v1", comparison)
    validate_against("twin-score-result-v1", score)

    out = {
        "mode": "FIXTURE_REPLAY",
        "manifest_hash": result.manifest.manifest_hash,
        "plan_hash": result.plan.plan_hash,
        "comparison_hash": result.comparison.content_hash(),
        "scorer_status": result.score.status.value,
        "scorer_reason": result.score.reason_code.value if result.score.reason_code else None,
        "declared_parity_level": result.manifest.declared_parity_level.value,
        "prerequisite_gate_hash": result.manifest.prerequisite_gate_hash,
        "note": _TRUTH_ISOLATION_NOTE,
    }
    _emit(
        out,
        args.json,
        f"Twin fixture replay (FIXTURE_REPLAY)\n"
        f"  manifest_hash : {result.manifest.manifest_hash}\n"
        f"  scorer        : {out['scorer_status']} ({out['scorer_reason']})\n"
        f"  note          : {_TRUTH_ISOLATION_NOTE}",
    )
    return EXIT_OK


def _cmd_twin_parity(args: argparse.Namespace) -> int:
    from minos_engine.schema_registry import validate_against
    from minos_engine.twin.contracts import (
        DECLARED_PARITY_LEVEL,
        ParityExpectation,
        ParityObservation,
    )
    from minos_engine.twin.parity import assess_parity

    expectation = ParityExpectation.model_validate(json.loads(Path(args.expected).read_text()))
    observation = ParityObservation.model_validate(json.loads(Path(args.observed).read_text()))
    report = assess_parity(
        name=args.name,
        expectation=expectation,
        observation=observation,
        declared_level=DECLARED_PARITY_LEVEL,
        created_at=datetime.now(UTC).isoformat(),
    )
    payload = report.model_dump(mode="json")
    validate_against("twin-parity-report-v1", payload)
    _emit(
        payload,
        args.json,
        f"Twin parity: {'MATCH' if report.matched else 'MISMATCH'} "
        f"(level={report.declared_level.value}, {len(report.differences)} differences)",
    )
    return EXIT_OK if report.matched else EXIT_VERIFY_FAILED


def _cmd_twin_qualify(args: argparse.Namespace) -> int:
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.qualification.twin_runner import qualify_twin, write_twin_outputs

    root = Path(args.root).resolve() if args.root else Path.cwd()
    result = qualify_twin(root)
    gate_path, report_path = write_twin_outputs(result, root)
    summary = {
        "status": result.gate.status.value,
        "gate_hash": result.gate.gate_hash,
        "declared_parity_level": result.declared_parity_level.value,
        "prerequisite_gate_hash": result.prerequisite_gate_hash,
        "gate_path": str(gate_path.relative_to(root)),
        "report_path": str(report_path.relative_to(root)),
        "coverage_percent": result.coverage.line_coverage_percent,
        "tests": result.accounting.as_report_row(),
    }
    _emit(summary, args.json, json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK if result.gate.status is GateStatus.PASS else EXIT_VERIFY_FAILED


def add_twin_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_twin = sub.add_parser(
        "twin",
        help="Validator Twin (Stage 1) — fixture-backed, not live",
        description=(
            "Validator Twin (Stage 1). Fixture-backed and deterministic; this is "
            "NOT a live validator connection. Truth isolation: truth identities are "
            "used only in offline comparison inputs and never enter production "
            "prediction features."
        ),
    )
    twin_sub = p_twin.add_subparsers(dest="twin_command", required=True)

    p_plan = twin_sub.add_parser("plan", help="build a GATK execution plan (no execution)")
    p_plan.add_argument("--request", required=True, help="twin replay fixture path")
    p_plan.add_argument("--json", action="store_true")
    p_plan.set_defaults(func=_cmd_twin_plan)

    p_replay = twin_sub.add_parser(
        "replay", help="deterministic fixture replay (not a live validator)"
    )
    p_replay.add_argument("--fixture", required=True)
    p_replay.add_argument("--json", action="store_true")
    p_replay.set_defaults(func=_cmd_twin_replay)

    p_parity = twin_sub.add_parser("parity", help="compare expectation vs observation")
    p_parity.add_argument("--name", default="twin-parity")
    p_parity.add_argument("--expected", required=True)
    p_parity.add_argument("--observed", required=True)
    p_parity.add_argument("--json", action="store_true")
    p_parity.set_defaults(func=_cmd_twin_parity)

    p_qualify = twin_sub.add_parser(
        "qualify", help="run TWIN-READY qualification and write gate + report"
    )
    p_qualify.add_argument("--root", default=None)
    p_qualify.add_argument("--json", action="store_true")
    p_qualify.set_defaults(func=_cmd_twin_qualify)
