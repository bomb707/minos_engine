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
from minos_engine.models.oof_runner import metric_artifact_identity
from minos_engine.models.oof_runner import (
    oof_artifact_identity as scientific_oof_artifact_identity,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root
from minos_engine.qualification.provenance import read_provenance

__all__ = [
    "METRIC_ARTIFACT_SCHEMA",
    "OOF_ARTIFACT_SCHEMA",
    "OUTPUT_LAYOUT",
    "CampaignEvidenceError",
    "TrustedL2GPublishedEvidence",
    "verify_published_l2g_train_campaign",
    "load_and_verify_metric_artifact",
    "load_and_verify_oof_artifact",
    "metric_artifact_content",
    "oof_artifact_content",
    "oof_wrapper_identity",
    "write_l2g_train_campaign_outputs",
]

OOF_ARTIFACT_SCHEMA: Final = "l2g-oof-artifact-v1"
#: The FILE wrapper's own domain. The frozen OOF record-set domain already belongs to
#: ``oof_runner.oof_artifact_identity``, which hashes the record SET; two different
#: canonicalizations must never share one scientific identity namespace, or "the OOF identity"
#: silently means whichever function the reader happens to be looking at.
OOF_WRAPPER_DOMAIN: Final = "minos:l2g-oof-evidence-wrapper:v1\n"
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
        # THE frozen scientific identity, recomputed from the records themselves
        "scientific_oof_hash": scientific_oof_artifact_identity(records),
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


def oof_wrapper_identity(content: dict[str, Any]) -> str:
    """Identity of the FILE wrapper, under its own domain.

    This is packaging, not science. The scientific identity of the evidence is
    ``oof_runner.oof_artifact_identity(records)``, which the wrapper carries as
    ``scientific_oof_hash``.
    """
    return sha256_hex(OOF_WRAPPER_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


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
    """Canonical bytes, written atomically and then READ BACK from the final path.

    Hashing the in-memory buffer proves what was intended to be written, not what the filesystem
    now holds. Everything downstream treats the returned SHA as a fact about the file, so it has
    to come from the file.
    """
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

    _require(path.is_file(), f"{path} is not a regular file after writing")
    _require(not path.is_symlink(), f"{path} is a symlink after writing")
    observed = path.read_bytes()
    _require(
        observed == expected,
        f"{path.name} on disk differs from the canonical bytes that were written",
    )
    return {
        "path": str(path),
        # derived from the FINAL file's bytes, not from the buffer
        "file_sha256": hashlib.sha256(observed).hexdigest(),
        "size_bytes": len(observed),
        "media_type": MEDIA_TYPE,
    }


#: Only the write/readback boundary holds it. A caller with a real trusted campaign could
#: otherwise hand the result builder any file hashes it liked, which would make the second half of
#: the evidence boundary caller-authored.
_EVIDENCE_MINT_TOKEN: Final = object()


class TrustedL2GPublishedEvidence:
    """What was ACTUALLY written and read back, minted only after every file verified."""

    __slots__ = ("_entries", "_output_dir")

    def __init__(
        self, token: object, *, entries: dict[str, dict[str, Any]], output_dir: str
    ) -> None:
        if token is not _EVIDENCE_MINT_TOKEN:
            raise CampaignEvidenceError(
                "published evidence may only be minted by the write/readback boundary; a "
                "caller-built dictionary is not proof that anything reached disk"
            )
        self._entries = copy.deepcopy(entries)
        self._output_dir = output_dir

    @property
    def output_dir(self) -> str:
        return self._output_dir

    def entries(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._entries)

    def spec_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))


def _verify_written_oof(
    path: Path,
    *,
    spec_hash: str,
    family: str,
    file_sha: str,
    scientific_hash: str,
    cells: frozenset[tuple[str, str]],
    training_dataset_hash: str,
    cv_manifest_hash: str,
) -> None:
    load_and_verify_oof_artifact(
        path,
        spec_hash=spec_hash,
        family=family,
        expected_file_sha256=file_sha,
        expected_scientific_hash=scientific_hash,
        expected_cell_set=cells,
        training_dataset_hash=training_dataset_hash,
        cv_manifest_hash=cv_manifest_hash,
    )


