"""Shared L2-G campaign fixtures.

Session-scoped because building a ten-spec five-fold campaign over 1040 cells is the expensive
part of these suites, and both of them need the same one.

The synthetic campaign uses the REAL frozen ``(dataset_id, config_hash)`` identifiers so the
whole-tree verifier's dataset reconstruction applies. Every label and every predictor is
synthetic: no real TRAIN label is consumed, no real feature value is read, and no model is fitted
on real data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from minos_engine.models.campaign import (
    _MINT_TOKEN,
    ACCEPTED_CANDIDATE_FAMILIES,
    ACCEPTED_CANDIDATE_SPEC_HASHES,
    ACCEPTED_REFERENCE_SPECS,
    REQUIRED_THREAD_POLICY,
    STATUS_COMPLETE,
    TrustedL2GTrainCampaign,
    _run_l2g_train_oof_core,
)
from minos_engine.models.campaign_evidence import (
    OUTPUT_LAYOUT,
    write_l2g_train_campaign_outputs,
)
from minos_engine.models.design_matrix import DesignMatrix
from minos_engine.models.fit_driver import fit_fold_estimators, fit_reference_fold
from minos_engine.models.prefit_loader import load_verified_training_dataset
from minos_engine.models.shortlist import (
    ACCEPTED_AUTHORITIES,
    ACCEPTED_PREFIT_AUTHORITY_SHA256,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root
from minos_engine.qualification.provenance import GitProvenance, read_provenance


def clean_provenance() -> GitProvenance:
    """The production path requires a clean worktree; a working tree mid-edit is not one."""
    real = read_provenance(repository_root())
    return GitProvenance(
        head_sha=real.head_sha,
        tree_sha=real.tree_sha,
        worktree_clean=True,
        parent_sha=real.parent_sha,
    )


def _spec(family: str, spec_hash: str) -> Any:
    class _S:
        def __init__(self) -> None:
            self.family = family
            self.score_model_implementation = "sklearn.linear_model.Ridge"
            self.admission_model_implementation = "sklearn.linear_model.LogisticRegression"
            self.score_hyperparameters = {"alpha": 1.0}
            self.admission_hyperparameters = {"C": 1.0, "max_iter": 1000}
            self.random_seed = 20260904

        def identity(self) -> str:
            return spec_hash

    return _S()


@pytest.fixture(scope="session")
def trusted_l2g_campaign() -> TrustedL2GTrainCampaign:
    real = load_verified_training_dataset()
    rng = np.random.default_rng(11)

    class _Row:
        def __init__(self, src: Any, admitted: bool) -> None:
            self.dataset_id = src.dataset_id
            self.config_hash = src.config_hash
            self.chromosome = src.chromosome
            self.admission_label = 1 if admitted else 0
            self.outcome = "ADMITTED" if admitted else "CANDIDATE_NON_ADMISSION"
            self.admitted_score = float(np.clip(rng.normal(0.7, 0.1), 0, 1)) if admitted else None

        def identity(self) -> str:
            return f"{self.dataset_id}|{self.config_hash}"

    rows = [_Row(r, bool(rng.random() > 0.25)) for r in real.rows]
    meta = tuple(
        {
            "dataset_id": r.dataset_id,
            "chromosome": r.chromosome,
            "config_hash": r.config_hash,
            "outcome": r.outcome,
            "admitted_score": r.admitted_score,
            "admission_label": r.admission_label,
            "identity": r.identity(),
        }
        for r in rows
    )
    design = DesignMatrix(
        x_bam=rng.normal(size=(len(rows), 129)),
        x_config=rng.normal(size=(len(rows), 28)),
        bam_columns=tuple(f"b{i}" for i in range(129)),
        config_columns=tuple(f"c{i}" for i in range(28)),
        meta=meta,
    )

    class _DS:
        cv_manifest = real.cv_manifest

        def __init__(self) -> None:
            self.rows = tuple(rows)

        def admission_weights(self) -> dict[str, float]:
            per: dict[str, int] = {}
            for r in rows:
                per[r.dataset_id] = per.get(r.dataset_id, 0) + 1
            return {r.identity(): 1 / per[r.dataset_id] for r in rows}

        def score_weights(self) -> dict[str, float]:
            admitted = [r for r in rows if r.admitted_score is not None]
            per: dict[str, int] = {}
            for r in admitted:
                per[r.dataset_id] = per.get(r.dataset_id, 0) + 1
            return {r.identity(): 1 / per[r.dataset_id] for r in admitted}

    closure = _run_l2g_train_oof_core(
        dataset=_DS(),
        design=design,
        candidate_specs=tuple(
            _spec(f, h)
            for f, h in zip(
                ACCEPTED_CANDIDATE_FAMILIES, ACCEPTED_CANDIDATE_SPEC_HASHES, strict=True
            )
        ),
        reference_specs=tuple(_spec(f, h) for f, h in ACCEPTED_REFERENCE_SPECS),
        fit_estimators=fit_fold_estimators,
        fit_reference=fit_reference_fold,
        thread_report=(
            {"user_api": "blas", "internal_api": "openblas", "num_threads": 1, "prefix": "lib"},
        ),
    )
    records = closure.pop("_records")
    failures = closure.pop("_failures")
    closure["authority"] = {
        **ACCEPTED_AUTHORITIES,
        "prefit_authority_sha256": ACCEPTED_PREFIT_AUTHORITY_SHA256,
    }
    closure["thread_policy"] = REQUIRED_THREAD_POLICY
    closure["candidate_spec_hashes"] = list(ACCEPTED_CANDIDATE_SPEC_HASHES)
    closure["reference_spec_hashes"] = [h for _, h in ACCEPTED_REFERENCE_SPECS]
    metrics = {
        h: e["metrics"] for h, e in closure["per_spec"].items() if e["status"] == STATUS_COMPLETE
    }
    provenance = clean_provenance()
    return TrustedL2GTrainCampaign(
        _MINT_TOKEN,
        closure=closure,
        records=records,
        metrics=metrics,
        failures=failures,
        execution_source_commit=provenance.head_sha,
        execution_source_tree=provenance.tree_sha,
    )


@pytest.fixture(scope="session")
def published_l2g_campaign(
    trusted_l2g_campaign: TrustedL2GTrainCampaign, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    import minos_engine.models.campaign_evidence as module

    provenance = clean_provenance()
    original = module.read_provenance
    module.read_provenance = lambda root: provenance  # type: ignore[assignment]
    try:
        out = tmp_path_factory.mktemp("publish") / OUTPUT_LAYOUT["root"]
        manifest = write_l2g_train_campaign_outputs(trusted_l2g_campaign, output_dir=out)
    finally:
        module.read_provenance = original  # type: ignore[assignment]
    return {"manifest": manifest, "dir": out}


@pytest.fixture(scope="session")
def published_l2g_result(published_l2g_campaign: dict[str, Any]) -> dict[str, Any]:
    return dict(
        json.loads(Path(published_l2g_campaign["manifest"]["campaign_result_path"]).read_bytes())
    )
