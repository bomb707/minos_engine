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
from minos_engine.models.relative_finalist_contract import (
    FINALIST_DOMAIN,
    SAFE_BASELINE_CONFIG_HASH,
)
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
    "VerifiedPublishedL2GV2TrainCampaign",
    "load_verified_published_l2g_v2_campaign",
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
FAILURE_REASON_LIMIT: Final = 300
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
    import math as _math

    # math.isfinite, not `value != value`: +inf and -inf are just as unusable as NaN
    for value in list(margins.values()):
        if not isinstance(value, (int, float)) or not _math.isfinite(float(value)):
            reasons.append("a learned margin is not finite")
            break
    for record in records:
        if not all(
            isinstance(record.get(f), (int, float)) and _math.isfinite(float(record[f]))
            for f in ("actual_delta", "predicted_delta")
        ):
            reasons.append("an advantage value is not finite")
            break
    for decision in decisions:
        if not all(
            isinstance(decision.get(f), (int, float)) and _math.isfinite(float(decision[f]))
            for f in (
                "regret",
                "selected_utility",
                "safe_utility",
                "oracle4_utility",
                "actual_selected_delta",
            )
        ):
            reasons.append("a decision value is not finite")
            break
    if entry.get("status") != STATUS_TRAINING_FAILURE and not entry.get("diagnostics"):
        reasons.append("a COMPLETE spec must carry its diagnostics")
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
            "implementation": entry["implementation"],
            "status": completeness["status"],
            # a failed candidate is ACCOUNTED FOR, not omitted: a reader must be able to tell
            # "this model was tried and failed" from "this model was never run"
            "training_failures": _jsonable(list(entry.get("training_failures", []))),
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
        # the decisions themselves, so the promotion bar can be recomputed rather than trusted
        "reference_decisions": {
            name: [_jsonable(d) for d in r.get("decisions", [])]
            for name, r in sorted(references.items())
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
            failures = list(entry.get("training_failures") or ())
            _require(
                bool(failures),
                f"{entry['spec_hash']} is TRAINING_FAILURE with no recorded reason",
            )
            for failure in failures:
                _require(
                    sorted(failure) == ["exception_type", "sanitized_reason", "stage"],
                    "a training failure entry is not the canonical three fields",
                )
                _require(
                    all(isinstance(failure[k], str) and failure[k] for k in failure),
                    "a training failure field is empty or not a string",
                )
                _require(
                    len(failure["sanitized_reason"]) <= FAILURE_REASON_LIMIT,
                    "a sanitised failure reason exceeds its cap",
                )
                _require(
                    "0x" not in failure["sanitized_reason"]
                    and "/" not in failure["sanitized_reason"]
                    and "Traceback" not in failure["sanitized_reason"],
                    "a sanitised failure reason leaks a path, an address or a traceback",
                )

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


def _recompute_policy_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute the decision metrics from published decisions, trusting no recorded scalar."""
    from minos_engine.models.relative_finalist_runner import policy_metrics

    class _D:
        def __init__(self, d: dict[str, Any]) -> None:
            self.__dict__.update(d)

    return policy_metrics([_D(d) for d in decisions])


def _decision_utilities(
    utility: dict[tuple[str, str], float], *, bam: str, selected: str
) -> dict[str, float]:
    """The four utility-derived fields of a decision, from the FROZEN table alone.

    Regret is oracle-minus-selected and the realised advantage is selected-minus-safe. Deriving
    them here rather than reading them means a published decision cannot claim a regret its own
    action does not produce.
    """
    safe_u = utility[(bam, SAFE_BASELINE_CONFIG_HASH)]
    selected_u = utility[(bam, selected)]
    oracle_u = max(utility[(bam, c)] for c in FINALIST_DOMAIN)
    return {
        "safe_utility": safe_u,
        "selected_utility": selected_u,
        "oracle4_utility": oracle_u,
        "regret": oracle_u - selected_u,
        "actual_selected_delta": selected_u - safe_u,
    }


def _recompute_delta_diagnostics(records: list[dict[str, Any]]) -> dict[str, float | None]:
    """Recompute the DELTA diagnostics from published records using the frozen definition."""
    from minos_engine.models.relative_finalist_runner import delta_diagnostics

    class _R:
        def __init__(self, d: dict[str, Any]) -> None:
            self.actual_delta = float(d["actual_delta"])
            self.predicted_delta = float(d["predicted_delta"])

    return delta_diagnostics([_R(r) for r in records])


def _regenerate_reference_decisions(
    utility: dict[tuple[str, str], float], chromosome_of: dict[str, str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Rebuild the three frozen reference policies from the frozen utilities and folds.

    This calls the same frozen ``reference_decisions`` the campaign uses, deliberately: the
    independence that matters is independence from the PUBLISHED BYTES, not a second hand-written
    copy of the rule that could silently disagree with the one the science was defined by.
    """
    from minos_engine.models.relative_finalist_runner import reference_decisions

    fields = (
        "actual_selected_delta",
        "chromosome",
        "oracle4_utility",
        "outer_fold",
        "regret",
        "safe_utility",
        "selected_config",
        "selected_utility",
        "switched",
    )
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name in ("ALWAYS_SAFE_BASELINE", "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN", "ORACLE4"):
        per_bam: dict[str, dict[str, Any]] = {}
        for chromosome in CV_FOLD_CHROMOSOMES:
            held = sorted(b for b, c in chromosome_of.items() if c == chromosome)
            train = sorted(b for b in chromosome_of if b not in set(held))
            for decision in reference_decisions(
                name,
                utility=utility,
                held_bams=held,
                training_bams=train,
                chromosome_of=chromosome_of,
                outer_fold=chromosome,
            ):
                content = decision.content()
                per_bam[str(content["dataset_id"])] = {f: content[f] for f in fields}
        out[name] = per_bam
    return out


def _finite(value: Any) -> bool:
    import math

    return isinstance(value, (int, float)) and math.isfinite(float(value))


def verify_published_l2g_v2_train_campaign(
    output_dir: Any, *, repository_root: Any = None
) -> dict[str, Any]:
    """Authenticate a published v2 tree against SOURCE authorities and recompute its numbers.

    Self-consistency is not enough: a result can agree with itself perfectly and still describe a
    different protocol, a different dataset, or metrics nobody can reproduce. So the committed
    pre-fit authority is re-hashed, the execution commit is checked against Git, and every metric
    and the shortlist are recomputed from the published records before anything is believed.
    """
    import math

    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_CONFIG_ENCODING_IDENTITY,
        ACCEPTED_FEATURE_MATRIX_HASH,
        ACCEPTED_FEATURE_SET_HASH,
        ACCEPTED_FINALIST_DOMAIN_HASH,
        ACCEPTED_RELATIVE_CONTRACT_HASH,
        ACCEPTED_RELATIVE_DATASET_IDENTITY,
        verify_v2_prefit_authority,
    )
    from minos_engine.models.relative_finalist_contract import ALTERNATIVE_FINALISTS
    from minos_engine.models.relative_finalist_dataset import (
        observed_bam_chromosome_set_hash,
        observed_relative_cell_set_hash,
    )
    from minos_engine.models.relative_finalist_protocol import (
        V2_CANDIDATE_GRID,
        build_v2_spec_content,
        build_v2_spec_hashes,
        compute_relative_protocol_hash,
    )
    from minos_engine.models.relative_finalist_reconstruction import (
        build_frozen_scientific_reference,
    )
    from minos_engine.models.runtime import compute_training_runtime_hash
    from minos_engine.qualification.git_tree import commit_tree_sha, is_commit
    from minos_engine.qualification.l2f_accepted_identities import (
        repository_root as _repo_root,
    )

    base = Path(repository_root) if repository_root is not None else _repo_root()
    root = Path(output_dir)
    result_path = root / V2_OUTPUT_LAYOUT["campaign_result"]
    _require(result_path.is_file() and not result_path.is_symlink(), f"{result_path} is missing")
    result = json.loads(result_path.read_bytes())
    verify_v2_campaign_result(result)

    # --- A/B: the committed authority, by bytes and by meaning ------------------------- #
    authority_sha = verify_v2_prefit_authority(base)
    _require(
        result["prefit_authority_sha256"] == authority_sha,
        "the result cites a different pre-fit authority than the committed one",
    )
    authority = json.loads((base / "reports/layer2/l2g-v2-prefit-authority.json").read_bytes())

    # --- C/D: the execution source, via Git --------------------------------------------- #
    commit = str(result["execution_source_commit"])
    _require(is_commit(base, commit), f"source_commit {commit} is not a commit here")
    _require(
        commit_tree_sha(base, commit) == result["execution_source_tree"],
        "the recorded source tree is not that commit's actual tree",
    )

    # --- E-O: exact source authorities --------------------------------------------------- #
    for field, expected in (
        ("relative_protocol_hash", compute_relative_protocol_hash()),
        ("relative_dataset_identity", ACCEPTED_RELATIVE_DATASET_IDENTITY),
        ("relative_contract_hash", ACCEPTED_RELATIVE_CONTRACT_HASH),
        ("finalist_domain_hash", ACCEPTED_FINALIST_DOMAIN_HASH),
        ("feature_set_hash", ACCEPTED_FEATURE_SET_HASH),
        ("feature_matrix_hash", ACCEPTED_FEATURE_MATRIX_HASH),
        ("config_encoding_identity", ACCEPTED_CONFIG_ENCODING_IDENTITY),
        ("training_runtime_hash", compute_training_runtime_hash()),
    ):
        _require(
            result.get(field) == expected,
            f"the result's {field} is {result.get(field)!r}, expected {expected}",
        )
    _require(
        list(result["candidate_spec_hashes"])
        == list(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)),
        "the result does not bind the accepted four spec identities",
    )
    expected_cells = authority["expected_relative_cell_set_hash"]
    expected_bams = authority["expected_bam_chromosome_set_hash"]

    # --- the frozen scientific truth, rebuilt from bundle bytes, never from the evidence ---- #
    frozen = build_frozen_scientific_reference(root=base)
    _require(
        frozen.dataset_identity == ACCEPTED_RELATIVE_DATASET_IDENTITY,
        "the reconstructed relative dataset is not the accepted one",
    )
    frozen_delta = frozen.delta
    frozen_utility = frozen.utility
    frozen_chromosome = frozen.chromosome_of
    _require(
        observed_relative_cell_set_hash(frozen_delta) == expected_cells
        and observed_bam_chromosome_set_hash(frozen_chromosome.items()) == expected_bams,
        "the reconstructed dataset does not reproduce the authority's expected set hashes",
    )
    accepted_specs = {
        h: build_v2_spec_content(recipe, dataset_identity=ACCEPTED_RELATIVE_DATASET_IDENTITY)
        for recipe, h in zip(
            V2_CANDIDATE_GRID,
            build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY),
            strict=True,
        )
    }

    # --- §14 thread evidence: single-threaded determinism, or the numbers are not reproducible - #
    pools = list(result.get("thread_report") or ())
    _require(bool(pools), "the campaign published no thread report")
    for pool in pools:
        _require("num_threads" in pool, "a thread pool entry records no num_threads")
        threads = pool["num_threads"]
        _require(
            isinstance(threads, int) and not isinstance(threads, bool) and threads == 1,
            f"a scientific thread pool ran with num_threads={threads!r}, not 1",
        )

    # --- per-spec: recompute everything from the artifacts ------------------------------ #
    accounted: set[str] = set()
    recomputed_metrics: dict[str, dict[str, float]] = {}
    for entry in result["per_spec"]:
        spec_hash = entry["spec_hash"]
        # §13: family and implementation are DERIVED from the accepted spec, never believed
        _require(spec_hash in accepted_specs, f"{spec_hash} is not one of the four frozen specs")
        accepted = accepted_specs[spec_hash]
        _require(
            entry.get("family") == accepted["family"],
            f"{spec_hash} claims family {entry.get('family')!r}, not {accepted['family']!r}",
        )
        _require(
            entry.get("implementation") == accepted["implementation"],
            f"{spec_hash} claims a different implementation than its accepted spec",
        )
        if entry["status"] != STATUS_COMPLETE:
            # §15: a failed candidate is accounted for and inert -- no artifacts, no metrics,
            # no shortlist. Its failure record was already checked structurally above.
            for stem in (V2_OUTPUT_LAYOUT["oof_dir"], V2_OUTPUT_LAYOUT["metrics_dir"]):
                _require(
                    not (root / stem / f"{spec_hash}.json").exists(),
                    f"{spec_hash} failed but has a scientific artifact",
                )
            _require(
                spec_hash not in result["shortlist"],
                f"{spec_hash} failed but is in the shortlist",
            )
            continue
        accounted.add(spec_hash)
        oof_path = root / V2_OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json"
        metric_path = root / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json"
        for path, sha in (
            (oof_path, entry["oof_file_sha256"]),
            (metric_path, entry["metric_file_sha256"]),
        ):
            _require(path.is_file() and not path.is_symlink(), f"{path} is missing")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            _require(actual == sha, f"{path.name} hashes to {actual}, expected {sha}")

        oof = json.loads(oof_path.read_bytes())
        _require(oof["model_spec_hash"] == spec_hash, "an OOF artifact describes another spec")
        _require(
            v2_oof_artifact_identity(oof) == entry["oof_scientific_hash"],
            "the OOF artifact's recomputed identity does not match the result",
        )
        records, decisions = list(oof["records"]), list(oof["decisions"])
        _require(len(records) == EXPECTED_OOF_RECORDS, "the artifact lacks 150 records")
        _require(len(decisions) == EXPECTED_DECISIONS, "the artifact lacks 50 decisions")
        _require(len(oof["margins"]) == EXPECTED_MARGINS, "the artifact lacks 5 margins")
        for value in oof["margins"].values():
            _require(_finite(value), "a published margin is not finite")
        # --- §7: every published label is the FROZEN label, not merely a finite number ----- #
        predicted_by_cell: dict[tuple[str, str], float] = {}
        for record in records:
            _require(record["spec_hash"] == spec_hash, "a record cites another spec")
            _require(
                _finite(record["actual_delta"]) and _finite(record["predicted_delta"]),
                "a published advantage is not finite",
            )
            cell = (record["dataset_id"], record["alternative_config"])
            _require(cell in frozen_delta, f"{spec_hash}: {cell} is not a frozen relative cell")
            _require(
                float(record["actual_delta"]) == frozen_delta[cell],
                f"{spec_hash}: the published actual_delta for {cell} is "
                f"{record['actual_delta']!r}, but the frozen label is {frozen_delta[cell]!r}",
            )
            _require(
                record["chromosome"] == frozen_chromosome[record["dataset_id"]]
                and record["outer_fold"] == frozen_chromosome[record["dataset_id"]],
                f"{spec_hash}: a record's chromosome or fold is not the frozen assignment",
            )
            predicted_by_cell[cell] = float(record["predicted_delta"])

        # --- §8/§9: the decisions must FOLLOW from those predictions and the frozen utilities - #
        for decision in decisions:
            bam = decision["dataset_id"]
            _require(decision["spec_hash"] == spec_hash, "a decision cites another spec")
            _require(
                decision["chromosome"] == frozen_chromosome[bam]
                and decision["outer_fold"] == frozen_chromosome[bam],
                f"{spec_hash}: a decision's chromosome or fold is not the frozen assignment",
            )
            for field in (
                "regret",
                "selected_utility",
                "safe_utility",
                "oracle4_utility",
                "actual_selected_delta",
            ):
                _require(_finite(decision[field]), f"{field} is not finite")

            published_predictions = dict(decision["predictions"])
            _require(
                sorted(published_predictions) == sorted(ALTERNATIVE_FINALISTS),
                f"{spec_hash}: {bam}'s decision does not carry exactly the three alternatives",
            )
            for config, value in published_predictions.items():
                _require(
                    float(value) == predicted_by_cell[(bam, config)],
                    f"{spec_hash}: {bam}'s decision prediction for {config} is not the "
                    "prediction its own OOF record published",
                )
            _require(
                float(decision["margin"]) == float(oof["margins"][frozen_chromosome[bam]]),
                f"{spec_hash}: {bam}'s decision margin is not its outer fold's published margin",
            )
            # the frozen switch rule, re-applied: ties to the lowest config hash, strict >
            best = max(published_predictions.values())
            argmax = sorted(c for c, v in published_predictions.items() if float(v) == best)[0]
            switched = float(best) > float(decision["margin"])
            selected = argmax if switched else SAFE_BASELINE_CONFIG_HASH
            _require(
                decision["selected_config"] == selected and bool(decision["switched"]) is switched,
                f"{spec_hash}: {bam}'s published action does not follow from its own "
                "predictions and margin under the frozen switch rule",
            )
            expected_decision = _decision_utilities(frozen_utility, bam=bam, selected=selected)
            for field, value in expected_decision.items():
                _require(
                    float(decision[field]) == value,
                    f"{spec_hash}: {bam}'s {field} is {decision[field]!r}, but the frozen "
                    f"utilities give {value!r}",
                )
        # RECOMPUTED, never read from the result's booleans
        _require(
            observed_relative_cell_set_hash(
                (r["dataset_id"], r["alternative_config"]) for r in records
            )
            == expected_cells,
            f"{spec_hash}: the published cells are not the frozen 150",
        )
        _require(
            observed_bam_chromosome_set_hash((d["dataset_id"], d["chromosome"]) for d in decisions)
            == expected_bams,
            f"{spec_hash}: the published decisions are not the frozen 50 BAMs",
        )

        metric = json.loads(metric_path.read_bytes())
        _require(metric["model_spec_hash"] == spec_hash, "a metric artifact describes another spec")
        _require(
            v2_metric_artifact_identity(metric) == entry["metric_scientific_hash"],
            "the metric artifact's recomputed identity does not match the result",
        )
        fresh = _recompute_policy_metrics(decisions)
        for name, value in fresh.items():
            recorded = metric["metrics"].get(name)
            if value is None or recorded is None:
                _require(value is None and recorded is None, f"{name} disagrees on availability")
                continue
            _require(
                float(recorded) == float(value),
                f"{spec_hash}: {name} is {recorded} in the artifact but recomputes to {value}",
            )
        # --- §12: diagnostics RECOMPUTED from the 150 records, not merely shape-checked ---- #
        diagnostics = metric.get("diagnostics") or {}
        _require(bool(diagnostics), f"{spec_hash} is COMPLETE with empty diagnostics")
        fresh_diagnostics = _recompute_delta_diagnostics(records)
        _require(
            sorted(diagnostics) == sorted(fresh_diagnostics),
            f"{spec_hash} publishes a different set of diagnostics than the frozen definition",
        )
        for name, value in fresh_diagnostics.items():
            recorded = diagnostics[name]
            if value is None:
                # null exactly where the statistic is undefined: a constant predictor has no rank
                # correlation, and a number there would be an invention
                _require(
                    recorded is None,
                    f"{spec_hash}: {name} is {recorded!r} but the recomputation is undefined",
                )
                continue
            _require(
                _finite(recorded) and float(recorded) == float(value),
                f"{spec_hash}: {name} is {recorded!r} in the artifact but recomputes to {value!r}",
            )
        recomputed_metrics[spec_hash] = {
            "mean_regret": float(fresh["mean_regret"]),
            "cvar_regret": float(fresh["cvar_regret"]),
        }
        for name in ("mean_regret", "cvar_regret"):
            _require(
                float(entry["promotion_metrics"][name]) == recomputed_metrics[spec_hash][name],
                f"{spec_hash}: {name} in the result differs from the recomputed value",
            )

    # --- §10: the three references, REGENERATED from the frozen utilities ---------------- #
    # Stored reference decisions are not evidence of anything on their own: a fabricated SAFE
    # policy with a comfortable regret would set a comfortable promotion bar. So each reference
    # is regenerated here from the frozen utility table and the frozen folds, and the stored
    # decisions must equal what the regeneration produces, field for field.
    regenerated = _regenerate_reference_decisions(frozen_utility, frozen_chromosome)
    for name, expected_decisions in regenerated.items():
        stored = list(result["reference_decisions"].get(name) or ())
        _require(
            len(stored) == EXPECTED_DECISIONS,
            f"the {name} reference has {len(stored)} decisions, expected {EXPECTED_DECISIONS}",
        )
        by_bam = {d["dataset_id"]: d for d in stored}
        _require(len(by_bam) == EXPECTED_DECISIONS, f"the {name} reference repeats a BAM")
        for bam, want in expected_decisions.items():
            got = by_bam.get(bam)
            _require(got is not None, f"the {name} reference has no decision for {bam}")
            assert got is not None
            for field, value in want.items():
                _require(
                    got.get(field) == value,
                    f"the {name} reference's {field} for {bam} is {got.get(field)!r}, but the "
                    f"frozen utilities give {value!r}",
                )
    safe = result["reference_decisions"]["ALWAYS_SAFE_BASELINE"]
    safe_metrics = _recompute_policy_metrics(safe)
    _require(safe_metrics["switch_fraction"] == 0.0, "the SAFE reference has a non-zero switch")
    _require(
        float(result["safe_baseline_mean_regret"]) == float(safe_metrics["mean_regret"])
        and float(result["safe_baseline_cvar_regret"]) == float(safe_metrics["cvar_regret"]),
        "the recorded SAFE bar is not what its own decisions give",
    )
    for name, stored_metrics in result["reference_metrics"].items():
        fresh_reference = _recompute_policy_metrics(result["reference_decisions"][name])
        for metric_name, value in fresh_reference.items():
            recorded = stored_metrics.get(metric_name)
            if value is None or recorded is None:
                _require(
                    value is None and recorded is None,
                    f"the {name} reference's {metric_name} disagrees on availability",
                )
                continue
            _require(
                float(recorded) == float(value),
                f"the {name} reference's {metric_name} is {recorded} but recomputes to {value}",
            )
    _require(
        all(
            math.isclose(float(d["regret"]), 0.0, abs_tol=1e-12)
            for d in result["reference_decisions"]["ORACLE4"]
        ),
        "ORACLE4 has non-zero regret",
    )

    rederived = sorted(
        h
        for h, m in recomputed_metrics.items()
        if qualifies_against_bar(
            mean_regret=m["mean_regret"],
            cvar_regret=m["cvar_regret"],
            bar_mean=float(safe_metrics["mean_regret"]),
            bar_cvar=float(safe_metrics["cvar_regret"]),
        )
    )
    _require(
        sorted(result["shortlist"]) == rederived,
        f"the recorded shortlist is not what the recomputed metrics give: {rederived}",
    )

    # --- §13 exact whole-tree layout ---------------------------------------------------- #
    expected_paths = {result_path.resolve()}
    for spec_hash in accounted:
        expected_paths.add((root / V2_OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json").resolve())
        expected_paths.add((root / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json").resolve())
    for path in sorted(root.rglob("*")):
        _require(not path.is_symlink(), f"{path} is a symlink")
        if path.is_dir():
            _require(
                path.name in (V2_OUTPUT_LAYOUT["oof_dir"], V2_OUTPUT_LAYOUT["metrics_dir"]),
                f"unexpected directory {path.name}",
            )
            _require(oct(path.stat().st_mode & 0o777) == "0o750", f"{path} has unexpected mode")
            continue
        _require(path.resolve() in expected_paths, f"unexpected file {path.name}")
        _require(oct(path.stat().st_mode & 0o777) == "0o640", f"{path} has unexpected mode")

    return {
        "ok": True,
        "output_dir": str(root),
        "complete_spec_count": len(accounted),
        "shortlist_size": len(rederived),
        "campaign_result_file_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "campaign_result_identity": v2_campaign_result_identity(result),
    }


_VERIFIED_TOKEN: Final = object()


class VerifiedPublishedL2GV2TrainCampaign:
    """A published v2 tree that has been authenticated end to end.

    Minted only by :func:`load_verified_published_l2g_v2_campaign`, which runs the whole-tree
    verifier first. The future deployment bundle takes this rather than an in-memory campaign, so
    the residuals it uses are the ones an independent verifier already checked.
    """

    __slots__ = ("_per_spec", "_result", "_root")

    def __init__(
        self,
        token: object,
        *,
        result: dict[str, Any],
        root: Path,
        per_spec: dict[str, dict[str, Any]],
    ) -> None:
        if token is not _VERIFIED_TOKEN:
            raise V2EvidenceError(
                "a verified published campaign may only be minted by the offline verifier; a "
                "dictionary has not been verified against anything"
            )
        self._result = copy.deepcopy(result)
        self._root = root
        self._per_spec = copy.deepcopy(per_spec)

    @property
    def result(self) -> dict[str, Any]:
        return copy.deepcopy(self._result)

    @property
    def shortlist(self) -> tuple[str, ...]:
        return tuple(self._result["shortlist"])

    def oof_scientific_hash(self, spec_hash: str) -> str:
        try:
            return str(self._per_spec[spec_hash]["oof_scientific_hash"])
        except KeyError:
            raise V2EvidenceError(f"{spec_hash} has no verified OOF artifact") from None

    def oof_residuals(self, spec_hash: str) -> list[float]:
        """The exact residuals from the VERIFIED artifact, not from anyone's memory."""
        try:
            records = self._per_spec[spec_hash]["records"]
        except KeyError:
            raise V2EvidenceError(f"{spec_hash} has no verified OOF artifact") from None
        return [abs(float(r["predicted_delta"]) - float(r["actual_delta"])) for r in records]


def load_verified_published_l2g_v2_campaign(
    output_dir: Any, *, repository_root: Any = None
) -> VerifiedPublishedL2GV2TrainCampaign:
    """Verify a published tree, then mint the capability that proves it was verified."""
    root = Path(output_dir)
    report = verify_published_l2g_v2_train_campaign(root, repository_root=repository_root)
    _require(bool(report["ok"]), "the published tree did not verify")
    result = json.loads((root / V2_OUTPUT_LAYOUT["campaign_result"]).read_bytes())
    per_spec: dict[str, dict[str, Any]] = {}
    for entry in result["per_spec"]:
        if entry["status"] != STATUS_COMPLETE:
            continue
        oof = json.loads(
            (root / V2_OUTPUT_LAYOUT["oof_dir"] / f"{entry['spec_hash']}.json").read_bytes()
        )
        per_spec[entry["spec_hash"]] = {
            "oof_scientific_hash": entry["oof_scientific_hash"],
            "records": oof["records"],
        }
    return VerifiedPublishedL2GV2TrainCampaign(
        _VERIFIED_TOKEN, result=result, root=root, per_spec=per_spec
    )
