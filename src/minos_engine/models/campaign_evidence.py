"""Serialize, publish and independently re-verify the TRAIN OOF campaign's evidence.

Hashing records and then discarding them leaves an ``oof_artifact_hash`` with nothing behind it:
a claim about evidence that no longer exists. So the trusted campaign retains its records until
these functions write them, and every hash the campaign result carries is computed from bytes
that are actually on disk.

Two identities are kept distinct throughout. The SCIENTIFIC identity is domain-separated over
canonical content and is what makes an artifact the artifact; the FILE SHA-256 is what makes those
particular bytes the ones that were published. A verifier that only checked one would miss either
a re-encoded file or a swapped one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.oof_runner import metric_artifact_identity

__all__ = [
    "METRIC_ARTIFACT_SCHEMA",
    "OOF_ARTIFACT_SCHEMA",
    "OUTPUT_LAYOUT",
    "CampaignEvidenceError",
    "load_and_verify_metric_artifact",
    "load_and_verify_oof_artifact",
    "metric_artifact_content",
    "oof_artifact_content",
    "write_l2g_train_campaign_outputs",
]

OOF_ARTIFACT_SCHEMA: Final = "l2g-oof-artifact-v1"
OOF_ARTIFACT_DOMAIN: Final = "minos:l2g-oof-artifact:v1\n"
METRIC_ARTIFACT_SCHEMA: Final = "l2g-metric-artifact-v1"
#: the record identity domain is already frozen in ``oof_runner``; this is the ARTIFACT wrapper
MEDIA_TYPE: Final = "application/json"

EXPECTED_RECORD_COUNT: Final = 1040
EXPECTED_BAM_COUNT: Final = 50

#: frozen so the real run cannot invent a layout on the day
OUTPUT_LAYOUT: Final[dict[str, str]] = {
    "root": "minos_l2g_train_oof",
    "campaign_result": "campaign-result.json",
    "oof_dir": "oof",
    "metrics_dir": "metrics",
    "failures_dir": "failures",
    "dir_mode": "0o750",
    "file_mode": "0o640",
}
_DIR_MODE: Final = 0o750
_FILE_MODE: Final = 0o640

#: promotion is decided on exactly these two; they are bound into the result so an offline
#: verifier can re-derive the shortlist without trusting anyone's arithmetic
PROMOTION_METRICS: Final[tuple[str, ...]] = ("mean_regret", "cvar_regret")


class CampaignEvidenceError(MinosEngineError):
    """Campaign evidence could not be published or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CampaignEvidenceError(message)


def _jsonable(value: Any) -> Any:
    """Canonical JSON carries no NaN; an unavailable diagnostic is null, never zero."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value


# ---------------------------------------------------------------------------------------- #
# canonical content
# ---------------------------------------------------------------------------------------- #
def oof_artifact_content(
    *,
    records: list[Any],
    spec_hash: str,
    family: str,
    training_dataset_hash: str,
    cv_manifest_hash: str,
    expected_cell_set: frozenset[tuple[str, str]],
) -> dict[str, Any]:
    """One COMPLETE spec's out-of-fold evidence, in canonical, order-independent form."""
    rows = [_jsonable(r.content()) for r in records]
    _require(
        bool(rows) and len(rows) == len(records),
        "an OOF artifact cannot be built from an empty record set",
    )
    for row in rows:
        _require(
            row["model_spec_hash"] == spec_hash,
            f"a record cites spec {row['model_spec_hash']}, not {spec_hash}",
        )
        _require(row["family"] == family, "a record cites a different family")
    cells = [(str(r["dataset_id"]), str(r["config_hash"])) for r in rows]
    _require(len(set(cells)) == len(cells), "the OOF records repeat a scientific cell")
    _require(
        frozenset(cells) == expected_cell_set,
        "the OOF records are not the frozen scientific cell set",
    )
    # sorted, so re-reading the same evidence in a different order is the same artifact
    ordered = sorted(rows, key=lambda r: (r["dataset_id"], r["config_hash"]))
    return {
        "schema_version": OOF_ARTIFACT_SCHEMA,
        "model_spec_hash": spec_hash,
        "family": family,
        "training_dataset_hash": training_dataset_hash,
        "cv_manifest_hash": cv_manifest_hash,
        "record_count": len(ordered),
        "bam_count": len({c[0] for c in cells}),
        "cell_set_identity": sha256_hex(canonical_json_bytes(sorted(cells))),
        "outer_folds": sorted({str(r["outer_fold"]) for r in rows}),
        "records": ordered,
    }


