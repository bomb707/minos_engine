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

**It attests its own source.** The parent verifies the checkout before launching this process,
but that observation is made *before* the process exists — it cannot speak for what this process
actually imported and ran. So the bridge independently derives the authority identity from the
root it was given: the git HEAD and the three authority-file digests, hashed BEFORE the upstream
imports, again AFTER them, and again AFTER scoring completes. All three must agree, and the
modules that were actually imported must resolve beneath that root. A checkout mutated between
the parent's pre-flight and this process — or during the score itself — is therefore detectable
here rather than silently attributed to the earlier, stale observation.

Nothing about that attestation is taken from the caller: hashes supplied in the request would
prove only that the caller can repeat itself.

Two modes. ``probe`` imports upstream and reports the literal container references it would use
plus its source attestation, scoring nothing. ``score`` runs the real thing.

Protocol: ``python _minos_subnet_bridge.py <request.json> <response.json>``. The response is
written to a FILE rather than stdout because the upstream scorer prints diagnostics to stdout and
would otherwise corrupt the channel; stdout and stderr are captured by the caller for diagnostics
only. Exit status 0 means the protocol completed — including the cases where upstream declined to
produce metrics or the source attestation failed, both of which are reported in the response
rather than as a crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # noqa: S404 - fixed argv, shell=False, no caller-supplied executable
import sys
import traceback
from typing import Any

BRIDGE_SCHEMA = "l2f2-minos-subnet-bridge-v2"

#: exactly the files the scoring authority pins. Derived here, never accepted from the request.
AUTHORITY_FILES = ("utils/scoring.py", "neurons/validator.py", "templates/tool_params.py")

_CHUNK = 1024 * 1024


class _SourceAuthorityError(Exception):
    """The source this process is operating under is not a stable, in-root pinned checkout."""


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _source_digests(root: str) -> dict[str, str]:
    """Hash the three authority files as they exist on disk RIGHT NOW."""
    digests: dict[str, str] = {}
    for relative in AUTHORITY_FILES:
        path = os.path.join(root, relative)
        if os.path.islink(path) or not os.path.isfile(path):
            raise _SourceAuthorityError(f"authority file {relative} is missing or a symlink")
        digests[relative] = _sha256_file(path)
    return digests


