"""``minos-engine intake`` CLI — input-integrity attestation producer.

Subcommands:
  intake attest-input   compute file hashes + @SQ M5 cross-check for one registered
                        identity and write the deterministic attestation JSON.

The registry record is supplied as a JSON file (one object from the epoch registry
snapshot: dataset_id, round_id, chromosome, the four file hashes, region bounds,
region_hash, identity_tuple_hash) so the producer binds to a *selected* registered
identity, never to whatever the files happen to be.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1


def _cmd_attest_input(args: argparse.Namespace) -> int:
    from minos_engine.common.canonical_json import canonical_json_str
    from minos_engine.common.errors import IngestionError
    from minos_engine.intake.attestation import attest_input

    record = json.loads(Path(args.registry_record).read_text(encoding="utf-8"))
    try:
        attestation = attest_input(
            bam_path=Path(args.bam),
            bai_path=Path(args.bai),
            reference_path=Path(args.reference),
            fai_path=Path(args.fai),
            registry_record=record,
            registry_snapshot_hash=args.registry_snapshot_hash,
        )
    except IngestionError as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}, indent=2))
        return EXIT_FAILED
    payload = attestation.model_dump(mode="json")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_json_str(payload) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "OK",
                "attestation": str(out),
                "m5_status": attestation.m5_status.value,
                "attestation_hash": attestation.attestation_hash,
            },
            indent=2,
        )
    )
    return EXIT_OK


def register_intake_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_intake = sub.add_parser("intake", help="input intake operations")
    intake_sub = p_intake.add_subparsers(dest="intake_command", required=True)

    p_attest = intake_sub.add_parser(
        "attest-input",
        help="produce the input-integrity attestation for one registered identity",
    )
    p_attest.add_argument("--bam", required=True, help="path to input.bam")
    p_attest.add_argument("--bai", required=True, help="path to input.bam.bai")
    p_attest.add_argument("--reference", required=True, help="path to the contig FASTA")
    p_attest.add_argument("--fai", required=True, help="path to the FASTA .fai index")
    p_attest.add_argument(
        "--registry-record",
        required=True,
        help="JSON file with the selected registry-snapshot record for this identity",
    )
    p_attest.add_argument(
        "--registry-snapshot-hash",
        required=True,
        help="the epoch registry_snapshot_hash the record was selected from",
    )
    p_attest.add_argument("--out", required=True, help="output path for the attestation JSON")
    p_attest.set_defaults(func=_cmd_attest_input)
