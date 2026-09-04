"""THE accepted production builder for the L2-G training dataset.

``TrainingDataset`` is a strict type, and a strict type is not an authority. A caller can hand it
an internally consistent set of hashes, 129 well-formed column names, fifty plausible BAM ids and
1040 well-formed rows, and get back a scientifically foreign table that validates perfectly. The
contract cannot tell the difference, because every field it checks was supplied by the same
caller.

So this module derives the science and lets the caller supply only operational handles that
cannot change scientific meaning: two authenticated engines and, for tests, a repository root.
The caller nominates no outcome, no score, no weight, no column, no member, no plan and no config
payload. Everything comes from the committed authorities, the qualified feature matrix, and the
sealed TRAIN ledgers read through the ephemeral observation surface.

Dedup is derived here rather than audited afterwards. The builder starts from all terminal
evidence rows, groups by the scientific cell ``(dataset_id, config_hash)``, and REQUIRES the
repeats to agree before collapsing them. There is no newest-row rule, no phase preference and no
averaging: a genuine conflict means two runs of the same cell disagree about what happened, and
the honest response is to stop rather than to pick a winner.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import (
    OUTCOME_ADMITTED,
    OUTCOME_EXECUTION_FAILURE,
    OUTCOME_NON_ADMISSION,
    compute_training_contract_hash,
)
from minos_engine.models.dataset import (
    BamFeatureBinding,
    CvManifest,
    TrainingDataset,
    TrainingRow,
)
from minos_engine.models.protocol import compute_training_protocol_hash

__all__ = [
    "EXPECTED_TERMINAL_JOB_COUNT",
    "TrainingDataAuthorityError",
    "build_accepted_l2g_training_dataset",
]

# --- exact accepted upstream authorities -------------------------------------------------- #
BASELINE_QUALIFIED_GATE_HASH: Final = (
    "b9436bf3263925ebe187ed5550c7214cfa92bc75a0dd2607a7766103bfa6befa"
)
BASELINE_QUALIFICATION_HASH: Final = (
    "afbcd418dee7f5521dc52b34e2c0b5d7bd31ea5f5d4ec3b1bf0768ab35babee8"
)
BASELINE_SELECTED_HASH: Final = "b13aef13fecf8e966184d03bad5ee0e6f096fb5649b30e336283e2f50f3eba38"
SAFE_BASELINE_CONFIG_HASH: Final = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
)
BASELINE_PROTOCOL_HASH: Final = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
SCORING_CONTRACT_HASH: Final = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
EXECUTION_ENVIRONMENT_HASH: Final = (
    "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
)
PARAMETER_SPACE_HASH: Final = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
CONFIG_ENCODING_IDENTITY: Final = "3053fed09a1a7fdc9462a963871564275c88e4eca5fe3a898d2d6821c36b1fe4"
FROZEN_FEATURE_SET_HASH: Final = "7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f"

EXPECTED_TERMINAL_JOB_COUNT: Final = 1175
EXPECTED_TRAIN_MEMBERS: Final = 50
EXPECTED_FEATURE_COLUMNS: Final = 129

#: bounded candidate failures. Anything else on the execution ledger is infrastructure.
_BOUNDED_EXECUTION_FAILURES: Final = frozenset({"GATK_NONZERO_EXIT", "GATK_TIMEOUT"})


class TrainingDataAuthorityError(MinosEngineError):
    """The real TRAIN evidence does not support an accepted training dataset."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TrainingDataAuthorityError(message)


