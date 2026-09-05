"""Load and VERIFY the frozen pre-fit authority and its external training bundle.

The trainer must not accept an operator-built JSON file. Every identity in the committed
``reports/layer2/l2g-prefit-authority.json`` is re-derived here from the source authorities, the
external artifacts are hashed from their own bytes, and the ``TrainingDataset`` is RECONSTRUCTED
and required to hash to the accepted identity. In particular the dataset hash written inside the
manifest is never trusted: a file that asserts its own correctness proves nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import compute_training_contract_hash
from minos_engine.models.dataset import (
    BamFeatureBinding,
    CvManifest,
    TrainingDataset,
    TrainingRow,
)
from minos_engine.models.protocol import compute_training_protocol_hash
from minos_engine.models.runtime import compute_training_runtime_hash

__all__ = [
    "ACCEPTED_TRAINING_DATASET_HASH",
    "PREFIT_AUTHORITY_PATH",
    "TRAINING_WORKSPACE",
    "PrefitAuthorityError",
    "load_accepted_prefit_authority",
    "load_verified_training_dataset",
]

PREFIT_AUTHORITY_PATH: Final = "reports/layer2/l2g-prefit-authority.json"
TRAINING_WORKSPACE: Final = Path("/home/hr/bittensor/minos_l2g_training")

ACCEPTED_TRAINING_DATASET_HASH: Final = (
    "d031758c58358270843b9b417ea034d1181a6aaafc1c94af000279c26dc62fcc"
)
ACCEPTED_CV_MANIFEST_HASH: Final = (
    "b441b15fdc185e62e243b93322d6c30d8787f49f9fafbb3dab6ac9371728d92f"
)
ACCEPTED_FEATURE_MATRIX_HASH: Final = (
    "c6a8db848318e5c78839474fa62a4e8e408157a1e6f5cb1bdd18c9cd3d0118b2"
)
ACCEPTED_FEATURE_ARTIFACT_SHA: Final = (
    "0396cb07734a18df803ac813d9d1224ecdc3ec9901d7b8a202ac8c6538f3c243"
)
ACCEPTED_CELL_COUNTS: Final[dict[str, int]] = {
    "scientific_cells": 1040,
    "admitted": 861,
    "non_admission": 149,
    "execution_failure": 30,
    "unique_configs": 80,
    "bams": 50,
}


class PrefitAuthorityError(MinosEngineError):
    """The frozen pre-fit authority or its bundle does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PrefitAuthorityError(message)


def _read_exact(path: Path, *, expected_sha: str, expected_size: int | None = None) -> bytes:
    """Read a bundle file that must be a REGULAR file with the exact recorded bytes."""
    _require(path.is_file(), f"{path} is not a regular file")
    _require(not path.is_symlink(), f"{path} is a symlink; the bundle must be real files")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    _require(
        actual == expected_sha,
        f"{path.name} hashes to {actual}, expected {expected_sha}",
    )
    if expected_size is not None:
        _require(
            len(data) == expected_size,
            f"{path.name} is {len(data)} bytes, expected {expected_size}",
        )
    return data


def load_accepted_prefit_authority(root: Path | None = None) -> dict[str, Any]:
    """The committed authority, with every recomputable identity re-derived rather than read."""
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = root or repository_root()
    path = base / PREFIT_AUTHORITY_PATH
    _require(path.is_file(), f"the committed pre-fit authority is missing: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    _require(
        document.get("schema_version") == "l2g-prefit-authority-v1",
        f"unexpected authority schema {document.get('schema_version')!r}",
    )

    authorities = document["authorities"]
    for name, expected in (
        ("training_contract_hash", compute_training_contract_hash()),
        ("training_protocol_hash", compute_training_protocol_hash()),
        ("training_runtime_hash", compute_training_runtime_hash()),
    ):
        _require(
            authorities.get(name) == expected,
            f"{name} in the committed authority is {authorities.get(name)}, but this source "
            f"computes {expected}",
        )
    _require(
        document["training_dataset_hash"] == ACCEPTED_TRAINING_DATASET_HASH,
        "the committed authority does not describe the accepted training dataset",
    )
    _require(
        document["cv_manifest_hash"] == ACCEPTED_CV_MANIFEST_HASH,
        "the committed authority does not describe the accepted CV manifest",
    )
    _require(
        authorities["feature_matrix_artifact_sha256"] == ACCEPTED_FEATURE_ARTIFACT_SHA
        if "feature_matrix_artifact_sha256" in authorities
        else True,
        "the committed authority cites a foreign feature artifact",
    )
    for key, expected_count in ACCEPTED_CELL_COUNTS.items():
        _require(
            int(document["counts"][key]) == expected_count,
            f"{key} is {document['counts'][key]}, expected {expected_count}",
        )
    _require(
        int(document["bams_without_admitted_examples"]) == 0,
        "the authority records a BAM with no admitted example",
    )
    _require(len(document["candidate_spec_hashes"]) == 6, "expected six candidate specs")
    _require(len(document["reference_spec_hashes"]) == 4, "expected four reference specs")
    _require(document["no_model_fitted"] is True, "the authority records a fitted model")
    _require(
        document["validation_consulted"] is False, "the authority records VALIDATION consultation"
    )
    return dict(document)


