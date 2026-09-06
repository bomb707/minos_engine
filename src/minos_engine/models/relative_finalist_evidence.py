"""Trusted v2 campaign evidence: capability, staged publication, and offline verification.

The same architecture campaign v1 earned, applied before the expensive v2 result exists rather
than after. A campaign that returned a mutable dictionary could be edited between running and
publishing, and a shortlist recorded rather than re-derived is a claim rather than a finding.

The v2 shortlist is re-derived offline from bound per-spec metrics under the three-part rule, so
editing it in a published result fails verification even if the file is rehashed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.relative_finalist_protocol import qualifies_against_bar
from minos_engine.qualification.l2f_accepted_identities import repository_root
from minos_engine.qualification.provenance import read_provenance

__all__ = [
    "V2_CAMPAIGN_RESULT_SCHEMA",
    "V2_METRIC_ARTIFACT_SCHEMA",
    "V2_OOF_ARTIFACT_SCHEMA",
    "V2_OUTPUT_LAYOUT",
    "TrustedL2GV2TrainCampaign",
    "V2EvidenceError",
    "build_v2_campaign_result",
    "mint_trusted_v2_campaign",
    "verify_published_l2g_v2_train_campaign",
    "verify_v2_campaign_result",
    "write_l2g_v2_train_campaign_outputs",
]

V2_OOF_ARTIFACT_SCHEMA: Final = "l2g-v2-relative-oof-artifact-v1"
V2_OOF_ARTIFACT_DOMAIN: Final = "minos:l2g-v2-relative-oof-artifact:v1\n"
V2_METRIC_ARTIFACT_SCHEMA: Final = "l2g-v2-relative-metric-artifact-v1"
V2_METRIC_ARTIFACT_DOMAIN: Final = "minos:l2g-v2-relative-metric-artifact:v1\n"
V2_CAMPAIGN_RESULT_SCHEMA: Final = "l2g-v2-train-oof-campaign-result-v1"
V2_CAMPAIGN_RESULT_DOMAIN: Final = "minos:l2g-v2-train-oof-campaign-result:v1\n"

V2_OUTPUT_LAYOUT: Final[dict[str, str]] = {
    "root": "minos_l2g_v2_train_oof",
    "campaign_result": "campaign-result.json",
    "oof_dir": "oof",
    "metrics_dir": "metrics",
    "dir_mode": "0o750",
    "file_mode": "0o640",
}
_DIR_MODE: Final = 0o750
_FILE_MODE: Final = 0o640
MEDIA_TYPE: Final = "application/json"

EXPECTED_OOF_RECORDS: Final = 150
EXPECTED_DECISIONS: Final = 50
EXPECTED_MARGINS: Final = 5
STATUS_COMPLETE: Final = "COMPLETE"
STATUS_TRAINING_FAILURE: Final = "TRAINING_FAILURE"

_CAMPAIGN_TOKEN: Final = object()


class V2EvidenceError(MinosEngineError):
    """The v2 campaign evidence could not be produced or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise V2EvidenceError(message)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value


class TrustedL2GV2TrainCampaign:
    """A v2 campaign that actually ran, with its records retained by value."""

    __slots__ = ("_authority", "_per_spec", "_references", "_shortlist")

    def __init__(
        self,
        token: object,
        *,
        authority: dict[str, Any],
        per_spec: dict[str, dict[str, Any]],
        references: dict[str, Any],
        shortlist: tuple[str, ...],
    ) -> None:
        if token is not _CAMPAIGN_TOKEN:
            raise V2EvidenceError(
                "a trusted v2 campaign may only be minted by the sealed production entry; a "
                "caller-built dictionary is not evidence that a campaign ran"
            )
        self._authority = copy.deepcopy(authority)
        self._per_spec = copy.deepcopy(per_spec)
        self._references = copy.deepcopy(references)
        self._shortlist = tuple(shortlist)

    @property
    def authority(self) -> dict[str, Any]:
        return copy.deepcopy(self._authority)

    @property
    def shortlist(self) -> tuple[str, ...]:
        return self._shortlist

    def spec_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(self._per_spec))

    def spec(self, spec_hash: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._per_spec[spec_hash])
        except KeyError:
            raise V2EvidenceError(f"{spec_hash} is not in this campaign") from None

    def references(self) -> dict[str, Any]:
        return copy.deepcopy(self._references)


