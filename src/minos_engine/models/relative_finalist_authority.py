"""The sealed L2-G v2 boundaries: trusted data, derived design matrix, and the future rules.

Nothing scientific crosses these boundaries from a caller. The v2 dataset is rebuilt from the
accepted v1 TRAIN dataset and required to hash to the frozen identity; the 157-column design
matrix is derived from verified feature bytes and the accepted config encoder; the specs come from
the frozen grid. A caller supplies where files live and nothing else.

The final-bundle procedure and the VALIDATION rule are implemented here too, before the first fit,
because both are places where a number could otherwise be chosen once the answer is visible: the
deployment margin, and the bar a selector must clear on the ten VALIDATION BAMs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    FINALIST_DOMAIN,
    PARENT_CAMPAIGN_FREEZE_IDENTITY,
    SAFE_BASELINE_CONFIG_HASH,
    compute_finalist_domain_hash,
    compute_relative_contract_hash,
)
from minos_engine.models.relative_finalist_protocol import (
    FUTURE_VALIDATION_RULE,
    NUMPY_QUANTILE_METHOD,
    V2_CANDIDATE_GRID,
    build_v2_spec_content,
    build_v2_spec_hashes,
    compute_relative_protocol_hash,
)

__all__ = [
    "ACCEPTED_RELATIVE_DATASET_IDENTITY",
    "V2_OUTPUT_ROOT",
    "RelativeAuthorityError",
    "TrustedRelativeTrainingData",
    "build_final_train_bundle_content",
    "evaluate_future_validation",
    "load_trusted_relative_training_data",
    "select_validation_winner",
]

ACCEPTED_SOURCE_TRAINING_DATASET: Final = (
    "d031758c58358270843b9b417ea034d1181a6aaafc1c94af000279c26dc62fcc"
)
ACCEPTED_RELATIVE_DATASET_IDENTITY: Final = (
    "4a8f2777ebaddffb29dc2ae96a6426eff5e76c1f23b3ee43c1b275f8d97bc5e5"
)
ACCEPTED_FINALIST_DOMAIN_HASH: Final = (
    "11f712430e94bd533e2f330c6f54323731e6787b6235ae1d74fd07f76b9bedb5"
)
ACCEPTED_RELATIVE_CONTRACT_HASH: Final = (
    "dd5aca807bf0499411b9b4279c206d7a0e9a6d50c85bf75c5d319cf7ba2d394e"
)
FEASIBILITY_PATH: Final = "reports/layer2/l2g-v2-relative-finalist-feasibility.json"
ACCEPTED_FEASIBILITY_SHA256: Final = (
    "7b269c015ec6414beba6643864b6cb48a04f52ce61e6e2687cd67069afd3cfdf"
)
V2_OUTPUT_ROOT: Final = "minos_l2g_v2_train_oof"

BAM_COLUMNS: Final = 129
CONFIG_COLUMNS: Final = 28
PREDICTOR_COLUMNS: Final = BAM_COLUMNS + CONFIG_COLUMNS
RELATIVE_ROWS: Final = 150
SOURCE_CELLS: Final = 200

_MINT_TOKEN: Final = object()


class RelativeAuthorityError(MinosEngineError):
    """The v2 training data or its design matrix does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RelativeAuthorityError(message)


class TrustedRelativeTrainingData:
    """The v2 dataset and its derived design matrix, minted only by the loader below."""

    __slots__ = ("_design", "_utility", "dataset", "spec_hashes")

    dataset: Any
    spec_hashes: tuple[str, ...]

    def __init__(
        self,
        token: object,
        *,
        dataset: Any,
        design: dict[tuple[str, str], Any],
        utility: dict[tuple[str, str], float],
        spec_hashes: tuple[str, ...],
    ) -> None:
        if token is not _MINT_TOKEN:
            raise RelativeAuthorityError(
                "trusted v2 training data may only be minted by the accepted loader; a "
                "caller-built dataset is not evidence of anything"
            )
        self.dataset = dataset
        self._design = design
        self._utility = utility
        self.spec_hashes = spec_hashes

    def design(self) -> dict[tuple[str, str], Any]:
        import copy

        return copy.deepcopy(self._design)

    def utility(self) -> dict[tuple[str, str], float]:
        return dict(self._utility)