def write_l2g_train_campaign_outputs(trusted: Any, *, output_dir: Any) -> dict[str, Any]:
    """Publish campaign evidence into a STAGING tree, verify all of it, then promote atomically.

    Writing straight into the final location risks two different bad outcomes: overwriting a
    previous campaign's evidence, and leaving a half-written tree that looks like evidence. So
    everything is staged, read back, semantically verified and whole-tree verified first; only
    then is the staging directory renamed into place, and only if the final path does not exist.

    Every scientific identity is required to survive execution -> trusted memory -> file ->
    reload. A hash that changes anywhere in that chain means the evidence is not what the campaign
    produced.
    """
    from minos_engine.models.campaign import STATUS_COMPLETE, TrustedL2GTrainCampaign
    from minos_engine.models.shortlist import (
        build_campaign_result,
        verify_campaign_result,
        verify_campaign_result_source,
    )

    _require(
        isinstance(trusted, TrustedL2GTrainCampaign),
        "only a trusted campaign minted by the sealed production entry may be published",
    )
    final = Path(output_dir)
    _require(
        not final.exists(),
        f"{final} already exists; refusing to overwrite a previous campaign's evidence",
    )

    closure = trusted.closure
    authority = closure["authority"]
    cells = frozenset((str(a), str(b)) for a, b in closure["expected_cell_set"])

    # --- publication-time source consistency (the checkout must not have moved) ----------- #
    _verify_execution_source_unchanged(trusted)

    staging = final.parent / f"{final.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    _require(not staging.exists(), f"the staging path {staging} already exists")
    try:
        staging.mkdir(parents=True)
        os.chmod(staging, _DIR_MODE)
        entries: dict[str, dict[str, Any]] = {}

        for spec_hash in trusted.complete_spec_hashes():
            entry = trusted.spec_entry(spec_hash)
            _require(entry["status"] == STATUS_COMPLETE, f"{spec_hash} is not COMPLETE")
            records = trusted.records_for(spec_hash)
            metrics = trusted.metrics_for(spec_hash)

            # --- core -> trusted memory continuity ---------------------------------------- #
            scientific_oof = scientific_oof_artifact_identity(records)
            _require(
                scientific_oof == entry["oof_artifact_hash"],
                f"{spec_hash}: the retained records no longer hash to the identity the campaign "
                "core earned; the evidence was altered after execution",
            )
            scientific_metric = metric_artifact_identity(metrics, spec_hash=spec_hash)
            _require(
                scientific_metric == entry["metric_artifact_hash"],
                f"{spec_hash}: the retained metrics no longer hash to the identity the campaign "
                "core earned",
            )

            oof = oof_artifact_content(
                records=records,
                spec_hash=spec_hash,
                family=entry["family"],
                training_dataset_hash=authority["training_dataset_hash"],
                cv_manifest_hash=authority["cv_manifest_hash"],
                expected_cell_set=cells,
            )
            metric = metric_artifact_content(
                metrics=metrics,
                spec_hash=spec_hash,
                family=entry["family"],
                training_dataset_hash=authority["training_dataset_hash"],
            )
            oof_file = _write_atomic(staging / OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json", oof)
            metric_file = _write_atomic(
                staging / OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json", metric
            )

            # --- file -> reload continuity, before the evidence is admitted ---------------- #
            _verify_written_oof(
                staging / OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json",
                spec_hash=spec_hash,
                family=entry["family"],
                file_sha=oof_file["file_sha256"],
                scientific_hash=scientific_oof,
                cells=cells,
                training_dataset_hash=authority["training_dataset_hash"],
                cv_manifest_hash=authority["cv_manifest_hash"],
            )
            promotion = {n: float(metrics[n]) for n in PROMOTION_METRICS}
            load_and_verify_metric_artifact(
                staging / OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json",
                spec_hash=spec_hash,
                family=entry["family"],
                expected_file_sha256=metric_file["file_sha256"],
                expected_scientific_hash=scientific_metric,
                expected_promotion_metrics=promotion,
                training_dataset_hash=authority["training_dataset_hash"],
            )

            entries[spec_hash] = {
                "oof_scientific_hash": scientific_oof,
                "oof_wrapper_hash": oof_wrapper_identity(oof),
                "oof_file_sha256": oof_file["file_sha256"],
                "oof_size_bytes": oof_file["size_bytes"],
                "metric_scientific_hash": scientific_metric,
                "metric_file_sha256": metric_file["file_sha256"],
                "metric_size_bytes": metric_file["size_bytes"],
                "media_type": MEDIA_TYPE,
                "promotion_metrics": promotion,
            }

        for spec_hash in closure["per_spec"]:
            failures = trusted.failures_for(spec_hash)
            if not failures:
                continue
            _write_atomic(
                staging / OUTPUT_LAYOUT["failures_dir"] / f"{spec_hash}.json",
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

        evidence = TrustedL2GPublishedEvidence(
            _EVIDENCE_MINT_TOKEN, entries=entries, output_dir=str(staging)
        )
        result = build_campaign_result(trusted=trusted, published=evidence)
        result_file = _write_atomic(staging / OUTPUT_LAYOUT["campaign_result"], result)

        # --- the result file is itself read back and re-verified ------------------------- #
        reloaded = json.loads((staging / OUTPUT_LAYOUT["campaign_result"]).read_bytes())
        verify_campaign_result(reloaded)
        verify_campaign_result_source(reloaded)
        verify_published_l2g_train_campaign(staging)

        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    from minos_engine.models.shortlist import campaign_result_identity

    return {
        "output_dir": str(final),
        "campaign_result_path": str(final / OUTPUT_LAYOUT["campaign_result"]),
        "campaign_result_identity": campaign_result_identity(result),
        "campaign_result_file_sha256": result_file["file_sha256"],
        "published": evidence.entries(),
    }


def _verify_execution_source_unchanged(trusted: Any) -> None:
    """The checkout that fitted the models must still be the checkout that publishes them."""
    provenance = read_provenance(repository_root())
    _require(
        provenance.head_sha == trusted.execution_source_commit,
        f"the campaign was fitted at {trusted.execution_source_commit} but publication is "
        f"running at {provenance.head_sha}; relabelling the result as produced by a different "
        "checkout would be a lie about its provenance",
    )
    _require(
        provenance.tree_sha == trusted.execution_source_tree,
        "the source tree changed between fitting and publication",
    )
    _require(provenance.worktree_clean, "the worktree is dirty at publication time")


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


class _ReplayRecord:
    """A record rebuilt from a published artifact, so the FROZEN identity can be recomputed."""

    __slots__ = ("_content",)

    def __init__(self, content: dict[str, Any]) -> None:
        self._content = content

    def content(self) -> dict[str, Any]:
        return self._content


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
    """Re-derive the FROZEN scientific identity from the file's own records.

    The recomputation runs ``oof_runner.oof_artifact_identity`` — the same one definition the
    campaign core earned — so core, published artifact and offline reload all speak about the same
    identity rather than three lookalikes.
    """
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

    recomputed = scientific_oof_artifact_identity([_ReplayRecord(dict(r)) for r in rows])
    _require(
        recomputed == expected_scientific_hash,
        f"the artifact's recomputed scientific identity {recomputed} does not match the campaign "
        f"result's {expected_scientific_hash}",
    )
    _require(
        payload.get("scientific_oof_hash") == expected_scientific_hash,
        "the artifact's declared scientific_oof_hash disagrees with its own records",
    )
    wrapper = oof_wrapper_identity(payload)
    _require(bool(wrapper), "the wrapper identity could not be computed")
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


def verify_published_l2g_train_campaign(
    output_dir: Any, *, repository_root: Any = None
) -> dict[str, Any]:
    """Verify a whole published campaign tree offline. No TRAIN database, no trust in anyone.

    This is the verifier to run immediately after the real campaign: it re-derives every scientific
    identity from bytes, rebuilds the accepted dataset from the frozen pre-fit bundle to obtain the
    1040 expected cells, re-runs the two-bar shortlist rule, and refuses any scientific file the
    result does not account for.
    """
    from minos_engine.models.prefit_loader import load_verified_training_dataset
    from minos_engine.models.shortlist import (
        verify_campaign_result,
        verify_campaign_result_source,
        verify_prefit_authority_bytes,
    )

    root = Path(output_dir)
    result_path = root / OUTPUT_LAYOUT["campaign_result"]
    _require(result_path.is_file(), f"{result_path} is missing")
    _require(not result_path.is_symlink(), f"{result_path} is a symlink")
    result = json.loads(result_path.read_bytes())
    _require(isinstance(result, dict), "the campaign result is not a JSON object")

    verify_campaign_result(result)
    verify_campaign_result_source(result, root=repository_root)
    # the committed authority's bytes must still hash to what the result cites
    verify_prefit_authority_bytes(repository_root)

    dataset = load_verified_training_dataset(root=repository_root)
    _require(
        dataset.identity() == result["training_dataset_hash"],
        "the frozen bundle does not reconstruct the dataset the result names",
    )
    cells = frozenset((r.dataset_id, r.config_hash) for r in dataset.rows)
    _require(
        len(cells) == EXPECTED_RECORD_COUNT,
        f"the reconstructed dataset holds {len(cells)} cells",
    )

    complete: list[dict[str, Any]] = []
    for entry in result["per_spec"]:
        if "oof_scientific_hash" not in entry:
            for stem in (OUTPUT_LAYOUT["oof_dir"], OUTPUT_LAYOUT["metrics_dir"]):
                _require(
                    not (root / stem / f"{entry['spec_hash']}.json").exists(),
                    f"{entry['spec_hash']} did not complete but has a scientific artifact",
                )
            continue
        complete.append(entry)
        load_and_verify_oof_artifact(
            root / OUTPUT_LAYOUT["oof_dir"] / f"{entry['spec_hash']}.json",
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=entry["oof_file_sha256"],
            expected_scientific_hash=entry["oof_scientific_hash"],
            expected_cell_set=cells,
            training_dataset_hash=result["training_dataset_hash"],
            cv_manifest_hash=result["cv_manifest_hash"],
        )
        load_and_verify_metric_artifact(
            root / OUTPUT_LAYOUT["metrics_dir"] / f"{entry['spec_hash']}.json",
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=entry["metric_file_sha256"],
            expected_scientific_hash=entry["metric_scientific_hash"],
            expected_promotion_metrics=entry["promotion_metrics"],
            training_dataset_hash=result["training_dataset_hash"],
        )

    # a file the result does not account for is unexplained evidence, which is not evidence
    accounted = {e["spec_hash"] for e in complete}
    for stem in (OUTPUT_LAYOUT["oof_dir"], OUTPUT_LAYOUT["metrics_dir"]):
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
        "complete_spec_count": len(complete),
        "shortlist_size": len(result["shortlist"]),
        "campaign_result_file_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
    }