def _git_head(root: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        ["git", "-C", root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise _SourceAuthorityError(f"{root} is not a git checkout or worktree")
    return completed.stdout.strip()


def _within(root: str, path: str) -> bool:
    """Is ``path`` genuinely beneath ``root``, after resolving links on both sides?"""
    resolved_root = os.path.realpath(root)
    resolved = os.path.realpath(path)
    return resolved == resolved_root or resolved.startswith(resolved_root + os.sep)


def _require_stable(label: str, first: dict[str, str], second: dict[str, str]) -> None:
    if first != second:
        changed = sorted(k for k in set(first) | set(second) if first.get(k) != second.get(k))
        raise _SourceAuthorityError(
            f"the pinned authority source changed {label}: {changed}. The score cannot be "
            "attributed to a single set of source bytes."
        )


def _run(request: dict[str, Any]) -> dict[str, Any]:
    root = request["upstream_root"]
    mode = request.get("mode", "score")
    if root not in sys.path:
        sys.path.insert(0, root)

    # (1) the identity BEFORE anything upstream is imported.
    git_head = _git_head(root)
    before_import = _source_digests(root)

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

    # (2) the modules that were ACTUALLY imported must come from the root we were given —
    #     not from a site-packages shadow, another PYTHONPATH entry, or the mutable clone.
    module_files = {
        "utils.scoring": upstream_scoring.__file__,
        "neurons.validator": upstream_validator.__file__,
    }
    outside = sorted(name for name, path in module_files.items() if not _within(root, path))
    if outside:
        raise _SourceAuthorityError(
            f"upstream modules {outside} were imported from outside the pinned root {root}: "
            f"{ {name: module_files[name] for name in outside} }"
        )

    # (3) importing must not have changed the source underneath us.
    after_import = _source_digests(root)
    _require_stable("during import", before_import, after_import)

    scorer = HappyScorer()
    # the LITERAL references the pinned source itself uses. Reported, never rewritten: the caller
    # verifies them against the authority and checks what they resolve to locally, but upstream
    # keeps constructing its own commands from its own constants.
    provenance: dict[str, Any] = {
        "python_version": sys.version.split()[0],
        "happy_upstream_ref": scorer.docker_image,
        "bcftools_upstream_ref": upstream_scoring.BCFTOOLS_DOCKER_IMAGE,
        "module_files": module_files,
        "modules_within_root": True,
    }
    attestation: dict[str, Any] = {
        "git_head": git_head,
        "before_import": before_import,
        "after_import": after_import,
        "after_score": None,
    }

    if mode == "probe":
        # PROBE: report what upstream would run, WITHOUT scoring anything. This is what lets
        # provenance be verified before a single biological byte is read.
        attestation["after_score"] = after_import
        return {
            "bridge_schema": BRIDGE_SCHEMA,
            "mode": "probe",
            "scored": False,
            "provenance": provenance,
            "source_attestation": attestation,
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
        result: dict[str, Any] = {
            "scored": False,
            "metrics": None,
            "advanced_score_100": None,
            "minos_score": None,
            "minos_score_accepted": False,
            "zero_input_fingerprint": False,
            "admitted": False,
            "admission_code": None,
        }
    else:
        metrics = {str(k): v for k, v in dict(metrics).items()}
        advanced_score_100 = float(AdvancedScorer.compute_advanced_score(metrics))

        # the validator's own call site constructs this argument and then asks its own helper
        # whether the value may be used. `normalized` is that argument, not a scoring formula;
        # `_valid_round_score` returning None IS the rejection, and when it accepts it returns
        # this exact float back.
        normalized = advanced_score_100 / 100.0
        combined_final = upstream_validator._valid_round_score(
            normalized, label="minos_engine offline evaluation"
        )
        if combined_final is None:
            # the label below is MINOS_ENGINE bookkeeping for WHICH upstream rejection occurred;
            # the accept/reject decision itself was made above, by upstream.
            result = {
                "scored": True,
                "metrics": metrics,
                "advanced_score_100": advanced_score_100,
                "minos_score": normalized,
                "minos_score_accepted": False,
                "zero_input_fingerprint": False,
                "admitted": False,
                "admission_code": (
                    "NONPOSITIVE_SCORE" if normalized <= 0.0 else "OUT_OF_RANGE_SCORE"
                ),
            }
        else:
            zero_input = bool(
                upstream_validator._is_zero_input_advanced_fingerprint(metrics, combined_final)
            )
            result = {
                "scored": True,
                "metrics": metrics,
                "advanced_score_100": advanced_score_100,
                "minos_score": float(combined_final),
                "minos_score_accepted": True,
                "zero_input_fingerprint": zero_input,
                "admitted": not zero_input,
                "admission_code": "ZERO_INPUT_FINGERPRINT" if zero_input else "ADMITTED",
            }

    # (4) the source must be the SAME bytes it was before the score. A mutation mid-score would
    #     otherwise produce a result no single set of source bytes can be said to have produced.
    after_score = _source_digests(root)
    _require_stable("during scoring", before_import, after_score)
    if _git_head(root) != git_head:
        raise _SourceAuthorityError("the pinned checkout's git HEAD moved during scoring")
    attestation["after_score"] = after_score

    return {
        "bridge_schema": BRIDGE_SCHEMA,
        "mode": "score",
        "provenance": provenance,
        "source_attestation": attestation,
        **result,
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
    except _SourceAuthorityError as exc:
        # a source-authority failure is NEVER reported alongside a usable scientific result.
        response = {
            "bridge_schema": BRIDGE_SCHEMA,
            "mode": request.get("mode", "score"),
            "scored": False,
            "source_authority_error": str(exc),
        }
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
