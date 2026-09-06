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
    "build_trusted_final_train_bundle",
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
ACCEPTED_FEATURE_SET_HASH: Final = (
    "7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f"
)
ACCEPTED_FEATURE_MATRIX_HASH: Final = (
    "c6a8db848318e5c78839474fa62a4e8e408157a1e6f5cb1bdd18c9cd3d0118b2"
)
ACCEPTED_CONFIG_ENCODING_IDENTITY: Final = (
    "3053fed09a1a7fdc9462a963871564275c88e4eca5fe3a898d2d6821c36b1fe4"
)
PARENT_V1_CAMPAIGN_FREEZE: Final = (
    "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
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
) -> Any:
    """THE sealed v2 production boundary. Operational handles only; nothing scientific.

    Returns a ``TrustedL2GV2TrainCampaign``: a mutable dictionary could be edited between running
    and publishing, which is exactly the gap the v1 architecture closed. Provenance is captured
    BEFORE the first estimator fit so the result names the checkout that actually ran the models.

    Not executed on real data in this task.
    """
    import hashlib

    from minos_engine.models.relative_finalist_evidence import (
        _CAMPAIGN_TOKEN,
        mint_trusted_v2_campaign,
    )
    from minos_engine.models.relative_finalist_runner import (
        reference_decisions,
        run_relative_outer_oof,
    )
    from minos_engine.models.runtime import verify_training_runtime
    from minos_engine.models.threading_control import observe_thread_pools, single_threaded
    from minos_engine.qualification.git_tree import commit_tree_sha, is_commit
    from minos_engine.qualification.l2f_accepted_identities import repository_root
    from minos_engine.qualification.provenance import read_provenance

    # captured BEFORE the first fit, so a later checkout cannot be relabelled as the one that ran
    source_root = Path(root) if root is not None else repository_root()
    provenance = read_provenance(source_root)
    _require(
        bool(provenance.head_sha) and bool(provenance.tree_sha),
        "the execution source provenance could not be read from Git",
    )
    _require(is_commit(source_root, str(provenance.head_sha)), "HEAD is not a commit")
    _require(
        commit_tree_sha(source_root, str(provenance.head_sha)) == provenance.tree_sha,
        "HEAD's recorded tree is not its actual tree",
    )
    _require(provenance.worktree_clean, "the worktree is dirty; the campaign cannot name a commit")
    authority_path = source_root / "reports/layer2/l2g-v2-prefit-authority.json"
    _require(authority_path.is_file(), "the v2 prefit authority is missing")
    prefit_sha = hashlib.sha256(authority_path.read_bytes()).hexdigest()

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
    from minos_engine.models.relative_finalist_protocol import qualifies_against_bar

    shortlist = sorted(
        h
        for h, r in per_spec.items()
        if qualifies_against_bar(
            mean_regret=float(r["metrics"]["mean_regret"]),
            cvar_regret=float(r["metrics"]["cvar_regret"]),
            bar_mean=float(bar["mean_regret"]),
            bar_cvar=float(bar["cvar_regret"]),
        )
    )
    campaign_authority = {
        "execution_source_commit": str(provenance.head_sha),
        "execution_source_tree": str(provenance.tree_sha),
        "prefit_authority_sha256": prefit_sha,
        "parent_campaign_freeze_identity": PARENT_V1_CAMPAIGN_FREEZE,
        "relative_dataset_identity": dataset.identity(),
        "relative_protocol_hash": compute_relative_protocol_hash(),
        "relative_contract_hash": compute_relative_contract_hash(),
        "finalist_domain_hash": compute_finalist_domain_hash(),
        "feature_set_hash": ACCEPTED_FEATURE_SET_HASH,
        "feature_matrix_hash": ACCEPTED_FEATURE_MATRIX_HASH,
        "config_encoding_identity": ACCEPTED_CONFIG_ENCODING_IDENTITY,
        "training_runtime_hash": runtime["runtime_hash"],
        "candidate_spec_hashes": list(trusted.spec_hashes),
        "thread_report": [dict(sorted(p.items())) for p in pools],
    }
    expected_cells = [[r.dataset_id, r.config_hash] for r in rows]
    for entry in per_spec.values():
        entry["expected_cell_set"] = expected_cells
        entry["records"] = [r.content() for r in entry["records"]]
        entry["decisions"] = [d.content() for d in entry["decisions"]]
        entry["training_failures"] = []
    return mint_trusted_v2_campaign(
        _CAMPAIGN_TOKEN,
        authority=campaign_authority,
        per_spec=per_spec,
        references={
            name: {"metrics": r["metrics"], "decision_count": len(r["decisions"])}
            for name, r in references.items()
        },
        shortlist=tuple(shortlist),
    )


