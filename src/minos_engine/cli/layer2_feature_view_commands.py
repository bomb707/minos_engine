"""``minos-engine layer2 feature-view`` / ``feature-matrix`` CLI — E5 gate verification.

  layer2 feature-view   qualify --check     verify the committed FEATURE-VIEW-READY gate
  layer2 feature-view   gate require-pass    require the committed gate to PASS
  layer2 feature-matrix qualify --check     verify the committed FEATURE-MATRIX-FROZEN-1 gate
  layer2 feature-matrix gate require-pass    require the committed gate to PASS

``--check`` and ``require-pass`` are non-mutating and fail closed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from minos_engine.qualification import layer2_feature_view_runner as R

_EXIT_OK = 0
_EXIT_FAIL = 3


def _cmd_view_check(args: argparse.Namespace) -> int:
    root = Path(args.base_dir).resolve()
    res = R.verify_feature_view_ready_gate(
        root, Path(args.gate), require_descends=not args.no_descends
    )
    print(json.dumps({"gate": "FEATURE-VIEW-READY", "ok": res.ok, "reasons": list(res.reasons)}))
    return _EXIT_OK if res.ok else _EXIT_FAIL


def _cmd_matrix_check(args: argparse.Namespace) -> int:
    root = Path(args.base_dir).resolve()
    res = R.verify_feature_matrix_frozen_1_gate(
        root, Path(args.gate), require_descends=not args.no_descends
    )
    print(
        json.dumps({"gate": "FEATURE-MATRIX-FROZEN-1", "ok": res.ok, "reasons": list(res.reasons)})
    )
    return _EXIT_OK if res.ok else _EXIT_FAIL


def _cmd_require_pass(args: argparse.Namespace) -> int:
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.gates.verifier import load_gate, require_gate_pass

    gate = load_gate(args.gate)
    result = require_gate_pass(gate, base_dir=args.base_dir)
    print(
        json.dumps({"gate_name": gate.gate_name, "ok": result.ok, "reasons": list(result.reasons)})
    )
    return _EXIT_OK if (result.ok and gate.status is GateStatus.PASS) else _EXIT_FAIL


def _add_pair(
    l2_sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    command: str,
    default_gate: str,
    check_func: Callable[[argparse.Namespace], int],
) -> None:
    parser = l2_sub.add_parser(command, help=f"Layer 2-E {command} gate verification")
    csub = parser.add_subparsers(dest=f"{command}_command", required=True)

    q = csub.add_parser("qualify", help="verify the committed gate (--check, non-mutating)")
    q.add_argument("--check", action="store_true", help="verify a committed gate only")
    q.add_argument("--gate", default=default_gate)
    q.add_argument("--base-dir", default=".")
    q.add_argument("--no-descends", action="store_true", help="skip HEAD-descends-source check")
    q.set_defaults(func=check_func)

    g = csub.add_parser("gate", help="committed-gate verification")
    gsub = g.add_subparsers(dest=f"{command}_gate_command", required=True)
    req = gsub.add_parser("require-pass", help="require the committed gate to PASS")
    req.add_argument("--gate", default=default_gate)
    req.add_argument("--base-dir", default=".")
    req.set_defaults(func=_cmd_require_pass)


def add_feature_view_subparser(l2_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _add_pair(
        l2_sub,
        command="feature-view",
        default_gate=R.FEATURE_VIEW_READY_GATE_PATH,
        check_func=_cmd_view_check,
    )
    _add_pair(
        l2_sub,
        command="feature-matrix",
        default_gate=R.FEATURE_MATRIX_FROZEN_1_GATE_PATH,
        check_func=_cmd_matrix_check,
    )
