"""The ALREADY-FROZEN TRAIN shortlist rule, and the shape of the campaign result it produces.

The rule is not invented here and must not be adjusted after seeing numbers: a promotable spec
enters the shortlist iff its OOF mean regret AND its OOF CVaR-0.25 regret are both no worse than
the best reference's. If nothing clears both bars the shortlist is EMPTY, MODELS-QUALIFIED will
hold, and SAFE_BASELINE remains the fallback — promoting the least-bad contextual model would be
choosing a threshold after the fact.
"""

from __future__ import annotations

import re
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.campaign import (
    ACCEPTED_CANDIDATE_FAMILIES,
    ACCEPTED_CANDIDATE_SPEC_HASHES,
    ACCEPTED_REFERENCE_SPECS,
)
from minos_engine.models.oof_runner import metric_artifact_identity

__all__ = [
    "SHORTLIST_RESULT_SCHEMA",
    "ShortlistError",
    "derive_train_shortlist",
    "derive_verified_train_shortlist",
    "build_campaign_result",
    "verify_campaign_result",
    "verify_campaign_result_source",
]

#: v2: the result now binds per-spec COMPLETENESS and the reference-threshold availability, which
#: materially changes what an accepted campaign asserts. No real result existed under v1.
SHORTLIST_RESULT_SCHEMA: Final = "l2g-train-oof-campaign-result-v2"
SHORTLIST_RESULT_DOMAIN: Final = "minos:l2g-train-oof-campaign-result:v2\n"
SUPERSEDED_RESULT_V1: Final = "SUPERSEDED_BEFORE_FIRST_CAMPAIGN"

_HEX64: Final = re.compile(r"[0-9a-f]{64}")

#: the EXACT accepted upstream identities a real campaign result must carry
ACCEPTED_AUTHORITIES: Final[dict[str, str]] = {
    "training_contract_hash": "35a9ac9139d880e236786461a29fd91610b04049cfba90f71af3d5b3a931c6b1",
    "training_dataset_hash": "d031758c58358270843b9b417ea034d1181a6aaafc1c94af000279c26dc62fcc",
    "cv_manifest_hash": "b441b15fdc185e62e243b93322d6c30d8787f49f9fafbb3dab6ac9371728d92f",
    "training_protocol_hash": "b13a408ad46ae120d776ccde075a4071c28692a1699b3bdf3241bbfbc6deb7cd",
    "training_runtime_hash": "e710e426784aa64d4fc6f9e37f212e3951b3707f3c804b851652d0affe84d30b",
    "feature_matrix_hash": "c6a8db848318e5c78839474fa62a4e8e408157a1e6f5cb1bdd18c9cd3d0118b2",
    "feature_matrix_artifact_sha256": "0396cb07734a18df803ac813d9d1224ecdc3ec9901d7b8a202ac8c6538f3c243",
    "config_encoding_identity": "3053fed09a1a7fdc9462a963871564275c88e4eca5fe3a898d2d6821c36b1fe4",
}


class ShortlistError(MinosEngineError):
    """The shortlist could not be derived under the frozen rule."""