def load_trusted_relative_training_data(
    *,
    feature_matrix_artifact_path: Any,
    workspace: Any = None,
    config_payload_root: Any = None,
    root: Any = None,
) -> TrustedRelativeTrainingData:
    """Derive the whole v2 input from accepted authorities. Operational handles only."""
    import numpy as np

    from minos_engine.models.config_table import load_verified_config_vectors
    from minos_engine.models.feature_values import load_verified_feature_values
    from minos_engine.models.prefit_loader import load_verified_training_dataset
    from minos_engine.models.relative_finalist_dataset import (
        build_relative_finalist_dataset,
    )
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = Path(root) if root is not None else repository_root()
    feasibility = base / FEASIBILITY_PATH
    _require(feasibility.is_file(), f"the v2 feasibility artifact is missing: {feasibility}")
    import hashlib

    _require(
        hashlib.sha256(feasibility.read_bytes()).hexdigest() == ACCEPTED_FEASIBILITY_SHA256,
        "the v2 feasibility artifact does not match its accepted bytes",
    )

    source = load_verified_training_dataset(workspace=workspace, root=root)
    _require(
        source.identity() == ACCEPTED_SOURCE_TRAINING_DATASET,
        "the v1 TRAIN dataset is not the accepted source dataset",
    )
    dataset = build_relative_finalist_dataset(source)
    _require(
        dataset.identity() == ACCEPTED_RELATIVE_DATASET_IDENTITY,
        f"the relative dataset hashes to {dataset.identity()}, not the accepted identity",
    )
    _require(
        dataset.finalist_domain_hash == ACCEPTED_FINALIST_DOMAIN_HASH
        and dataset.relative_contract_hash == ACCEPTED_RELATIVE_CONTRACT_HASH,
        "the relative dataset cites foreign v2 authorities",
    )
    _require(
        dataset.parent_campaign_freeze_identity == PARENT_CAMPAIGN_FREEZE_IDENTITY,
        "the relative dataset does not bind the campaign-v1 freeze as its parent",
    )
    _require(len(dataset.rows) == RELATIVE_ROWS, f"{len(dataset.rows)} rows, expected 150")
    _require(
        len(dataset.source_cell_identities) == SOURCE_CELLS,
        f"{len(dataset.source_cell_identities)} source cells, expected 200",
    )

    bam_vectors = load_verified_feature_values(
        artifact_path=Path(feature_matrix_artifact_path), dataset=source
    )
    config_hashes = tuple(sorted({r.config_hash for r in source.rows}))
    config_vectors, _ = load_verified_config_vectors(
        config_hashes=config_hashes,
        payload_root=Path(config_payload_root) if config_payload_root else None,
    )
    for finalist in FINALIST_DOMAIN:
        _require(finalist in config_vectors, f"finalist {finalist} has no verified config vector")
    safe_vector = np.asarray(config_vectors[SAFE_BASELINE_CONFIG_HASH], dtype=float)
    deltas = {
        c: np.asarray(config_vectors[c], dtype=float) - safe_vector for c in ALTERNATIVE_FINALISTS
    }
    for c, vector in deltas.items():
        _require(vector.size == CONFIG_COLUMNS, f"{c} delta has {vector.size} columns")

    design: dict[tuple[str, str], Any] = {}
    for row in dataset.rows:
        bam = np.asarray(bam_vectors[row.dataset_id], dtype=float)
        _require(bam.size == BAM_COLUMNS, f"{row.dataset_id} has {bam.size} feature values")
        vector = np.concatenate([bam, deltas[row.config_hash]])
        _require(
            vector.size == PREDICTOR_COLUMNS,
            f"a predictor row has {vector.size} columns, expected {PREDICTOR_COLUMNS}",
        )
        _require(bool(np.all(np.isfinite(vector))), "a predictor value is not finite")
        design[(row.dataset_id, row.config_hash)] = vector
    _require(len(design) == RELATIVE_ROWS, "the design matrix is not the 150-row table")

    utility: dict[tuple[str, str], float] = {}
    for source_row in source.rows:
        if source_row.config_hash not in FINALIST_DOMAIN:
            continue
        score = source_row.admitted_score
        utility[(source_row.dataset_id, source_row.config_hash)] = (
            float(score) if source_row.outcome == "ADMITTED" and score is not None else 0.0
        )
    _require(len(utility) == SOURCE_CELLS, "the four-finalist utility table is not 200 cells")

    return TrustedRelativeTrainingData(
        _MINT_TOKEN,
        dataset=dataset,
        design=design,
        utility=utility,
        spec_hashes=build_v2_spec_hashes(dataset.identity()),
    )