def oof_artifact_identity(content: dict[str, Any]) -> str:
    """Domain-separated scientific identity of a whole OOF artifact."""
    return sha256_hex(OOF_ARTIFACT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def metric_artifact_content(
    *, metrics: dict[str, Any], spec_hash: str, family: str, training_dataset_hash: str
) -> dict[str, Any]:
    """One COMPLETE spec's metrics, bound to the spec they describe."""
    for name in PROMOTION_METRICS:
        _require(name in metrics, f"the metric set is missing {name}")
    return {
        "schema_version": METRIC_ARTIFACT_SCHEMA,
        "model_spec_hash": spec_hash,
        "family": family,
        "training_dataset_hash": training_dataset_hash,
        "regret_orientation": "ORACLE_MINUS_SELECTED_LOWER_IS_BETTER",
        "cvar_alpha": 0.25,
        "cvar_tail_rule": "CEIL_ALPHA_TIMES_N",
        "metrics": _jsonable(metrics),
    }


# ---------------------------------------------------------------------------------------- #
# publication
# ---------------------------------------------------------------------------------------- #
def _write_atomic(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical bytes, written through a temp file so a crash leaves no half artifact."""
    data = canonical_json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, _DIR_MODE)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, _FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {
        "path": str(path),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": MEDIA_TYPE,
    }


def write_l2g_train_campaign_outputs(
    trusted: Any,
    *,
    output_dir: Any,
) -> dict[str, Any]:
    """Publish the campaign's evidence and build its canonical result from the WRITTEN bytes.

    Accepts the trusted campaign and a destination. It takes no shortlist, threshold, metric,
    hash, spec list or status from the caller: every one of those is read out of the campaign that
    actually ran, and every recorded file hash is computed from the file that was actually
    written.
    """
    from minos_engine.models.campaign import STATUS_COMPLETE, TrustedL2GTrainCampaign
    from minos_engine.models.shortlist import build_campaign_result

    _require(
        isinstance(trusted, TrustedL2GTrainCampaign),
        "only a trusted campaign minted by the sealed production entry may be published",
    )
    root = Path(output_dir)
    closure = trusted.closure
    authority = closure["authority"]
    expected_cell_set = frozenset((str(a), str(b)) for a, b in closure["expected_cell_set"])

    written: list[Path] = []
    published: dict[str, dict[str, Any]] = {}
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, _DIR_MODE)
        for spec_hash in trusted.complete_spec_hashes():
            entry = trusted.spec_entry(spec_hash)
            _require(entry["status"] == STATUS_COMPLETE, f"{spec_hash} is not COMPLETE")
            oof = oof_artifact_content(
                records=trusted.records_for(spec_hash),
                spec_hash=spec_hash,
                family=entry["family"],
                training_dataset_hash=authority["training_dataset_hash"],
                cv_manifest_hash=authority["cv_manifest_hash"],
                expected_cell_set=expected_cell_set,
            )
            metrics = trusted.metrics_for(spec_hash)
            metric = metric_artifact_content(
                metrics=metrics,
                spec_hash=spec_hash,
                family=entry["family"],
                training_dataset_hash=authority["training_dataset_hash"],
            )
            oof_path = root / OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json"
            metric_path = root / OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json"
            oof_file = _write_atomic(oof_path, oof)
            written.append(oof_path)
            metric_file = _write_atomic(metric_path, metric)
            written.append(metric_path)
            published[spec_hash] = {
                "oof_scientific_hash": oof_artifact_identity(oof),
                "oof_file_sha256": oof_file["file_sha256"],
                "oof_size_bytes": oof_file["size_bytes"],
                "metric_scientific_hash": metric_artifact_identity(metrics, spec_hash=spec_hash),
                "metric_file_sha256": metric_file["file_sha256"],
                "metric_size_bytes": metric_file["size_bytes"],
                "media_type": MEDIA_TYPE,
                "promotion_metrics": {n: float(metrics[n]) for n in PROMOTION_METRICS},
            }

        for spec_hash in closure["per_spec"]:
            failures = trusted.failures_for(spec_hash)
            if not failures:
                continue
            failure_path = root / OUTPUT_LAYOUT["failures_dir"] / f"{spec_hash}.json"
            _write_atomic(
                failure_path,
                {
                    "schema_version": "l2g-training-failure-v1",
                    "model_spec_hash": spec_hash,
                    "family": trusted.spec_entry(spec_hash)["family"],
                    "successful_outer_fold_count": trusted.spec_entry(spec_hash)[
                        "successful_outer_fold_count"
                    ],
                    "failures": _jsonable(failures),
                },
            )
            written.append(failure_path)

        result = build_campaign_result(trusted=trusted, published=published)
        result_path = root / OUTPUT_LAYOUT["campaign_result"]
        result_file = _write_atomic(result_path, result)
        written.append(result_path)
    except BaseException:
        # a partially published campaign is worse than none: it looks like evidence
        for path in written:
            path.unlink(missing_ok=True)
        raise

    return {
        "output_dir": str(root),
        "campaign_result_path": str(result_path),
        "campaign_result_file_sha256": result_file["file_sha256"],
        "published": published,
        "files": [str(p) for p in written],
    }


# ---------------------------------------------------------------------------------------- #
# offline verification of the written evidence
# ---------------------------------------------------------------------------------------- #
def _read_verified(path: Path, *, expected_sha: str) -> dict[str, Any]:
    _require(path.is_file(), f"{path} is not a regular file")
    _require(not path.is_symlink(), f"{path} is a symlink")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    _require(actual == expected_sha, f"{path.name} hashes to {actual}, expected {expected_sha}")
    payload = json.loads(data)
    _require(isinstance(payload, dict), f"{path.name} is not a JSON object")
    return dict(payload)


def load_and_verify_oof_artifact(
    path: Any,
    *,
    spec_hash: str,
    family: str,
    expected_file_sha256: str,
    expected_scientific_hash: str,
    expected_cell_set: frozenset[tuple[str, str]],
    training_dataset_hash: str,
    cv_manifest_hash: str,
) -> dict[str, Any]:
    """Re-derive an OOF artifact's identity from its bytes. This is what catches a file swap."""
    payload = _read_verified(Path(path), expected_sha=expected_file_sha256)
    _require(
        payload.get("schema_version") == OOF_ARTIFACT_SCHEMA,
        f"unexpected OOF schema {payload.get('schema_version')!r}",
    )
    _require(
        payload.get("model_spec_hash") == spec_hash,
        f"the artifact describes {payload.get('model_spec_hash')}, not {spec_hash}",
    )
    _require(payload.get("family") == family, "the artifact cites a different family")
    _require(
        payload.get("training_dataset_hash") == training_dataset_hash,
        "the artifact cites a foreign training dataset",
    )
    _require(
        payload.get("cv_manifest_hash") == cv_manifest_hash,
        "the artifact cites a foreign CV manifest",
    )
    records = payload.get("records")
    _require(isinstance(records, list), "the artifact carries no records")
    rows: list[dict[str, Any]] = list(records or [])
    _require(
        len(rows) == EXPECTED_RECORD_COUNT,
        f"the artifact holds {len(rows)} records, expected {EXPECTED_RECORD_COUNT}",
    )
    cells = [(str(r["dataset_id"]), str(r["config_hash"])) for r in rows]
    _require(len(set(cells)) == len(cells), "the artifact repeats a scientific cell")
    _require(frozenset(cells) == expected_cell_set, "the artifact is not the frozen cell set")
    _require(
        len({c[0] for c in cells}) == EXPECTED_BAM_COUNT,
        f"the artifact covers {len({c[0] for c in cells})} BAMs, expected {EXPECTED_BAM_COUNT}",
    )
    folds = {str(r["outer_fold"]) for r in rows}
    _require(
        folds == set(CV_FOLD_CHROMOSOMES),
        f"the artifact covers folds {sorted(folds)}, expected the five chromosomes",
    )
    for row in rows:
        _require(
            row.get("model_spec_hash") == spec_hash,
            "a record inside the artifact cites a different spec",
        )
        _require(row.get("family") == family, "a record inside the artifact cites another family")
    recomputed = oof_artifact_identity(payload)
    _require(
        recomputed == expected_scientific_hash,
        f"the artifact's recomputed identity {recomputed} does not match the campaign result's "
        f"{expected_scientific_hash}",
    )
    return payload


def load_and_verify_metric_artifact(
    path: Any,
    *,
    spec_hash: str,
    family: str,
    expected_file_sha256: str,
    expected_scientific_hash: str,
    expected_promotion_metrics: dict[str, float],
    training_dataset_hash: str,
) -> dict[str, Any]:
    """Re-derive a metric artifact's identity and require it to describe THIS spec."""
    payload = _read_verified(Path(path), expected_sha=expected_file_sha256)
    _require(
        payload.get("schema_version") == METRIC_ARTIFACT_SCHEMA,
        f"unexpected metric schema {payload.get('schema_version')!r}",
    )
    _require(
        payload.get("model_spec_hash") == spec_hash,
        f"the metric artifact describes {payload.get('model_spec_hash')}, not {spec_hash}",
    )
    _require(payload.get("family") == family, "the metric artifact cites a different family")
    _require(
        payload.get("training_dataset_hash") == training_dataset_hash,
        "the metric artifact cites a foreign training dataset",
    )
    metrics = payload.get("metrics")
    _require(isinstance(metrics, dict), "the metric artifact carries no metrics")
    recomputed = metric_artifact_identity(dict(metrics or {}), spec_hash=spec_hash)
    _require(
        recomputed == expected_scientific_hash,
        "the metric artifact's recomputed identity does not match the campaign result's",
    )
    for name, expected in expected_promotion_metrics.items():
        actual = (metrics or {}).get(name)
        _require(
            actual is not None and float(actual) == float(expected),
            f"{name} in the artifact is {actual}, but the campaign result records {expected}",
        )
    return payload
