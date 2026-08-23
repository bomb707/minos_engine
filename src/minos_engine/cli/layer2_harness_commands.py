"""``minos-engine layer2 harness`` CLI — L2-F F7 HARNESS-READY qualification.

  layer2 harness qualify              run the live qualification and write its outputs
  layer2 harness qualify --check      verify an already-committed gate (offline, non-mutating)
  layer2 harness gate require-pass    require integrity, PASS and the complete required-check set

``--check`` and ``require-pass`` run no GATK, open no database and never run Alembic. ``qualify``
performs the live qualification and writes outputs ONLY when explicitly invoked; it accepts no
plan, hashes, result, trust bundle or runner override, and it refuses the operational database.

During F7-A the live path is deliberately unreachable without a provisioned official GATK
environment, and no ``gates/harness-ready.json`` is committed: the gate and its evidence belong to
the later F7-B commit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from minos_engine.qualification import l2f_harness_ready_runner as R

_EXIT_OK = 0
_EXIT_FAIL = 3


def _cmd_qualify(args: argparse.Namespace) -> int:
    if args.check:
        return _cmd_check(args)
    # the live qualification requires the provisioned official GATK environment and an isolated
    # scratch database; it never runs against the operational store.
    try:
        R.refuse_operational_database()
    except R.OperationalDatabaseRefused as exc:
        print(json.dumps({"gate": R.HARNESS_READY_GATE, "ok": False, "reasons": [str(exc)]}))
        return _EXIT_FAIL
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    try:
        SubprocessGatkRunner.from_env()
    except Exception as exc:  # noqa: BLE001 - reported as a fail-closed reason
        print(
            json.dumps(
                {
                    "gate": R.HARNESS_READY_GATE,
                    "ok": False,
                    "reasons": [
                        "official GATK qualification environment is unavailable; "
                        "HARNESS-READY cannot be issued",
                        str(exc),
                    ],
                }
            )
        )
        return _EXIT_FAIL
    print(
        json.dumps(
            {
                "gate": R.HARNESS_READY_GATE,
                "ok": False,
                "reasons": [
                    "F7-A ships the qualification framework only; the live qualification run and "
                    "its committed evidence belong to F7-B"
                ],
            }
        )
    )
    return _EXIT_FAIL


def _cmd_check(args: argparse.Namespace) -> int:
    result = R.verify_committed_harness_ready_gate(
        base_dir=Path(args.base_dir).resolve(),
        gate_path=args.gate,
        qualification_path=args.qualification,
    )
    print(
        json.dumps(
            {
                "gate": result["gate_name"],
                "ok": result["ok"],
                "reasons": list(result.get("reasons", ())),
            }
        )
    )
    return _EXIT_OK if result["ok"] else _EXIT_FAIL


def _cmd_require_pass(args: argparse.Namespace) -> int:
    result = R.verify_committed_harness_ready_gate(
        base_dir=Path(args.base_dir).resolve(),
        gate_path=args.gate,
        qualification_path=args.qualification,
    )
    print(
        json.dumps(
            {
                "gate_name": result["gate_name"],
                "ok": result["ok"],
                "reasons": list(result.get("reasons", ())),
            }
        )
    )
    return _EXIT_OK if result["ok"] else _EXIT_FAIL


def add_harness_subparser(l2_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = l2_sub.add_parser("harness", help="Layer 2-F HARNESS-READY qualification (F7)")
    hsub = parser.add_subparsers(dest="harness_command", required=True)

    q = hsub.add_parser("qualify", help="run the live qualification (or --check to verify)")
    q.add_argument("--check", action="store_true", help="verify a committed gate only (offline)")
    q.add_argument("--gate", default=R.HARNESS_READY_GATE_PATH)
    q.add_argument("--qualification", default=None, help="optional canonical qualification result")
    q.add_argument("--base-dir", default=".")
    q.set_defaults(func=_cmd_qualify)

    g = hsub.add_parser("gate", help="committed-gate verification")
    gsub = g.add_subparsers(dest="harness_gate_command", required=True)
    req = gsub.add_parser("require-pass", help="require the committed gate to PASS")
    req.add_argument("--gate", default=R.HARNESS_READY_GATE_PATH)
    req.add_argument("--qualification", default=None)
    req.add_argument("--base-dir", default=".")
    req.set_defaults(func=_cmd_require_pass)
