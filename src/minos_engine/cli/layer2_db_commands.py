"""``minos-engine layer2 db`` CLI — DB-READY qualification and gate verification.

Subcommands:
  layer2 db qualify [--check]        run/verify L2-B DB-READY qualification
  layer2 db gate require-pass        require a committed DB-READY gate to PASS
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1


def _cmd_db_qualify(args: argparse.Namespace) -> int:  # pragma: no cover - subprocess + real PG
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.qualification.layer2_db_runner import (
        qualify_db_ready,
        verify_db_ready_gate,
        write_db_outputs,
    )

    root = Path(args.base_dir).resolve()
    if args.check:
        result = verify_db_ready_gate(root, Path(args.gate), require_descends=not args.no_descends)
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "gate_hash": result.gate_hash,
                    "checks": result.checks,
                    "reasons": list(result.reasons),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK if result.ok else EXIT_VERIFY_FAILED

    qresult = qualify_db_ready(root)
    gate_path, report_path = write_db_outputs(qresult, root)
    print(
        json.dumps(
            {
                "status": qresult.gate.status.value,
                "gate_hash": qresult.gate.gate_hash,
                "gate_path": str(gate_path),
                "report_path": str(report_path),
                "failing": sorted(k for k, v in qresult.mandatory.items() if not v),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK if qresult.gate.status is GateStatus.PASS else EXIT_VERIFY_FAILED


def _cmd_db_require_pass(args: argparse.Namespace) -> int:
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.verifier import load_gate, require_gate_pass

    gate = load_gate(args.gate)
    result = require_gate_pass(gate, base_dir=args.base_dir)
    print(
        json.dumps(
            {
                "gate_name": gate.gate_name,
                "status": gate.status.value,
                "ok": result.ok,
                "mode": result.mode,
                "reasons": list(result.reasons),
            },
            indent=2,
            sort_keys=True,
        )
    )
    ok = result.ok and gate.status is GateStatus.PASS
    return EXIT_OK if ok else EXIT_VERIFY_FAILED


def add_layer2_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from .layer2_feature_view_commands import add_feature_view_subparser
    from .layer2_harness_commands import add_harness_subparser
    from .layer2_split_commands import add_split_subparser

    p_layer2 = sub.add_parser(
        "layer2", help="Layer 2 operations (L2-B database, L2-C split, L2-F harness)"
    )
    l2_sub = p_layer2.add_subparsers(dest="layer2_command", required=True)
    add_split_subparser(l2_sub)
    add_feature_view_subparser(l2_sub)
    add_harness_subparser(l2_sub)
    p_db = l2_sub.add_parser("db", help="Layer 2 database (L2-B) operations")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)

    p_qual = db_sub.add_parser("qualify", help="run DB-READY qualification (or --check to verify)")
    p_qual.add_argument("--check", action="store_true", help="verify a committed gate only")
    p_qual.add_argument("--gate", default="gates/db-ready.json")
    p_qual.add_argument("--base-dir", default=".")
    p_qual.add_argument(
        "--no-descends", action="store_true", help="skip HEAD-descends-source check"
    )
    p_qual.set_defaults(func=_cmd_db_qualify)

    p_gate = db_sub.add_parser("gate", help="DB-READY gate verification")
    gate_sub = p_gate.add_subparsers(dest="db_gate_command", required=True)
    p_req = gate_sub.add_parser("require-pass", help="require a committed DB-READY gate to PASS")
    p_req.add_argument("--gate", default="gates/db-ready.json")
    p_req.add_argument("--base-dir", default=".")
    p_req.set_defaults(func=_cmd_db_require_pass)