def assess_v2_completeness(entry: dict[str, Any]) -> dict[str, Any]:
    """COMPLETE means every relative cell and every BAM decided exactly once, in five folds."""
    records = entry.get("records", [])
    decisions = entry.get("decisions", [])
    margins = entry.get("margins", {})
    cells = [(r["dataset_id"], r["alternative_config"]) for r in records]
    bams = [d["dataset_id"] for d in decisions]
    folds = {r["outer_fold"] for r in records}

    reasons: list[str] = []
    if entry.get("training_failures"):
        reasons.append(f"{len(entry['training_failures'])} training failure(s)")
    if len(records) != EXPECTED_OOF_RECORDS:
        reasons.append(f"{len(records)} of {EXPECTED_OOF_RECORDS} relative records")
    if len(set(cells)) != len(cells):
        reasons.append("a relative cell was predicted more than once")
    if len(decisions) != EXPECTED_DECISIONS:
        reasons.append(f"{len(decisions)} of {EXPECTED_DECISIONS} BAM decisions")
    if len(set(bams)) != len(bams):
        reasons.append("a BAM was decided more than once")
    if len(margins) != EXPECTED_MARGINS:
        reasons.append(f"{len(margins)} of {EXPECTED_MARGINS} outer margins")
    if folds and folds != set(CV_FOLD_CHROMOSOMES):
        reasons.append(f"folds {sorted(folds)} are not the five chromosomes")
    expected_cells = entry.get("expected_cell_set")
    exact = False
    if expected_cells is not None:
        observed = {tuple(c) for c in cells}
        wanted = {tuple(c) for c in expected_cells}
        if observed != wanted:
            reasons.append(
                f"the predicted cell set is not the frozen one: "
                f"{len(wanted - observed)} missing, {len(observed - wanted)} foreign"
            )
        else:
            exact = True
    for value in list(margins.values()):
        if not isinstance(value, (int, float)) or value != value:
            reasons.append("a learned margin is not finite")
            break
    return {
        "status": STATUS_COMPLETE if not reasons else STATUS_TRAINING_FAILURE,
        "expected_oof_record_count": EXPECTED_OOF_RECORDS,
        "observed_oof_record_count": len(records),
        "expected_decision_count": EXPECTED_DECISIONS,
        "observed_decision_count": len(decisions),
        "expected_margin_count": EXPECTED_MARGINS,
        "observed_margin_count": len(margins),
        "unique_bam_count": len(set(bams)),
        "duplicate_cell_count": len(cells) - len(set(cells)),
        "exact_cell_set_verified": exact,
        "reasons": reasons,
    }


def mint_trusted_v2_campaign(
    token: object,
    *,
    authority: dict[str, Any],
    per_spec: dict[str, dict[str, Any]],
    references: dict[str, Any],
    shortlist: tuple[str, ...],
) -> TrustedL2GV2TrainCampaign:
    """Internal mint. The token is module-private; nothing outside can supply it."""
    return TrustedL2GV2TrainCampaign(
        token,
        authority=authority,
        per_spec=per_spec,
        references=references,
        shortlist=shortlist,
    )


# ---------------------------------------------------------------------------------------- #
# canonical artifacts
# ---------------------------------------------------------------------------------------- #
def v2_oof_artifact_content(
    *, spec_hash: str, entry: dict[str, Any], authority: dict[str, Any]
) -> dict[str, Any]:
    records = sorted(
        (_jsonable(r) for r in entry["records"]),
        key=lambda r: (r["dataset_id"], r["alternative_config"]),
    )
    decisions = sorted((_jsonable(d) for d in entry["decisions"]), key=lambda d: d["dataset_id"])
    for record in records:
        _require(record["spec_hash"] == spec_hash, "a record cites a different spec")
    return {
        "schema_version": V2_OOF_ARTIFACT_SCHEMA,
        "model_spec_hash": spec_hash,
        "relative_protocol_hash": authority["relative_protocol_hash"],
        "relative_dataset_identity": authority["relative_dataset_identity"],
        "finalist_domain_hash": authority["finalist_domain_hash"],
        "record_count": len(records),
        "decision_count": len(decisions),
        "margin_count": len(entry["margins"]),
        "margins": dict(sorted(entry["margins"].items())),
        "records": records,
        "decisions": decisions,
    }


