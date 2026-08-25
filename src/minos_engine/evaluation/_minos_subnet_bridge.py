"""Isolated bridge that runs INSIDE the pinned MINOS_SUBNET checkout. Not a MINOS_ENGINE module.

This file is never imported by MINOS_ENGINE. It is executed as a standalone script by a separate
Python interpreter whose working directory is the verified upstream checkout, so the generic
upstream package names (``utils``, ``templates``, ``neurons``, ``base``) resolve to that checkout
and can never collide with MINOS_ENGINE's own imports in the long-lived evaluator process.

What it does is deliberately thin: it calls the ACTUAL upstream implementation and reports what
came back. It contains no scoring arithmetic, no metric parsing, no admission rule and no
container command — every one of those belongs to MINOS_SUBNET, whose exact bytes are pinned by
the scoring authority. If upstream changes, a new upstream commit is pinned; nothing here is
edited to match.

Two modes. ``probe`` imports upstream and reports the literal container references it would use,
scoring nothing — that is what lets MINOS_ENGINE verify runtime provenance before a single
biological byte is read. ``score`` runs the real thing.

Protocol: ``python _minos_subnet_bridge.py <request.json> <response.json>``. The response is
written to a FILE rather than stdout because the upstream scorer prints diagnostics to stdout and
would otherwise corrupt the channel; stdout and stderr are captured by the caller for diagnostics
only. Exit status 0 means the protocol completed — including the case where upstream declined to
produce metrics, which is reported in the response, not as a crash.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

BRIDGE_SCHEMA = "l2f2-minos-subnet-bridge-v1"


def _run(request: dict[str, Any]) -> dict[str, Any]:
    root = request["upstream_root"]
    if root not in sys.path:
        sys.path.insert(0, root)

    # the ACTUAL upstream implementation — never a copy, never a translation.
    # these resolve ONLY inside the verified upstream checkout, never in this repo.
    import neurons.validator as upstream_validator  # type: ignore[import-not-found] # noqa: PLC0415
    from utils import (  # type: ignore[import-not-found] # noqa: PLC0415
        AdvancedScorer,
        HappyScorer,
    )
    from utils import (
        scoring as upstream_scoring,
    )

    scorer = HappyScorer()
    # the LITERAL references the pinned source itself uses. Reported, never rewritten: the caller
    # verifies them against the authority and checks what they resolve to locally, but upstream
    # keeps constructing its own commands from its own constants.
    provenance = {
        "python_version": sys.version.split()[0],
        "happy_upstream_ref": scorer.docker_image,
        "bcftools_upstream_ref": upstream_scoring.BCFTOOLS_DOCKER_IMAGE,
        "module_files": {
            "utils.scoring": upstream_scoring.__file__,
            "neurons.validator": upstream_validator.__file__,
        },
    }
    if request.get("mode") == "probe":
        # PROBE: import upstream and report what it would run, WITHOUT scoring anything. This is
        # what lets provenance be verified before a single biological byte is read.
        return {
            "bridge_schema": BRIDGE_SCHEMA,
            "mode": "probe",
            "scored": False,
            "provenance": provenance,
        }

    # exactly the arguments the Minos validator supplies to its own scorer.
    metrics = scorer.score_vcf(
        truth_vcf=request["truth_vcf"],
        query_vcf=request["query_vcf"],
        reference_fasta=request["reference_fasta"],
        confident_bed=request["confident_bed"],
        region=request["region"],
        reference_sdf=request["reference_sdf"],
        mutations_vcf=request["mutations_vcf"],
    )
    if metrics is None:
        # upstream declined to produce metrics at all; that is not an admission outcome.
        return {
            "bridge_schema": BRIDGE_SCHEMA,
            "mode": "score",
            "scored": False,
            "metrics": None,
            "advanced_score_100": None,
            "minos_score": None,
            "minos_score_accepted": False,
            "zero_input_fingerprint": False,
            "admitted": False,
            "admission_code": None,
            "provenance": provenance,
        }

    metrics = {str(k): v for k, v in dict(metrics).items()}

    # the authoritative final score, and then the validator's OWN control flow — its helpers are
    # called, never reimplemented. `_valid_round_score` returning None IS the rejection.
    advanced_score_100 = float(AdvancedScorer.compute_advanced_score(metrics))

    # the validator's own call site constructs this argument and then asks its own helper whether
    # the value may be used. `normalized` is that argument, not a scoring formula; `_valid_round_score`
    # returning None IS the rejection, and when it accepts it returns this exact float back.
    normalized = advanced_score_100 / 100.0
    combined_final = upstream_validator._valid_round_score(
        normalized, label="minos_engine offline evaluation"
    )
    if combined_final is None:
        # the label below is MINOS_ENGINE bookkeeping for WHICH upstream rejection occurred; the
        # accept/reject decision itself was made above, by upstream.
        code = "NONPOSITIVE_SCORE" if normalized <= 0.0 else "OUT_OF_RANGE_SCORE"
        return {
            "bridge_schema": BRIDGE_SCHEMA,
            "mode": "score",
            "scored": True,
            "metrics": metrics,
            "advanced_score_100": advanced_score_100,
            "minos_score": normalized,
            "minos_score_accepted": False,
            "zero_input_fingerprint": False,
            "admitted": False,
            "admission_code": code,
            "provenance": provenance,
        }

    zero_input = bool(
        upstream_validator._is_zero_input_advanced_fingerprint(metrics, combined_final)
    )
    return {
        "bridge_schema": BRIDGE_SCHEMA,
        "mode": "score",
        "scored": True,
        "metrics": metrics,
        "advanced_score_100": advanced_score_100,
        "minos_score": float(combined_final),
        "minos_score_accepted": True,
        "zero_input_fingerprint": zero_input,
        "admitted": not zero_input,
        "admission_code": "ZERO_INPUT_FINGERPRINT" if zero_input else "ADMITTED",
        "provenance": provenance,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: _minos_subnet_bridge.py <request.json> <response.json>\n")
        return 2
    request_path, response_path = argv[1], argv[2]
    with open(request_path, encoding="utf-8") as handle:
        request = json.load(handle)
    try:
        response = _run(request)
    except BaseException:  # reported as data, so the caller sees WHY upstream could not run
        response = {
            "bridge_schema": BRIDGE_SCHEMA,
            "mode": request.get("mode", "score"),
            "scored": False,
            "error": traceback.format_exc(limit=12),
        }
    with open(response_path, "w", encoding="utf-8") as handle:
        json.dump(response, handle, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
