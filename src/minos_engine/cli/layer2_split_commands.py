"""``minos-engine layer2 split`` CLI — L2-C dataset registry + 50/10/15 split.

Subcommands:
  layer2 split discover   --dataset-root DIR    scan + confirm the corpus (no writes)
  layer2 split generate   --dataset-root DIR    write manifest + local input inventory
  layer2 split verify     --manifest FILE       non-mutating manifest verification
  layer2 split summarize  --manifest FILE       print counts / per-chromosome / hashes
  layer2 split qualify [--check]                run/verify SPLIT-FROZEN qualification
  layer2 split gate require-pass                require a committed SPLIT-FROZEN gate PASS
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1


def _cmd_split_discover(args: argparse.Namespace) -> int:
    from minos_engine.layer2.split.discovery import discover_corpus
    from minos_engine.layer2.split.generator import _assign

    samples = discover_corpus(args.dataset_root)
    by_chrom = Counter(s.chromosome for s in samples)
    assignment = _assign(samples)
    parts: Counter[str] = Counter(assignment[s.round_id][0] for s in samples)
    print(
        json.dumps(
            {
                "total_samples": len(samples),
                "per_chromosome": dict(sorted(by_chrom.items())),
                "partition_totals": dict(sorted(parts.items())),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _cmd_split_generate(args: argparse.Namespace) -> int:
    from minos_engine.common.canonical_json import canonical_json_str
    from minos_engine.layer2.split.generator import generate

    manifest, inventory = generate(args.dataset_root)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "layer2_dataset_split_v1.json"
    inventory_path = out / "layer2_local_input_inventory_v1.json"
    manifest_path.write_text(canonical_json_str(manifest.to_canonical()) + "\n", encoding="utf-8")
    inventory_path.write_text(canonical_json_str(inventory.to_canonical()) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest_path": str(manifest_path),
                "inventory_path": str(inventory_path),
                "manifest_hash": manifest.manifest_hash,
                "dataset_registry_hash": manifest.dataset_registry_hash,
                "inventory_hash": inventory.inventory_hash,
                "counts": manifest.counts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _cmd_split_verify(args: argparse.Namespace) -> int:
    from minos_engine.layer2.split.verifier import verify_manifest_file

    result = verify_manifest_file(args.manifest)
    print(
        json.dumps(
            {
                "ok": result.ok,
                "manifest_hash": result.manifest_hash,
                "checks": result.checks,
                "reasons": list(result.reasons),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK if result.ok else EXIT_VERIFY_FAILED


def _cmd_split_summarize(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    per_partition: Counter[str] = Counter(s["partition"] for s in raw["samples"])
    per_chrom: dict[str, Counter[str]] = {}
    for s in raw["samples"]:
        per_chrom.setdefault(s["chromosome"], Counter())[s["partition"]] += 1
    print(
        json.dumps(
            {
                "total_samples": len(raw["samples"]),
                "counts": dict(sorted(per_partition.items())),
                "per_chromosome": {
                    c: dict(sorted(v.items())) for c, v in sorted(per_chrom.items())
                },
                "manifest_hash": raw.get("manifest_hash"),
                "dataset_registry_hash": raw.get("dataset_registry_hash"),
                "split_policy_hash": raw.get("split_policy_hash"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _cmd_split_qualify(args: argparse.Namespace) -> int:  # pragma: no cover - subprocess + PG
    from minos_engine.gates.contracts import GateStatus
    from minos_engine.qualification.layer2_split_runner import (
        qualify_split_frozen,
        verify_split_frozen_gate,
        write_split_outputs,
    )

    root = Path(args.base_dir).resolve()
    if args.check:
        result = verify_split_frozen_gate(
            root, Path(args.gate), require_descends=not args.no_descends
        )
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

    qresult = qualify_split_frozen(root, args.dataset_root)
    gate_path, manifest_path, inventory_path, report_path = write_split_outputs(qresult, root)
    print(
        json.dumps(
            {
                "status": qresult.gate.status.value,
                "gate_hash": qresult.gate.gate_hash,
                "gate_path": str(gate_path),
                "manifest_path": str(manifest_path),
                "inventory_path": str(inventory_path),
                "report_path": str(report_path),
                "failing": sorted(k for k, v in qresult.mandatory.items() if not v),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK if qresult.gate.status is GateStatus.PASS else EXIT_VERIFY_FAILED


def _cmd_split_require_pass(args: argparse.Namespace) -> int:
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


def add_split_subparser(l2_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_split = l2_sub.add_parser("split", help="Layer 2 dataset split (L2-C) operations")
    split_sub = p_split.add_subparsers(dest="split_command", required=True)

    p_disc = split_sub.add_parser("discover", help="scan + confirm the corpus (no writes)")
    p_disc.add_argument("--dataset-root", required=True)
    p_disc.set_defaults(func=_cmd_split_discover)

    p_gen = split_sub.add_parser("generate", help="write manifest + local input inventory")
    p_gen.add_argument("--dataset-root", required=True)
    p_gen.add_argument("--out-dir", default="manifests")
    p_gen.set_defaults(func=_cmd_split_generate)

    p_ver = split_sub.add_parser("verify", help="non-mutating manifest verification")
    p_ver.add_argument("--manifest", default="manifests/layer2_dataset_split_v1.json")
    p_ver.set_defaults(func=_cmd_split_verify)

    p_sum = split_sub.add_parser("summarize", help="print counts / per-chromosome / hashes")
    p_sum.add_argument("--manifest", default="manifests/layer2_dataset_split_v1.json")
    p_sum.set_defaults(func=_cmd_split_summarize)

    p_qual = split_sub.add_parser("qualify", help="run SPLIT-FROZEN qualification (or --check)")
    p_qual.add_argument("--check", action="store_true", help="verify a committed gate only")
    p_qual.add_argument("--gate", default="gates/split-frozen.json")
    p_qual.add_argument("--base-dir", default=".")
    p_qual.add_argument("--dataset-root", default=None)
    p_qual.add_argument(
        "--no-descends", action="store_true", help="skip HEAD-descends-source check"
    )
    p_qual.set_defaults(func=_cmd_split_qualify)

    p_gate = split_sub.add_parser("gate", help="SPLIT-FROZEN gate verification")
    gate_sub = p_gate.add_subparsers(dest="split_gate_command", required=True)
    p_req = gate_sub.add_parser(
        "require-pass", help="require a committed SPLIT-FROZEN gate to PASS"
    )
    p_req.add_argument("--gate", default="gates/split-frozen.json")
    p_req.add_argument("--base-dir", default=".")
    p_req.set_defaults(func=_cmd_split_require_pass)