def run_real_l2g_v2_train_oof_campaign(
    *,
    feature_matrix_artifact_path: Any,
    workspace: Any = None,
    config_payload_root: Any = None,
    root: Any = None,
) -> dict[str, Any]:
    """THE sealed v2 production boundary. Operational handles only; nothing scientific.

    Not executed on real data in this task.
    """
    from minos_engine.models.relative_finalist_runner import (
        reference_decisions,
        run_relative_outer_oof,
    )
    from minos_engine.models.runtime import verify_training_runtime
    from minos_engine.models.threading_control import observe_thread_pools, single_threaded

    trusted = load_trusted_relative_training_data(
        feature_matrix_artifact_path=feature_matrix_artifact_path,
        workspace=workspace,
        config_payload_root=config_payload_root,
        root=root,
    )
    runtime = verify_training_runtime()
    with single_threaded():
        pools = [p for p in observe_thread_pools() if p["user_api"] in ("blas", "openmp")]
    _require(
        bool(pools) and all(p["num_threads"] == 1 for p in pools), "thread enforcement did not bind"
    )

    dataset = trusted.dataset
    chromosome_of = dict(dataset.bam_chromosome)
    design, utility = trusted.design(), trusted.utility()
    rows = list(dataset.rows)

    per_spec: dict[str, Any] = {}
    with single_threaded():
        for recipe, spec_hash in zip(V2_CANDIDATE_GRID, trusted.spec_hashes, strict=True):
            spec = build_v2_spec_content(recipe, dataset_identity=dataset.identity())
            per_spec[spec_hash] = run_relative_outer_oof(
                spec=spec,
                spec_hash=spec_hash,
                rows=rows,
                design=design,
                utility=utility,
                chromosome_of=chromosome_of,
            )
    references: dict[str, dict[str, Any]] = {}
    for name in ("ALWAYS_SAFE_BASELINE", "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN", "ORACLE4"):
        from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
        from minos_engine.models.relative_finalist_runner import policy_metrics

        decisions = []
        for chromosome in CV_FOLD_CHROMOSOMES:
            held = sorted(b for b, c in chromosome_of.items() if c == chromosome)
            train = sorted(b for b in chromosome_of if b not in set(held))
            decisions.extend(
                reference_decisions(
                    name,
                    utility=utility,
                    held_bams=held,
                    training_bams=train,
                    chromosome_of=chromosome_of,
                    outer_fold=chromosome,
                )
            )
        references[name] = {"decisions": decisions, "metrics": policy_metrics(decisions)}

    bar: dict[str, Any] = references["ALWAYS_SAFE_BASELINE"]["metrics"]
    shortlist = sorted(
        h
        for h, r in per_spec.items()
        if r["metrics"]["mean_regret"] <= bar["mean_regret"]
        and r["metrics"]["cvar_regret"] <= bar["cvar_regret"]
    )
    return {
        "relative_dataset_identity": dataset.identity(),
        "relative_protocol_hash": compute_relative_protocol_hash(),
        "relative_contract_hash": compute_relative_contract_hash(),
        "finalist_domain_hash": compute_finalist_domain_hash(),
        "training_runtime_hash": runtime["runtime_hash"],
        "thread_report": [dict(sorted(p.items())) for p in pools],
        "per_spec": per_spec,
        "references": references,
        "safe_baseline_bar": bar,
        "shortlist": shortlist,
        "shortlist_empty": not shortlist,
        "validation_read": False,
        "test_accessed": False,
    }