def _classify(cell: dict[str, Any]) -> dict[str, Any]:
    """Derive ONE terminal evidence row's outcome class. Never supplied by a caller."""
    job_key = cell.get("job_key")
    if cell.get("has_evaluation_failure"):
        raise TrainingDataAuthorityError(
            f"job {job_key} carries an evaluation failure; an infrastructure incident is our "
            "defect and may not become a training label, so the freeze stops here"
        )
    failure_code = cell.get("execution_failure_code")
    has_result = bool(cell.get("has_execution_result"))
    if failure_code is not None:
        _require(
            not has_result,
            f"job {job_key} carries both an execution result and an execution failure",
        )
        _require(
            failure_code in _BOUNDED_EXECUTION_FAILURES,
            f"job {job_key} failed with {failure_code!r}, which is not a bounded candidate "
            "failure; an infrastructure incident is never a label",
        )
        return {
            "outcome": OUTCOME_EXECUTION_FAILURE,
            "admitted_score": None,
            "admission_code": None,
            "execution_failure_code": str(failure_code),
        }
    _require(has_result, f"job {job_key} is terminal with neither a result nor a failure")
    _require(
        cell.get("scoring_contract_hash") == SCORING_CONTRACT_HASH,
        f"job {job_key} was evaluated under {cell.get('scoring_contract_hash')!r}, not the "
        "frozen scoring contract",
    )
    admitted = cell.get("admitted")
    _require(isinstance(admitted, bool), f"job {job_key} has no decided admission state")
    admission_code = cell.get("admission_code")
    _require(bool(admission_code), f"job {job_key} carries no admission code")
    if admitted:
        score = cell.get("minos_score")
        _require(
            isinstance(score, (int, float)),
            f"job {job_key} is ADMITTED but carries no persisted score",
        )
        return {
            "outcome": OUTCOME_ADMITTED,
            "admitted_score": float(score),  # type: ignore[arg-type]
            "admission_code": str(admission_code),
            "execution_failure_code": None,
        }
    # A non-admission is a candidate failure worth 0, NOT a score of any value. The persisted
    # minos_score is deliberately not carried forward: the frozen objective refuses to consume it.
    return {
        "outcome": OUTCOME_NON_ADMISSION,
        "admitted_score": None,
        "admission_code": str(admission_code),
        "execution_failure_code": None,
    }