def v2_oof_artifact_identity(content: dict[str, Any]) -> str:
    return sha256_hex(V2_OOF_ARTIFACT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def v2_metric_artifact_content(
    *,
    spec_hash: str,
    metrics: dict[str, Any],
    diagnostics: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": V2_METRIC_ARTIFACT_SCHEMA,
        "model_spec_hash": spec_hash,
        "relative_protocol_hash": authority["relative_protocol_hash"],
        "relative_dataset_identity": authority["relative_dataset_identity"],
        "metrics": _jsonable(metrics),
        "diagnostics": _jsonable(diagnostics),
    }


def v2_metric_artifact_identity(content: dict[str, Any]) -> str:
    return sha256_hex(V2_METRIC_ARTIFACT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def v2_campaign_result_identity(content: dict[str, Any]) -> str:
    return sha256_hex(V2_CAMPAIGN_RESULT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


# ---------------------------------------------------------------------------------------- #
# publication
# ---------------------------------------------------------------------------------------- #
def _write_atomic(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical bytes, then READ BACK from the final path: the SHA is a fact about the file."""
    expected = canonical_json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, _DIR_MODE)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    _require(path.is_file() and not path.is_symlink(), f"{path} is not a regular file")
    observed = path.read_bytes()
    _require(observed == expected, f"{path.name} on disk differs from the canonical bytes")
    return {"file_sha256": hashlib.sha256(observed).hexdigest(), "size_bytes": len(observed)}


def build_v2_campaign_result(
    *, trusted: Any, published: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Derive the canonical result from the trusted campaign and the bytes actually written."""
    _require(
        isinstance(trusted, TrustedL2GV2TrainCampaign),
        "a v2 campaign result may only be built from a trusted campaign",
    )
    authority = trusted.authority
    references = trusted.references()
    bar = references["ALWAYS_SAFE_BASELINE"]["metrics"]

    per_spec = []
    for spec_hash in trusted.spec_hashes():
        entry = trusted.spec(spec_hash)
        completeness = assess_v2_completeness(entry)
        record: dict[str, Any] = {
            "spec_hash": spec_hash,
            "family": entry["family"],
            "status": completeness["status"],
            **{k: v for k, v in completeness.items() if k != "status"},
        }
        if completeness["status"] == STATUS_COMPLETE:
            evidence = published.get(spec_hash)
            _require(evidence is not None, f"{spec_hash} is COMPLETE but nothing was published")
            assert evidence is not None
            metrics = entry["metrics"]
            record.update(
                oof_scientific_hash=evidence["oof_scientific_hash"],
                oof_file_sha256=evidence["oof_file_sha256"],
                metric_scientific_hash=evidence["metric_scientific_hash"],
                metric_file_sha256=evidence["metric_file_sha256"],
                promotion_metrics={
                    "mean_regret": float(metrics["mean_regret"]),
                    "cvar_regret": float(metrics["cvar_regret"]),
                },
            )
        else:
            _require(
                spec_hash not in published,
                f"{spec_hash} did not complete but a scientific artifact was published",
            )
        per_spec.append(record)

    shortlist = sorted(trusted.shortlist)
    content = {
        "schema_version": V2_CAMPAIGN_RESULT_SCHEMA,
        **dict(sorted(authority.items())),
        "per_spec": sorted(per_spec, key=lambda e: e["spec_hash"]),
        "reference_metrics": {
            name: _jsonable(r["metrics"]) for name, r in sorted(references.items())
        },
        "safe_baseline_mean_regret": float(bar["mean_regret"]),
        "safe_baseline_cvar_regret": float(bar["cvar_regret"]),
        "shortlist": shortlist,
        "shortlist_empty": not shortlist,
        "validation_authorized": bool(shortlist),
        "validation_read": False,
        "test_accessed": False,
    }
    verify_v2_campaign_result(content)
    return content


def verify_v2_campaign_result(content: dict[str, Any]) -> dict[str, Any]:
    """Re-derive the shortlist from the bound metrics under the frozen three-part rule."""
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_RELATIVE_DATASET_IDENTITY,
        PARENT_V1_CAMPAIGN_FREEZE,
    )
    from minos_engine.models.relative_finalist_protocol import compute_relative_protocol_hash

    _require(
        content.get("schema_version") == V2_CAMPAIGN_RESULT_SCHEMA,
        f"unexpected v2 result schema {content.get('schema_version')!r}",
    )
    _require(
        content.get("relative_dataset_identity") == ACCEPTED_RELATIVE_DATASET_IDENTITY,
        "the result cites a foreign relative dataset",
    )
    _require(
        content.get("relative_protocol_hash") == compute_relative_protocol_hash(),
        "the result cites a protocol this source does not compute",
    )
    _require(
        content.get("parent_campaign_freeze_identity") == PARENT_V1_CAMPAIGN_FREEZE,
        "the result does not bind the campaign-v1 freeze as its parent",
    )
    per_spec = list(content["per_spec"])
    _require(len(per_spec) == 4, "a v2 result must describe all four frozen specs")

    bar_mean = float(content["safe_baseline_mean_regret"])
    bar_cvar = float(content["safe_baseline_cvar_regret"])
    complete = {}
    for entry in per_spec:
        if entry["status"] == STATUS_COMPLETE:
            _require(
                entry["observed_oof_record_count"] == EXPECTED_OOF_RECORDS,
                f"{entry['spec_hash']} claims COMPLETE without 150 records",
            )
            _require(
                entry["observed_decision_count"] == EXPECTED_DECISIONS,
                f"{entry['spec_hash']} claims COMPLETE without 50 decisions",
            )
            _require(
                entry["observed_margin_count"] == EXPECTED_MARGINS,
                f"{entry['spec_hash']} claims COMPLETE without 5 margins",
            )
            _require(
                entry["duplicate_cell_count"] == 0,
                f"{entry['spec_hash']} claims COMPLETE with duplicate cells",
            )
            _require(
                bool(entry["exact_cell_set_verified"]),
                f"{entry['spec_hash']} claims COMPLETE without an exact cell-set proof",
            )
            for name in (
                "oof_scientific_hash",
                "oof_file_sha256",
                "metric_scientific_hash",
                "metric_file_sha256",
            ):
                _require(bool(entry.get(name)), f"{entry['spec_hash']} is COMPLETE with no {name}")
            complete[entry["spec_hash"]] = entry["promotion_metrics"]
        else:
            for name in ("oof_scientific_hash", "metric_scientific_hash", "promotion_metrics"):
                _require(name not in entry, f"{entry['spec_hash']} failed but carries {name}")

    rederived = sorted(
        h
        for h, m in complete.items()
        if qualifies_against_bar(
            mean_regret=float(m["mean_regret"]),
            cvar_regret=float(m["cvar_regret"]),
            bar_mean=bar_mean,
            bar_cvar=bar_cvar,
        )
    )
    _require(
        sorted(content["shortlist"]) == rederived,
        f"the recorded shortlist {sorted(content['shortlist'])} is not what the frozen "
        f"three-part rule derives: {rederived}",
    )
    _require(
        bool(content["shortlist_empty"]) == (not content["shortlist"]),
        "shortlist_empty disagrees with the shortlist",
    )
    _require(
        bool(content["validation_authorized"]) == bool(content["shortlist"]),
        "validation_authorized disagrees with the shortlist",
    )
    _require(content["validation_read"] is False, "the result records a VALIDATION read")
    _require(content["test_accessed"] is False, "the result records a TEST access")
    return {"ok": True, "complete_spec_count": len(complete), "shortlist_size": len(rederived)}


def write_l2g_v2_train_campaign_outputs(trusted: Any, *, output_dir: Any) -> dict[str, Any]:
    """Stage, read back, verify, then atomically promote. Refuses an existing final target."""
    _require(
        isinstance(trusted, TrustedL2GV2TrainCampaign),
        "only a trusted v2 campaign may be published",
    )
    final = Path(output_dir)
    _require(not final.exists(), f"{final} already exists; refusing to overwrite v2 evidence")

    authority = trusted.authority
    provenance = read_provenance(repository_root())
    _require(
        provenance.head_sha == authority["execution_source_commit"]
        and provenance.tree_sha == authority["execution_source_tree"],
        "the checkout moved between fitting and publication",
    )
    _require(provenance.worktree_clean, "the worktree is dirty at publication time")

    staging = final.parent / f"{final.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        staging.mkdir(parents=True)
        os.chmod(staging, _DIR_MODE)
        published: dict[str, dict[str, Any]] = {}
        for spec_hash in trusted.spec_hashes():
            entry = trusted.spec(spec_hash)
            if assess_v2_completeness(entry)["status"] != STATUS_COMPLETE:
                continue
            oof = v2_oof_artifact_content(spec_hash=spec_hash, entry=entry, authority=authority)
            metric = v2_metric_artifact_content(
                spec_hash=spec_hash,
                metrics=entry["metrics"],
                diagnostics=entry.get("diagnostics", {}),
                authority=authority,
            )
            oof_file = _write_atomic(
                staging / V2_OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json", oof
            )
            metric_file = _write_atomic(
                staging / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json", metric
            )
            published[spec_hash] = {
                "oof_scientific_hash": v2_oof_artifact_identity(oof),
                "oof_file_sha256": oof_file["file_sha256"],
                "metric_scientific_hash": v2_metric_artifact_identity(metric),
                "metric_file_sha256": metric_file["file_sha256"],
            }
        result = build_v2_campaign_result(trusted=trusted, published=published)
        result_file = _write_atomic(staging / V2_OUTPUT_LAYOUT["campaign_result"], result)
        reloaded = json.loads((staging / V2_OUTPUT_LAYOUT["campaign_result"]).read_bytes())
        verify_v2_campaign_result(reloaded)
        verify_published_l2g_v2_train_campaign(staging)
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "output_dir": str(final),
        "campaign_result_path": str(final / V2_OUTPUT_LAYOUT["campaign_result"]),
        "campaign_result_identity": v2_campaign_result_identity(result),
        "campaign_result_file_sha256": result_file["file_sha256"],
        "published": published,
    }


def verify_published_l2g_v2_train_campaign(
    output_dir: Any, *, repository_root: Any = None
) -> dict[str, Any]:
    """Verify a whole published v2 tree offline. No TRAIN database, no trust in the result."""
    root = Path(output_dir)
    result_path = root / V2_OUTPUT_LAYOUT["campaign_result"]
    _require(result_path.is_file() and not result_path.is_symlink(), f"{result_path} is missing")
    result = json.loads(result_path.read_bytes())
    verify_v2_campaign_result(result)

    accounted: set[str] = set()
    for entry in result["per_spec"]:
        if entry["status"] != STATUS_COMPLETE:
            for stem in (V2_OUTPUT_LAYOUT["oof_dir"], V2_OUTPUT_LAYOUT["metrics_dir"]):
                _require(
                    not (root / stem / f"{entry['spec_hash']}.json").exists(),
                    f"{entry['spec_hash']} failed but has a scientific artifact",
                )
            continue
        accounted.add(entry["spec_hash"])
        oof_path = root / V2_OUTPUT_LAYOUT["oof_dir"] / f"{entry['spec_hash']}.json"
        metric_path = root / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{entry['spec_hash']}.json"
        for path, sha in (
            (oof_path, entry["oof_file_sha256"]),
            (metric_path, entry["metric_file_sha256"]),
        ):
            _require(path.is_file() and not path.is_symlink(), f"{path} is missing")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            _require(actual == sha, f"{path.name} hashes to {actual}, expected {sha}")
        oof = json.loads(oof_path.read_bytes())
        _require(
            oof["model_spec_hash"] == entry["spec_hash"],
            "an OOF artifact describes a different spec",
        )
        _require(
            v2_oof_artifact_identity(oof) == entry["oof_scientific_hash"],
            "the OOF artifact's recomputed identity does not match the result",
        )
        _require(oof["record_count"] == EXPECTED_OOF_RECORDS, "the OOF artifact lacks 150 records")
        _require(oof["decision_count"] == EXPECTED_DECISIONS, "the OOF artifact lacks 50 decisions")
        _require(oof["margin_count"] == EXPECTED_MARGINS, "the OOF artifact lacks 5 margins")
        metric = json.loads(metric_path.read_bytes())
        _require(
            metric["model_spec_hash"] == entry["spec_hash"],
            "a metric artifact describes a different spec",
        )
        _require(
            v2_metric_artifact_identity(metric) == entry["metric_scientific_hash"],
            "the metric artifact's recomputed identity does not match the result",
        )
        for name in ("mean_regret", "cvar_regret"):
            _require(
                float(metric["metrics"][name]) == float(entry["promotion_metrics"][name]),
                f"{name} in the artifact differs from the result's promotion metrics",
            )

    for stem in (V2_OUTPUT_LAYOUT["oof_dir"], V2_OUTPUT_LAYOUT["metrics_dir"]):
        directory = root / stem
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            _require(
                path.stem in accounted,
                f"{stem}/{path.name} is not accounted for by the campaign result",
            )
    return {
        "ok": True,
        "output_dir": str(root),
        "complete_spec_count": len(accounted),
        "shortlist_size": len(result["shortlist"]),
    }