def build_final_train_bundle_content(
    *,
    spec_hash: str,
    spec: dict[str, Any],
    oof_residuals: Any,
    deployment_margin: float,
    estimator_artifact_sha256: str,
    transform_artifact_sha256: str | None,
    train_oof_evidence_identity: str,
    source_commit: str,
    source_tree: str,
) -> dict[str, Any]:
    """The FUTURE deployable bundle for a TRAIN-shortlisted spec. Not fitted in this task.

    The deployment margin is derived from all 150 TRAIN out-of-fold residuals under the spec's own
    frozen quantile, so it cannot be tuned once the campaign's numbers are visible. No VALIDATION
    label enters the bundle or its identity.
    """
    import numpy as np

    residuals = np.asarray(oof_residuals, dtype=float)
    _require(
        residuals.size == RELATIVE_ROWS,
        f"{residuals.size} OOF residuals, expected all {RELATIVE_ROWS}",
    )
    expected = float(
        np.quantile(residuals, float(spec["margin_quantile"]), method=NUMPY_QUANTILE_METHOD)
    )
    _require(
        deployment_margin == expected,
        f"the deployment margin {deployment_margin} is not the frozen quantile of the TRAIN OOF "
        f"residuals ({expected})",
    )
    return {
        "schema_version": "l2g-relative-finalist-bundle-v1",
        "model_spec_hash": spec_hash,
        "relative_protocol_hash": compute_relative_protocol_hash(),
        "relative_contract_hash": compute_relative_contract_hash(),
        "relative_dataset_identity": ACCEPTED_RELATIVE_DATASET_IDENTITY,
        "finalist_domain_hash": ACCEPTED_FINALIST_DOMAIN_HASH,
        "safe_baseline_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "feature_schema_hash": spec["finalist_domain_hash"],
        "config_delta_representation": spec["config_delta_representation"],
        "deployment_margin": float(deployment_margin),
        "margin_quantile": float(spec["margin_quantile"]),
        "quantile_method": spec["quantile_method"],
        "estimator_artifact_sha256": estimator_artifact_sha256,
        "transform_artifact_sha256": transform_artifact_sha256,
        "train_oof_evidence_identity": train_oof_evidence_identity,
        "training_runtime_hash": spec["training_runtime_hash"],
        "source_commit": source_commit,
        "source_tree": source_tree,
        "validation_labels_used": False,
    }


def bundle_identity(content: dict[str, Any]) -> str:
    return sha256_hex(b"minos:l2g-relative-finalist-bundle:v1\n" + canonical_json_bytes(content))


def evaluate_future_validation(
    *,
    train_shortlist: tuple[str, ...],
    selector_metrics: dict[str, dict[str, float]],
    safe_baseline_metrics: dict[str, float],
) -> dict[str, Any]:
    """The FUTURE one-shot VALIDATION rule, frozen while VALIDATION is still unread.

    Refuses to run at all without a non-empty frozen TRAIN shortlist: with nothing shortlisted
    there is no candidate for VALIDATION to choose among, and reading it could only rescue a model
    the TRAIN criterion rejected.
    """
    _require(
        bool(train_shortlist),
        "VALIDATION may not be evaluated without a non-empty frozen TRAIN shortlist",
    )
    unknown = sorted(set(selector_metrics) - set(train_shortlist))
    _require(not unknown, f"metrics supplied for non-shortlisted selectors {unknown}")
    bar_mean = float(safe_baseline_metrics["mean_regret"])
    bar_cvar = float(safe_baseline_metrics["cvar_regret"])
    qualified = sorted(
        h
        for h, m in selector_metrics.items()
        if float(m["mean_regret"]) <= bar_mean and float(m["cvar_regret"]) <= bar_cvar
    )
    return {
        "rule": FUTURE_VALIDATION_RULE["bar"],
        "validation_safe_baseline_mean_regret": bar_mean,
        "validation_safe_baseline_cvar_regret": bar_cvar,
        "qualified": qualified,
        "qualified_empty": not qualified,
        "models_qualified_status": (
            "HOLD_NO_VALIDATION_QUALIFIED_SELECTOR" if not qualified else "CANDIDATE_QUALIFIED"
        ),
    }


def select_validation_winner(
    *,
    qualified: tuple[str, ...],
    validation_metrics: dict[str, dict[str, float]],
    train_metrics: dict[str, dict[str, float]],
) -> str:
    """The frozen deterministic tie-break, fixed before any VALIDATION number exists."""
    _require(bool(qualified), "no qualified selector to choose between")
    ordered = sorted(
        qualified,
        key=lambda h: (
            float(validation_metrics[h]["mean_regret"]),
            float(validation_metrics[h]["cvar_regret"]),
            float(train_metrics[h]["mean_regret"]),
            float(train_metrics[h]["cvar_regret"]),
            h,
        ),
    )
    return ordered[0]