def _collapse(cells: list[dict[str, Any]], *, chromosome_of: dict[str, str]) -> list[TrainingRow]:
    """Group terminal evidence into scientific cells, requiring repeats to AGREE."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for cell in cells:
        key = (str(cell["dataset_id"]), str(cell["config_hash"]))
        grouped.setdefault(key, []).append(cell)

    rows: list[TrainingRow] = []
    for (dataset_id, config_hash), members in sorted(grouped.items()):
        classified = [_classify(c) for c in members]
        first = classified[0]
        for other, evidence in zip(classified[1:], members[1:], strict=True):
            for field in ("outcome", "admission_code", "execution_failure_code"):
                _require(
                    other[field] == first[field],
                    f"the repeated cell ({dataset_id}, {config_hash}) disagrees on {field}: "
                    f"{first[field]!r} vs {other[field]!r}. Repeats are collapsed only when they "
                    "agree; there is no newest-row rule and no averaging",
                )
            _require(
                other["admitted_score"] == first["admitted_score"],
                f"the repeated cell ({dataset_id}, {config_hash}) disagrees on the admitted "
                f"score: {first['admitted_score']!r} vs {other['admitted_score']!r}",
            )
            _require(
                evidence.get("execution_environment_hash")
                == members[0].get("execution_environment_hash"),
                f"the repeated cell ({dataset_id}, {config_hash}) ran under two execution "
                "environments; those are not the same experiment",
            )
            _require(
                evidence.get("parameter_space_hash") == members[0].get("parameter_space_hash"),
                f"the repeated cell ({dataset_id}, {config_hash}) cites two parameter spaces",
            )
        rows.append(
            TrainingRow(
                dataset_id=dataset_id,
                chromosome=chromosome_of[dataset_id],
                config_hash=config_hash,
                partition="train",
                outcome=str(first["outcome"]),
                admitted_score=first["admitted_score"],
                admission_code=first["admission_code"],
                execution_failure_code=first["execution_failure_code"],
                source_job_keys=tuple(sorted(str(c["job_key"]) for c in members)),
                source_plan_hashes=tuple(sorted({str(c["plan_hash"]) for c in members})),
            )
        )
    return rows


def _read_qualified_train_matrix(operational_engine: Any) -> dict[str, Any]:
    """The REAL qualified TRAIN feature matrix. Identity and per-BAM value hashes."""
    from sqlalchemy import text

    with operational_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT fm.id, fm.matrix_hash, fm.artifact_sha256, fm.row_count, "
                    "       fm.column_count, fs.feature_set_hash, fs.registry_hash, "
                    "       fs.column_count AS set_column_count "
                    "  FROM profiling.feature_matrices fm "
                    "  JOIN profiling.feature_sets fs ON fs.id = fm.feature_set_id "
                    " WHERE fm.partition = 'train'"
                )
            )
            .mappings()
            .all()
        )
        _require(len(row) == 1, f"expected exactly one TRAIN feature matrix, found {len(row)}")
        matrix = row[0]
        members = (
            conn.execute(
                text(
                    "SELECT dr.dataset_id, mm.member_index, mm.vector_hash, "
                    "       mm.feature_values_hash "
                    "  FROM profiling.feature_matrix_members mm "
                    "  JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
                    " WHERE mm.feature_matrix_id = :i ORDER BY mm.member_index"
                ),
                {"i": matrix["id"]},
            )
            .mappings()
            .all()
        )
    _require(
        int(matrix["row_count"]) == EXPECTED_TRAIN_MEMBERS,
        f"the TRAIN matrix holds {matrix['row_count']} rows, expected {EXPECTED_TRAIN_MEMBERS}",
    )
    _require(
        int(matrix["column_count"]) == EXPECTED_FEATURE_COLUMNS,
        f"the TRAIN matrix holds {matrix['column_count']} columns, expected "
        f"{EXPECTED_FEATURE_COLUMNS}",
    )
    _require(
        matrix["feature_set_hash"] == FROZEN_FEATURE_SET_HASH,
        "the persisted TRAIN matrix was built under a different feature set",
    )
    _require(
        int(matrix["set_column_count"]) == EXPECTED_FEATURE_COLUMNS,
        f"the qualified feature set declares {matrix['set_column_count']} columns, expected "
        f"{EXPECTED_FEATURE_COLUMNS}",
    )
    _require(
        len(members) == EXPECTED_TRAIN_MEMBERS,
        f"the TRAIN matrix has {len(members)} members, expected {EXPECTED_TRAIN_MEMBERS}",
    )
    for member in members:
        _require(
            member["vector_hash"] is not None and member["feature_values_hash"] is not None,
            f"TRAIN matrix member {member['dataset_id']} carries a null value identity",
        )
    return {"matrix": dict(matrix), "members": [dict(m) for m in members]}


def build_accepted_l2g_training_dataset(
    *,
    train_conn: Any,
    operational_engine: Any,
    root: Any | None = None,
) -> TrainingDataset:
    """Derive THE accepted L2-G training dataset from real, sealed evidence.

    ``train_conn`` must already have the ephemeral L2-G observation surface installed and be
    authenticated as its grantee; ``operational_engine`` reaches the qualified feature matrix.
    Neither handle can change what the science says.
    """
    from minos_engine.baseline.schedule import build_train_schedule
    from minos_engine.experiments.gatk_live_space import (
        load_committed_live_gatk_parameter_space,
    )
    from minos_engine.models.config_encoder import build_config_encoding
    from minos_engine.qualification.l2g_training_observation import (
        observe_l2g_training_evidence,
    )

    # --- committed authorities, recomputed rather than trusted ---------------------------- #
    encoding = build_config_encoding()
    _require(
        encoding.identity() == CONFIG_ENCODING_IDENTITY,
        "the config encoding is not the accepted identity",
    )
    space = load_committed_live_gatk_parameter_space()
    _require(
        space.parameter_space_hash == PARAMETER_SPACE_HASH,
        "the committed parameter space is not the accepted identity",
    )
    schedule = build_train_schedule(root)
    chromosome_of = {m.dataset_id: m.chromosome for m in schedule.members}
    _require(
        len(chromosome_of) == EXPECTED_TRAIN_MEMBERS,
        f"the TRAIN schedule holds {len(chromosome_of)} members",
    )

    # --- the qualified feature matrix ----------------------------------------------------- #
    matrix = _read_qualified_train_matrix(operational_engine)
    matrix_bams = {str(m["dataset_id"]) for m in matrix["members"]}
    _require(
        matrix_bams == set(chromosome_of),
        "the qualified TRAIN feature matrix and the frozen TRAIN schedule describe different "
        f"BAM sets: {sorted(matrix_bams ^ set(chromosome_of))}",
    )

    # --- the sealed TRAIN ledgers --------------------------------------------------------- #
    observation = observe_l2g_training_evidence(train_conn)
    _require(
        int(observation["nonterminal_job_count"]) == 0,
        "the TRAIN campaign still holds non-terminal jobs; the dataset is not final",
    )
    _require(
        int(observation["evaluation_failure_count"]) == 0,
        "the TRAIN campaign carries evaluation failures; an infrastructure incident may not "
        "enter a training set, so the freeze refuses rather than filtering them out",
    )
    _require(
        int(observation["foreign_scoring_contract_count"]) == 0,
        "some TRAIN evaluation was scored under a foreign scoring contract",
    )
    terminal = int(observation["terminal_job_count"])
    _require(
        terminal == EXPECTED_TERMINAL_JOB_COUNT,
        f"the TRAIN campaign holds {terminal} terminal jobs, expected "
        f"{EXPECTED_TERMINAL_JOB_COUNT}",
    )
    spaces = {str(h) for h in observation["parameter_space_hashes"]}
    _require(
        spaces == {PARAMETER_SPACE_HASH},
        f"the TRAIN plans cite parameter spaces {sorted(spaces)}",
    )

    cells = [dict(c) for c in observation["cells"]]
    _require(len(cells) == terminal, "the observation returned a different number of cells")
    observed_bams = {str(c["dataset_id"]) for c in cells}
    _require(
        observed_bams <= set(chromosome_of),
        f"TRAIN evidence references BAMs outside the frozen schedule: "
        f"{sorted(observed_bams - set(chromosome_of))}",
    )
    for cell in cells:
        _require(
            cell.get("execution_environment_hash") == EXECUTION_ENVIRONMENT_HASH,
            f"job {cell.get('job_key')} ran under execution environment "
            f"{cell.get('execution_environment_hash')!r}, not the frozen one",
        )

    rows = _collapse(cells, chromosome_of=chromosome_of)

    # --- bind the per-BAM feature VALUES -------------------------------------------------- #
    surface_values = {
        str(m["dataset_id"]): str(m["feature_values_hash"]) for m in observation["members"]
    }
    bindings = []
    for member in matrix["members"]:
        dataset_id = str(member["dataset_id"])
        values_hash = str(member["feature_values_hash"])
        if dataset_id in surface_values:
            _require(
                surface_values[dataset_id] == values_hash,
                f"{dataset_id} carries feature values {surface_values[dataset_id]} in the TRAIN "
                f"campaign but {values_hash} in the qualified matrix; the model would be trained "
                "on features the campaign did not run under",
            )
        bindings.append(
            BamFeatureBinding(
                dataset_id=dataset_id,
                vector_hash=str(member["vector_hash"]),
                feature_values_hash=values_hash,
            )
        )

    from minos_engine.layer2.features.contracts import AUTHORITATIVE_COLUMNS

    return TrainingDataset(
        baseline_qualified_gate_hash=BASELINE_QUALIFIED_GATE_HASH,
        baseline_selected_hash=BASELINE_SELECTED_HASH,
        feature_registry_hash=str(matrix["matrix"]["registry_hash"]),
        config_encoding_identity=CONFIG_ENCODING_IDENTITY,
        parameter_space_hash=PARAMETER_SPACE_HASH,
        scoring_contract_hash=SCORING_CONTRACT_HASH,
        execution_environment_hash=EXECUTION_ENVIRONMENT_HASH,
        training_contract_hash=compute_training_contract_hash(),
        training_protocol_hash=compute_training_protocol_hash(),
        train_schedule_hash=schedule.split_manifest_sha256,
        train_plan_hashes=tuple(sorted(str(v) for v in observation["phase_plan_map"].values())),
        feature_set_hash=FROZEN_FEATURE_SET_HASH,
        feature_matrix_hash=str(matrix["matrix"]["matrix_hash"]),
        feature_matrix_artifact_sha256=str(matrix["matrix"]["artifact_sha256"]),
        bam_features=tuple(bindings),
        feature_names=tuple(AUTHORITATIVE_COLUMNS),
        config_feature_names=tuple(encoding.feature_names),
        rows=tuple(rows),
        cv_manifest=CvManifest(bam_chromosome=dict(chromosome_of)),
    )