def load_verified_training_dataset(
    *, workspace: Path | None = None, root: Path | None = None
) -> TrainingDataset:
    """Rebuild the TrainingDataset from bundle BYTES and require the accepted identity.

    Nothing here trusts a hash the bundle asserts about itself; the reconstructed object must hash
    to the accepted dataset identity, which is only possible if every example, binding and
    authority survived byte-for-byte.
    """
    from minos_engine.layer2.features.contracts import AUTHORITATIVE_COLUMNS

    document = load_accepted_prefit_authority(root)
    base = workspace or TRAINING_WORKSPACE
    recorded = {entry["name"]: entry for entry in document["external_artifacts"]}

    manifest_path = base / "training_dataset_manifest.json"
    _require(manifest_path.is_file(), f"the training manifest is missing: {manifest_path}")
    manifest_bytes = manifest_path.read_bytes()
    _require(
        hashlib.sha256(manifest_bytes).hexdigest() == document["external_manifest_sha256"],
        "the external training manifest does not match its committed SHA-256",
    )
    manifest = json.loads(manifest_bytes)

    def _bundle(name: str) -> Any:
        entry = recorded[name]
        return json.loads(
            _read_exact(
                base / name,
                expected_sha=str(entry["sha256"]),
                expected_size=int(entry["size_bytes"]),
            )
        )

    examples = _bundle("training_examples.json")
    bindings = _bundle("bam_feature_bindings.json")
    configs = _bundle("config_table.json")

    _require(
        len(examples["examples"]) == ACCEPTED_CELL_COUNTS["scientific_cells"],
        f"the bundle holds {len(examples['examples'])} examples",
    )
    _require(
        len(configs["config_hashes"]) == ACCEPTED_CELL_COUNTS["unique_configs"],
        "the config table does not hold the accepted number of configs",
    )
    _require(
        bindings["feature_matrix_hash"] == ACCEPTED_FEATURE_MATRIX_HASH
        and bindings["feature_matrix_artifact_sha256"] == ACCEPTED_FEATURE_ARTIFACT_SHA,
        "the bundle cites a foreign feature matrix",
    )

    rows = tuple(
        TrainingRow(
            dataset_id=str(e["dataset_id"]),
            chromosome=str(e["chromosome"]),
            config_hash=str(e["config_hash"]),
            partition="train",
            outcome=str(e["outcome"]),
            admitted_score=e["admitted_score"],
            admission_code=e["admission_code"],
            execution_failure_code=e["execution_failure_code"],
            source_job_keys=tuple(e["source_job_keys"]),
            source_plan_hashes=tuple(e["source_plan_hashes"]),
        )
        for e in examples["examples"]
    )
    features = tuple(
        BamFeatureBinding(
            dataset_id=str(b["dataset_id"]),
            vector_hash=str(b["vector_hash"]),
            feature_values_hash=str(b["feature_values_hash"]),
        )
        for b in bindings["bindings"]
    )
    a = manifest["authorities"]
    dataset = TrainingDataset(
        baseline_qualified_gate_hash=a["baseline_qualified_gate_hash"],
        baseline_selected_hash=a["baseline_selected_hash"],
        feature_registry_hash=a["feature_registry_hash"],
        config_encoding_identity=a["config_encoding_identity"],
        parameter_space_hash=a["parameter_space_hash"],
        scoring_contract_hash=a["scoring_contract_hash"],
        execution_environment_hash=a["execution_environment_hash"],
        training_contract_hash=compute_training_contract_hash(),
        training_protocol_hash=compute_training_protocol_hash(),
        train_schedule_hash=a["train_schedule_hash"],
        train_plan_hashes=tuple(sorted(a["train_plan_hashes"])),
        feature_set_hash=a["feature_set_hash"],
        feature_matrix_hash=bindings["feature_matrix_hash"],
        feature_matrix_artifact_sha256=bindings["feature_matrix_artifact_sha256"],
        bam_features=features,
        feature_names=tuple(AUTHORITATIVE_COLUMNS),
        config_feature_names=tuple(configs["config_feature_names"]),
        rows=rows,
        cv_manifest=CvManifest(
            bam_chromosome={r.dataset_id: r.chromosome for r in rows},
        ),
    )
    _require(
        dataset.identity() == ACCEPTED_TRAINING_DATASET_HASH,
        f"the reconstructed dataset hashes to {dataset.identity()}, not the accepted "
        f"{ACCEPTED_TRAINING_DATASET_HASH}",
    )
    _require(
        dataset.cv_manifest.identity() == ACCEPTED_CV_MANIFEST_HASH,
        "the reconstructed CV manifest does not match the accepted identity",
    )
    return dataset
