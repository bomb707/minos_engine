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


# ---------------------------------------------------------------------------------------- #
# L2-G v2: the real producer's output, published once
# ---------------------------------------------------------------------------------------- #
class _V2Row:
    def __init__(self, bam: str, config: str, delta: float) -> None:
        self.dataset_id, self.config_hash, self.delta = bam, config, delta


@pytest.fixture(scope="session")
def v2_world() -> dict[str, Any]:
    """REAL identities, REAL labels, REAL utilities. Only the PREDICTORS are synthetic.

    The offline verifier reconstructs the frozen dataset and authenticates every published label,
    utility and decision against it, so invented utilities cannot publish -- correctly. What stays
    synthetic is the design matrix: no real feature value is read, so the fitted models carry no
    scientific claim, while everything they are scored against is the frozen truth. One column
    carries signal so the shortlist and bundle paths are exercised rather than skipped.
    """
    from minos_engine.models.relative_finalist_reconstruction import (
        build_frozen_scientific_reference,
    )

    frozen = build_frozen_scientific_reference()
    rng = np.random.default_rng(17)
    rows = [_V2Row(bam, config, frozen.delta[(bam, config)]) for bam, config in frozen.cells()]
    design = {}
    for row in rows:
        vector = rng.normal(size=157)
        vector[0] = row.delta + rng.normal(0.0, 0.01)
        design[(row.dataset_id, row.config_hash)] = vector
    return {
        "bams": frozen.chromosome_of,
        "rows": rows,
        "utility": frozen.utility,
        "design": design,
    }


@pytest.fixture(scope="session")
def v2_produced(v2_world: dict[str, Any]) -> dict[str, Any]:
    """Exactly what the SEALED production helper returns. Nothing is added by the tests.

    ``run_frozen_candidates`` is the same function the real campaign entry calls, so family,
    implementation, diagnostics, labels, decision utilities and the cell set all come from
    production code. A fixture that filled any of those in afterwards would be testing itself --
    which is how the missing ``family`` survived a green suite once already.
    """
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_RELATIVE_DATASET_IDENTITY,
        run_frozen_candidates,
    )
    from minos_engine.models.relative_finalist_protocol import build_v2_spec_hashes

    return run_frozen_candidates(
        spec_hashes=tuple(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)),
        dataset_identity=ACCEPTED_RELATIVE_DATASET_IDENTITY,
        rows=v2_world["rows"],
        design=v2_world["design"],
        utility=v2_world["utility"],
        chromosome_of=v2_world["bams"],
    )


def v2_campaign_authority() -> dict[str, Any]:
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_CONFIG_ENCODING_IDENTITY,
        ACCEPTED_FEATURE_MATRIX_HASH,
        ACCEPTED_FEATURE_SET_HASH,
        ACCEPTED_FINALIST_DOMAIN_HASH,
        ACCEPTED_RELATIVE_CONTRACT_HASH,
        ACCEPTED_RELATIVE_DATASET_IDENTITY,
        ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
        PARENT_V1_CAMPAIGN_FREEZE,
    )
    from minos_engine.models.relative_finalist_protocol import (
        build_v2_spec_hashes,
        compute_relative_protocol_hash,
    )
    from minos_engine.models.runtime import compute_training_runtime_hash

    provenance = read_provenance(repository_root())
    return {
        "execution_source_commit": provenance.head_sha,
        "execution_source_tree": provenance.tree_sha,
        "prefit_authority_sha256": ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
        "parent_campaign_freeze_identity": PARENT_V1_CAMPAIGN_FREEZE,
        "relative_dataset_identity": ACCEPTED_RELATIVE_DATASET_IDENTITY,
        "relative_protocol_hash": compute_relative_protocol_hash(),
        "relative_contract_hash": ACCEPTED_RELATIVE_CONTRACT_HASH,
        "finalist_domain_hash": ACCEPTED_FINALIST_DOMAIN_HASH,
        "feature_set_hash": ACCEPTED_FEATURE_SET_HASH,
        "feature_matrix_hash": ACCEPTED_FEATURE_MATRIX_HASH,
        "config_encoding_identity": ACCEPTED_CONFIG_ENCODING_IDENTITY,
        "training_runtime_hash": compute_training_runtime_hash(),
        "candidate_spec_hashes": list(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)),
        "thread_report": [
            {"internal_api": "openblas", "num_threads": 1, "prefix": "l", "user_api": "blas"}
        ],
    }


def v2_reference_bundle(world: dict[str, Any]) -> dict[str, Any]:
    from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
    from minos_engine.models.relative_finalist_runner import policy_metrics, reference_decisions

    out: dict[str, Any] = {}
    for name in ("ALWAYS_SAFE_BASELINE", "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN", "ORACLE4"):
        decisions = []
        for chromosome in CV_FOLD_CHROMOSOMES:
            held = sorted(b for b, c in world["bams"].items() if c == chromosome)
            train = sorted(b for b in world["bams"] if b not in set(held))
            decisions.extend(
                reference_decisions(
                    name,
                    utility=world["utility"],
                    held_bams=held,
                    training_bams=train,
                    chromosome_of=world["bams"],
                    outer_fold=chromosome,
                )
            )
        out[name] = {
            "metrics": policy_metrics(decisions),
            "decisions": [d.content() for d in decisions],
            "decision_count": len(decisions),
        }
    return out


@pytest.fixture(scope="session")
def v2_trusted(v2_produced: dict[str, Any], v2_world: dict[str, Any]) -> Any:
    import copy

    from minos_engine.models.relative_finalist_evidence import (
        _CAMPAIGN_TOKEN,
        mint_trusted_v2_campaign,
    )
    from minos_engine.models.relative_finalist_protocol import qualifies_against_bar

    references = v2_reference_bundle(v2_world)
    bar = references["ALWAYS_SAFE_BASELINE"]["metrics"]
    per_spec = copy.deepcopy(v2_produced)
    shortlist = tuple(
        sorted(
            h
            for h, e in per_spec.items()
            if qualifies_against_bar(
                mean_regret=float(e["metrics"]["mean_regret"]),
                cvar_regret=float(e["metrics"]["cvar_regret"]),
                bar_mean=float(bar["mean_regret"]),
                bar_cvar=float(bar["cvar_regret"]),
            )
        )
    )
    return mint_trusted_v2_campaign(
        _CAMPAIGN_TOKEN,
        authority=v2_campaign_authority(),
        per_spec=per_spec,
        references=references,
        shortlist=shortlist,
    )


@pytest.fixture(scope="session")
def v2_published(v2_trusted: Any, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    import minos_engine.models.relative_finalist_evidence as module
    from minos_engine.models.relative_finalist_evidence import (
        V2_OUTPUT_LAYOUT,
        write_l2g_v2_train_campaign_outputs,
    )

    provenance = clean_provenance()
    original = module.read_provenance
    module.read_provenance = lambda root: provenance  # type: ignore[assignment]
    try:
        out = tmp_path_factory.mktemp("v2publish") / V2_OUTPUT_LAYOUT["root"]
        manifest = write_l2g_v2_train_campaign_outputs(v2_trusted, output_dir=out)
    finally:
        module.read_provenance = original  # type: ignore[assignment]
    return {"manifest": manifest, "dir": out}