def _build_final_train_bundle_content(
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
    """PRIVATE pure helper, retained for tests only.

    It accepts a caller-supplied residual set, which is exactly why it is not the production
    boundary: whoever chooses the residuals chooses the deployment margin.
    :func:`build_trusted_final_train_bundle` derives them from verified campaign evidence.

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
        # the finalist DECISION domain is not the BAM feature schema; v2 wrote one into the
        # other, which would have made a bundle claim a feature identity it never had
        "feature_set_hash": ACCEPTED_FEATURE_SET_HASH,
        "feature_matrix_hash": ACCEPTED_FEATURE_MATRIX_HASH,
        "config_encoding_identity": ACCEPTED_CONFIG_ENCODING_IDENTITY,
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
    from minos_engine.models.relative_finalist_protocol import qualifies_against_bar

    # the SAME three-part rule TRAIN uses. A selector that only ever keeps the safe baseline ties
    # both bars, and tying both is not evidence of contextual value.
    qualified = sorted(
        h
        for h, m in selector_metrics.items()
        if qualifies_against_bar(
            mean_regret=float(m["mean_regret"]),
            cvar_regret=float(m["cvar_regret"]),
            bar_mean=bar_mean,
            bar_cvar=bar_cvar,
        )
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


def build_trusted_final_train_bundle(
    *,
    trusted_campaign: Any,
    spec_hash: str,
    estimator_artifact_sha256: str,
    transform_artifact_sha256: str | None,
) -> dict[str, Any]:
    """THE authoritative future bundle builder. Derives its own residuals from the campaign.

    Whoever chooses the residual set chooses the deployment margin, so the caller does not get to
    supply either. The spec must actually be in the campaign's frozen shortlist: a bundle for a
    model the TRAIN criterion rejected is not a deployment candidate.
    """
    from minos_engine.models.relative_finalist_evidence import TrustedL2GV2TrainCampaign
    from minos_engine.models.relative_finalist_protocol import (
        V2_CANDIDATE_GRID,
        build_v2_spec_content,
        build_v2_spec_hashes,
    )

    _require(
        isinstance(trusted_campaign, TrustedL2GV2TrainCampaign),
        "a deployment bundle may only be built from a trusted v2 campaign",
    )
    _require(
        spec_hash in trusted_campaign.shortlist,
        f"{spec_hash} is not in the frozen TRAIN shortlist; a rejected model is not deployable",
    )
    authority = trusted_campaign.authority
    entry = trusted_campaign.spec(spec_hash)
    hashes = build_v2_spec_hashes(authority["relative_dataset_identity"])
    index = list(hashes).index(spec_hash)
    spec = build_v2_spec_content(
        V2_CANDIDATE_GRID[index], dataset_identity=authority["relative_dataset_identity"]
    )

    residuals = [
        abs(float(r["predicted_delta"]) - float(r["actual_delta"])) for r in entry["records"]
    ]
    import numpy as np

    margin = float(
        np.quantile(
            np.asarray(residuals, dtype=float),
            float(spec["margin_quantile"]),
            method=NUMPY_QUANTILE_METHOD,
        )
    )
    content = _build_final_train_bundle_content(
        spec_hash=spec_hash,
        spec=spec,
        oof_residuals=residuals,
        deployment_margin=margin,
        estimator_artifact_sha256=estimator_artifact_sha256,
        transform_artifact_sha256=transform_artifact_sha256,
        train_oof_evidence_identity=entry.get("oof_scientific_hash", "0" * 64),
        source_commit=authority["execution_source_commit"],
        source_tree=authority["execution_source_tree"],
    )
    return content