def derive_train_shortlist(
    *,
    reference_metrics: dict[str, dict[str, float]],
    candidate_metrics: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Apply the frozen two-bar rule. Both metrics are lower-is-better regret."""
    if not reference_metrics:
        raise ShortlistError("no reference metrics; the promotion bar is undefined")
    for name, metrics in {**reference_metrics, **candidate_metrics}.items():
        missing = {"mean_regret", "cvar_regret"} - set(metrics)
        if missing:
            raise ShortlistError(f"{name} is missing {sorted(missing)}")

    best_mean = min(m["mean_regret"] for m in reference_metrics.values())
    best_cvar = min(m["cvar_regret"] for m in reference_metrics.values())

    shortlist = sorted(
        name
        for name, metrics in candidate_metrics.items()
        if metrics["mean_regret"] <= best_mean and metrics["cvar_regret"] <= best_cvar
    )
    return {
        "best_reference_mean_regret": best_mean,
        "best_reference_cvar_regret": best_cvar,
        "rule": (
            "mean_regret <= best_reference_mean_regret AND "
            "cvar_regret <= best_reference_cvar_regret"
        ),
        "shortlist": shortlist,
        "shortlist_empty": not shortlist,
        "fallback_if_empty": "SAFE_BASELINE_REMAINS_AND_MODELS_QUALIFIED_HOLDS",
    }


def _train_campaign_result_content(
    *,
    source_commit: str,
    source_tree: str,
    prefit_authority_sha256: str,
    training_dataset_hash: str,
    cv_manifest_hash: str,
    training_runtime_hash: str,
    candidate_spec_hashes: tuple[str, ...],
    reference_spec_hashes: tuple[str, ...],
    oof_artifact_hashes: dict[str, str],
    metric_artifact_hashes: dict[str, str],
    training_failures: tuple[dict[str, Any], ...],
    shortlist: dict[str, Any],
    thread_report: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """LOW-LEVEL, NON-AUTHORITATIVE serializer, retained for unit tests only.

    It will serialize whatever it is handed, which is why it is private:
    :func:`build_campaign_result` derives every field from a verified campaign closure instead.
    """
    if len(candidate_spec_hashes) + len(reference_spec_hashes) != 10:
        raise ShortlistError("a campaign result must bind all ten model-spec hashes")
    return {
        "schema_version": SHORTLIST_RESULT_SCHEMA,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "prefit_authority_sha256": prefit_authority_sha256,
        "training_dataset_hash": training_dataset_hash,
        "cv_manifest_hash": cv_manifest_hash,
        "training_runtime_hash": training_runtime_hash,
        "candidate_spec_hashes": list(candidate_spec_hashes),
        "reference_spec_hashes": list(reference_spec_hashes),
        "oof_artifact_hashes": dict(sorted(oof_artifact_hashes.items())),
        "metric_artifact_hashes": dict(sorted(metric_artifact_hashes.items())),
        "training_failures": list(training_failures),
        "best_reference_mean_regret": shortlist["best_reference_mean_regret"],
        "best_reference_cvar_regret": shortlist["best_reference_cvar_regret"],
        "shortlist": list(shortlist["shortlist"]),
        "shortlist_empty": bool(shortlist["shortlist_empty"]),
        "thread_report": list(thread_report),
        "validation_read": False,
        "test_accessed": False,
    }


def train_campaign_result_identity(content: dict[str, Any]) -> str:
    return sha256_hex(SHORTLIST_RESULT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def derive_verified_train_shortlist(
    *,
    reference_metrics: dict[str, dict[str, float]],
    candidate_metrics: dict[str, dict[str, float]],
    reference_spec_hashes: tuple[str, ...],
    candidate_spec_hashes: tuple[str, ...],
    ineligible_candidate_hashes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """FAIL-CLOSED wrapper. A partial dictionary must not be mistaken for a whole campaign.

    The pure helper below is happy to compare whatever it is handed; that is exactly why it must
    not be the production entry point. Here the reference set must be complete and exact, every
    candidate must be a known frozen spec, and an ineligible candidate can never appear.
    """
    if len(reference_spec_hashes) != 4:
        raise ShortlistError(
            f"{len(reference_spec_hashes)} reference specs; the promotion bar is defined by "
            "exactly the frozen four"
        )
    if len(set(reference_spec_hashes)) != 4:
        raise ShortlistError("a reference spec hash appears twice")
    missing = sorted(set(reference_spec_hashes) - set(reference_metrics))
    if missing:
        raise ShortlistError(
            f"reference metrics are missing for {missing}; the bar was never fully observed"
        )
    extra = sorted(set(reference_metrics) - set(reference_spec_hashes))
    if extra:
        raise ShortlistError(f"unknown reference spec(s) {extra}")

    known = set(candidate_spec_hashes)
    if len(known) != len(candidate_spec_hashes):
        raise ShortlistError("a candidate spec hash appears twice")
    unknown = sorted(set(candidate_metrics) - known)
    if unknown:
        raise ShortlistError(f"metrics supplied for unknown candidate spec(s) {unknown}")
    smuggled = sorted(set(candidate_metrics) & set(ineligible_candidate_hashes))
    if smuggled:
        raise ShortlistError(
            f"candidate(s) {smuggled} did not complete and cannot carry a promotable metric"
        )

    result = derive_train_shortlist(
        reference_metrics=reference_metrics, candidate_metrics=candidate_metrics
    )
    result["evaluated_candidate_count"] = len(candidate_metrics)
    result["ineligible_candidate_count"] = len(ineligible_candidate_hashes)
    result["reference_threshold_available"] = True
    return result


# ---------------------------------------------------------------------------------------- #
# THE TRUSTED CAMPAIGN RESULT
# ---------------------------------------------------------------------------------------- #
_REQUIRED_PER_SPEC_FIELDS: Final[tuple[str, ...]] = (
    "spec_hash",
    "family",
    "role",
    "status",
    "expected_outer_fold_count",
    "successful_outer_fold_count",
    "failed_folds",
    "expected_oof_record_count",
    "observed_oof_record_count",
    "unique_bam_count",
    "duplicate_cell_count",
    "exact_cell_set_verified",
    "training_failures",
)


def build_campaign_result(
    *, trusted: Any, published: dict[str, dict[str, Any]], root: Any = None
) -> dict[str, Any]:
    """Derive the canonical v2 content from a TRUSTED campaign and its PUBLISHED bytes.

    ``trusted`` cannot be a dictionary: only the sealed production entry can mint one, so this
    function has no path by which an operator-authored campaign becomes publishable. The operator
    supplies no shortlist, threshold, eligibility, status or hash; each is read out of the
    campaign that ran, and the artifact hashes come from files that were actually written.
    """
    from pathlib import Path as _Path

    from minos_engine.models.campaign import TrustedL2GTrainCampaign
    from minos_engine.qualification.l2f_accepted_identities import repository_root
    from minos_engine.qualification.provenance import read_provenance

    if not isinstance(trusted, TrustedL2GTrainCampaign):
        raise ShortlistError(
            "a campaign result may only be built from a trusted campaign minted by the sealed "
            "production entry; a dictionary is whatever its author typed"
        )
    campaign = trusted.closure

    base = _Path(root) if root is not None else repository_root()
    provenance = read_provenance(base)
    if not provenance.head_sha or not provenance.tree_sha:
        raise ShortlistError("the source commit/tree could not be read from Git")

    per_spec_in = campaign.get("per_spec")
    if not isinstance(per_spec_in, dict) or len(per_spec_in) != 10:
        raise ShortlistError("a campaign closure must describe all ten frozen specs")

    per_spec: list[dict[str, Any]] = []
    for spec_hash, entry in sorted(per_spec_in.items()):
        missing = [f for f in _REQUIRED_PER_SPEC_FIELDS if f not in entry]
        if missing:
            raise ShortlistError(f"{spec_hash} is missing completeness fields {missing}")
        record = {field: entry[field] for field in _REQUIRED_PER_SPEC_FIELDS}
        if entry["status"] == "COMPLETE":
            evidence = published.get(spec_hash)
            if evidence is None:
                raise ShortlistError(f"{spec_hash} is COMPLETE but nothing was published for it")
            metrics = trusted.metrics_for(spec_hash)
            expected = metric_artifact_identity(metrics, spec_hash=spec_hash)
            if evidence["metric_scientific_hash"] != expected:
                raise ShortlistError(
                    f"{spec_hash} carries a metric identity that does not describe its own "
                    "metrics; the artifacts were swapped or edited"
                )
            promotion = {name: float(metrics[name]) for name in ("mean_regret", "cvar_regret")}
            if evidence["promotion_metrics"] != promotion:
                raise ShortlistError(f"{spec_hash} published promotion metrics that differ")
            record.update(
                oof_scientific_hash=evidence["oof_scientific_hash"],
                oof_file_sha256=evidence["oof_file_sha256"],
                oof_size_bytes=evidence["oof_size_bytes"],
                metric_scientific_hash=evidence["metric_scientific_hash"],
                metric_file_sha256=evidence["metric_file_sha256"],
                metric_size_bytes=evidence["metric_size_bytes"],
                media_type=evidence["media_type"],
                # bound so an offline verifier can RE-DERIVE the shortlist rather than trust it
                promotion_metrics=promotion,
            )
        elif spec_hash in published:
            raise ShortlistError(
                f"{spec_hash} did not complete but a scientific artifact was published for it"
            )
        per_spec.append(record)

    content = {
        "schema_version": SHORTLIST_RESULT_SCHEMA,
        "source_commit": provenance.head_sha,
        "source_tree": provenance.tree_sha,
        **dict(sorted(campaign["authority"].items())),
        "candidate_spec_hashes": list(campaign["candidate_spec_hashes"]),
        "reference_spec_hashes": list(campaign["reference_spec_hashes"]),
        "per_spec": per_spec,
        "all_required_references_complete": bool(campaign["all_required_references_complete"]),
        "reference_threshold_available": bool(campaign["reference_threshold_available"]),
        "eligible_candidate_hashes": list(campaign["eligible_candidates"]),
        "ineligible_candidate_hashes": list(campaign["ineligible_candidates"]),
        "best_reference_mean_regret": campaign["best_reference_mean_regret"],
        "best_reference_cvar_regret": campaign["best_reference_cvar_regret"],
        "shortlist": list(campaign["shortlist"]),
        "shortlist_empty": bool(campaign["shortlist_empty"]),
        "fallback_if_empty": campaign["fallback_if_empty"],
        "thread_policy": campaign["thread_policy"],
        "thread_report": list(campaign["thread_report"]),
        "validation_read": False,
        "test_accessed": False,
    }
    verify_campaign_result(content)
    return content


def campaign_result_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity of a canonical campaign result."""
    return sha256_hex(SHORTLIST_RESULT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def verify_campaign_result(content: dict[str, Any]) -> dict[str, Any]:
    """Check the result's own assertions against each other. Fails closed.

    A result is the artifact a later stage will trust, so its internal claims have to be
    consistent without re-running anything: a spec cannot be COMPLETE with four folds, a failed
    candidate cannot be shortlisted, and a shortlist cannot exist without a fully observed bar.

    On artifact swaps, precisely: duplicated or reused artifact hashes are caught here, and a
    METRIC artifact exchanged between two specs is caught at build time, where the hash is
    recomputed from that spec's own metrics and its spec hash. An OOF hash exchanged inside an
    already-built result cannot be re-derived from content alone -- the records are not in the
    document -- so what protects it is the domain-separated result identity, which moves on any
    edit. Claiming more than that here would be false.
    """

    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ShortlistError(message)

    _require(
        content.get("schema_version") == SHORTLIST_RESULT_SCHEMA,
        f"schema is {content.get('schema_version')!r}, expected {SHORTLIST_RESULT_SCHEMA}",
    )
    for field in ("source_commit", "source_tree"):
        _require(bool(content.get(field)), f"{field} is missing")
    # EXACT accepted authorities, not merely well-formed strings. A foreign campaign whose
    # hashes are all 64 lowercase hex characters is precisely what this must refuse.
    for field, expected in ACCEPTED_AUTHORITIES.items():
        value = content.get(field)
        _require(
            value == expected,
            f"{field} is {value!r}, but the accepted authority is {expected}",
        )
    prefit = content.get("prefit_authority_sha256")
    _require(
        isinstance(prefit, str) and _HEX64.fullmatch(prefit) is not None,
        "prefit_authority_sha256 is not a lowercase 64-hex identity",
    )

    raw_per_spec = content.get("per_spec")
    _require(
        isinstance(raw_per_spec, list) and len(raw_per_spec) == 10, "expected ten spec records"
    )
    per_spec: list[dict[str, Any]] = list(raw_per_spec or [])
    by_hash = {e["spec_hash"]: e for e in per_spec}
    _require(len(by_hash) == 10, "a spec hash appears twice")

    candidates = list(content["candidate_spec_hashes"])
    references = list(content["reference_spec_hashes"])
    _require(len(candidates) == 6 and len(references) == 4, "expected six candidates, four refs")
    _require(
        tuple(candidates) == ACCEPTED_CANDIDATE_SPEC_HASHES,
        "the candidate spec identities are not the accepted frozen six",
    )
    _require(
        tuple(references) == tuple(h for _, h in ACCEPTED_REFERENCE_SPECS),
        "the reference spec identities are not the accepted frozen four",
    )
    expected_family = {
        **dict(zip(ACCEPTED_CANDIDATE_SPEC_HASHES, ACCEPTED_CANDIDATE_FAMILIES, strict=True)),
        **{h: f for f, h in ACCEPTED_REFERENCE_SPECS},
    }
    _require(
        set(candidates) | set(references) == set(by_hash),
        "the per-spec records do not describe exactly the ten declared specs",
    )

    complete: set[str] = set()
    for spec_hash, entry in by_hash.items():
        role = "REFERENCE" if spec_hash in references else "CANDIDATE"
        _require(entry["role"] == role, f"{spec_hash} is recorded as {entry['role']}, not {role}")
        _require(
            entry["family"] == expected_family[spec_hash],
            f"{spec_hash} is recorded as {entry['family']}, not {expected_family[spec_hash]}",
        )
        _require(entry["expected_outer_fold_count"] == 5, f"{spec_hash} expects != 5 folds")
        _require(
            entry["expected_oof_record_count"] == 1040,
            f"{spec_hash} expects {entry['expected_oof_record_count']} records, not 1040",
        )
        if entry["status"] == "COMPLETE":
            _require(
                entry["successful_outer_fold_count"] == 5,
                f"{spec_hash} claims COMPLETE with {entry['successful_outer_fold_count']} folds",
            )
            _require(
                entry["observed_oof_record_count"] == 1040,
                f"{spec_hash} claims COMPLETE with {entry['observed_oof_record_count']} records",
            )
            _require(
                entry["unique_bam_count"] == 50, f"{spec_hash} claims COMPLETE without 50 BAMs"
            )
            _require(
                entry["duplicate_cell_count"] == 0, f"{spec_hash} claims COMPLETE with duplicates"
            )
            _require(
                bool(entry["exact_cell_set_verified"]),
                f"{spec_hash} claims COMPLETE without an exact cell-set proof",
            )
            _require(not entry["failed_folds"], f"{spec_hash} claims COMPLETE with failed folds")
            _require(
                not entry["training_failures"],
                f"{spec_hash} claims COMPLETE with training failures",
            )
            for name in (
                "oof_scientific_hash",
                "oof_file_sha256",
                "metric_scientific_hash",
                "metric_file_sha256",
            ):
                _require(bool(entry.get(name)), f"{spec_hash} is COMPLETE with no {name}")
            promotion = entry.get("promotion_metrics")
            _require(
                isinstance(promotion, dict) and set(promotion) == {"mean_regret", "cvar_regret"},
                f"{spec_hash} is COMPLETE without its promotion metrics",
            )
            complete.add(spec_hash)
        else:
            _require(entry["status"] == "TRAINING_FAILURE", f"{spec_hash} has an unknown status")
            for name in (
                "oof_scientific_hash",
                "metric_scientific_hash",
                "oof_file_sha256",
                "metric_file_sha256",
                "promotion_metrics",
            ):
                _require(name not in entry, f"{spec_hash} failed but carries {name}")

    artifact_hashes = [e["oof_scientific_hash"] for e in per_spec if "oof_scientific_hash" in e]
    _require(
        len(set(artifact_hashes)) == len(artifact_hashes),
        "two specs share an OOF artifact hash; the artifacts were swapped or reused",
    )
    metric_hashes = [e["metric_scientific_hash"] for e in per_spec if "metric_scientific_hash" in e]
    _require(
        len(set(metric_hashes)) == len(metric_hashes), "two specs share a metric artifact hash"
    )

    references_complete = set(references) <= complete
    _require(
        bool(content["all_required_references_complete"]) == references_complete,
        "all_required_references_complete disagrees with the per-spec records",
    )
    _require(
        bool(content["reference_threshold_available"]) == references_complete,
        "reference_threshold_available disagrees with the reference completeness",
    )
    shortlist = list(content["shortlist"])
    if not references_complete:
        _require(not shortlist, "a shortlist exists although the promotion bar was never observed")
    eligible = set(content["eligible_candidate_hashes"])
    ineligible = set(content["ineligible_candidate_hashes"])
    _require(eligible <= complete, "an eligible candidate is not COMPLETE")
    _require(not (eligible & ineligible), "a candidate is both eligible and ineligible")
    _require(eligible | ineligible == set(candidates), "the candidate partition is incomplete")
    _require(set(shortlist) <= eligible, "a shortlisted candidate is not eligible and COMPLETE")
    _require(
        bool(content["shortlist_empty"]) == (not shortlist),
        "shortlist_empty disagrees with the shortlist",
    )
    # --- INDEPENDENT shortlist re-derivation ------------------------------------------- #
    # Recording a shortlist and asserting it was derived correctly proves nothing; the bound
    # promotion metrics let the frozen rule be run again here, from the document itself.
    if references_complete:
        reference_metrics = {
            h: by_hash[h]["promotion_metrics"] for h in references if h in complete
        }
        candidate_metrics = {
            h: by_hash[h]["promotion_metrics"] for h in candidates if h in complete
        }
        best_mean = min(m["mean_regret"] for m in reference_metrics.values())
        best_cvar = min(m["cvar_regret"] for m in reference_metrics.values())
        _require(
            content["best_reference_mean_regret"] == best_mean,
            f"best_reference_mean_regret is {content['best_reference_mean_regret']}, but the "
            f"bound reference metrics give {best_mean}",
        )
        _require(
            content["best_reference_cvar_regret"] == best_cvar,
            f"best_reference_cvar_regret is {content['best_reference_cvar_regret']}, but the "
            f"bound reference metrics give {best_cvar}",
        )
        rederived = sorted(
            h
            for h, m in candidate_metrics.items()
            if m["mean_regret"] <= best_mean and m["cvar_regret"] <= best_cvar
        )
        _require(
            sorted(shortlist) == rederived,
            f"the recorded shortlist {sorted(shortlist)} is not what the frozen two-bar rule "
            f"derives from the bound metrics: {rederived}",
        )
        _require(
            sorted(eligible) == sorted(candidate_metrics),
            "the eligible set does not match the COMPLETE candidates carrying metrics",
        )

    _require(
        content["thread_policy"] == "SINGLE_THREADED_DETERMINISTIC",
        f"thread_policy is {content.get('thread_policy')!r}",
    )
    report = content.get("thread_report")
    _require(isinstance(report, list) and bool(report), "the thread report is empty")
    for pool in report or []:
        _require(
            int(pool.get("num_threads", -1)) == 1,
            f"a recorded pool ran with {pool.get('num_threads')} threads",
        )
    _require(content["validation_read"] is False, "the result records a VALIDATION read")
    _require(content["test_accessed"] is False, "the result records a TEST access")
    return {"ok": True, "complete_spec_count": len(complete), "shortlist_size": len(shortlist)}


def verify_campaign_result_source(content: dict[str, Any], *, root: Any = None) -> dict[str, Any]:
    """Prove the recorded commit EXISTS and its real tree is the recorded one.

    Two non-empty strings prove nothing; a commit that Git does not have, or whose tree differs,
    means the result does not describe the source that produced it.
    """
    from pathlib import Path as _Path

    from minos_engine.qualification.git_tree import commit_tree_sha, is_commit
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = _Path(root) if root is not None else repository_root()
    commit = str(content["source_commit"])
    tree = str(content["source_tree"])
    if not is_commit(base, commit):
        raise ShortlistError(f"source_commit {commit} is not a commit in this repository")
    actual = commit_tree_sha(base, commit)
    if actual != tree:
        raise ShortlistError(
            f"source_commit {commit} has tree {actual}, but the result records {tree}"
        )
    return {"ok": True, "source_commit": commit, "source_tree": tree}
