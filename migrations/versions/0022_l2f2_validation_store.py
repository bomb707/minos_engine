"""Let the L2-F plan graph durably represent a VALIDATION plan — without loosening TRAIN.

``0021`` admitted ``PHASE_D`` at the runner boundary, but the substrate underneath it was still
TRAIN-only: ``l2f_experiment_plans`` and ``l2f_experiment_plan_members`` both carried
``CHECK (partition = 'train')``, and every member required a TRAIN feature-matrix lineage that a
validation member does not have and must not fabricate. A Phase-D plan could be derived in source
and then had nowhere to live.

This migration closes that, and the shape of the fix matters more than its size.

**TRAIN is strengthened, not relaxed.** The old schema said "partition must be train", and TRAIN's
matrix lineage was enforced only by NOT NULL columns that happened to apply to every row. Widening
the partition CHECK would have silently made those columns optional for TRAIN too. So each
NOT NULL that is being relaxed is replaced by a partition-CONDITIONAL check that requires it for
TRAIN explicitly. A TRAIN plan or member that omits its matrix lineage is refused now exactly as it
was before — by a constraint that names TRAIN rather than by a column default nobody restated.

**Validation is represented truthfully.** A validation plan carries NO feature-matrix identity at
all, because validation does not use one: the feature matrix is how Phase A and Phase B *chose*
candidates from BAM profile features, and Phase D does not choose anything — it runs four already
frozen configurations on ten BAMs. The honest representation of "there is no matrix here" is NULL,
not a fabricated hash, so the conditional check requires those columns to be NULL for validation.
Nothing is invented to satisfy an older column.

**The composite foreign keys need no change.** ``fk_l2f_plans_train_matrix_lineage`` and
``fk_l2f_pm_matrix_member`` are MATCH SIMPLE, so they are enforced when their columns are all
present and skipped when any is NULL. TRAIN rows still satisfy them in full; validation rows, whose
matrix columns are NULL by constraint, are outside their scope. The snapshot-member FK still
applies to both, and it carries ``partition``, so a validation member must point at a genuine
VALIDATION snapshot member.

**TEST is still refused everywhere.** The widened CHECK admits ``train`` and ``validation`` only.

This migration also adds the VALIDATION counterpart of ``0009``'s truth-registration surface: a
validation-only target projection and a narrow ``SECURITY DEFINER`` registrar that re-derives the
partition from ``catalog.split_allocations`` and refuses anything that is not ``validation``. The
TRAIN registrar is untouched and is not parameterised.

It is source support for a SEPARATE validation database. It is deliberately NOT applied to the
completed TRAIN baseline store, which is scientifically closed at ``0020``.

``downgrade`` restores ``0021`` exactly, and REFUSES while any 0022-owned scientific state exists —
a validation plan, a ``PHASE_D`` authority, or a registered VALIDATION truth identity — because
squeezing those back into a TRAIN-only graph would mean deleting or relabelling evidence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022_l2f2_validation_store"
down_revision: str | None = "0021_l2f2_validation_execution"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_PLANS_TABLE = "l2f_experiment_plans"
_MEMBERS_TABLE = "l2f_experiment_plan_members"
_PLANS = f"{_SCHEMA}.{_PLANS_TABLE}"
_MEMBERS = f"{_SCHEMA}.{_MEMBERS_TABLE}"
_AUTHORITIES = f"{_SCHEMA}.l2f2_execution_authorities"

_TRAIN = "train"
_VALIDATION = "validation"

#: the two partition CHECKs 0006 created, and what 0022 replaces them with.
_PLANS_PARTITION_CK = "ck_l2f_plans_partition_train"
_MEMBERS_PARTITION_CK = "ck_l2f_pm_partition_train"
_PLANS_PARTITION_CK_0022 = "ck_l2f_plans_partition_valid"
_MEMBERS_PARTITION_CK_0022 = "ck_l2f_pm_partition_valid"

#: the partition-conditional lineage checks 0022 introduces.
_PLANS_LINEAGE_CK = "ck_l2f_plans_partition_lineage"
_MEMBERS_LINEAGE_CK = "ck_l2f_pm_partition_lineage"

#: plan columns that are TRAIN feature-matrix lineage, relaxed to NULLABLE and then required back
#: for TRAIN by an explicit conditional check.
_PLAN_MATRIX_COLUMNS = (
    "train_feature_matrix_id",
    "train_matrix_hash",
    "train_feature_view_hash",
    "feature_set_id",
    "feature_set_hash",
    "feature_registry_hash",
)
#: member columns that are TRAIN feature-matrix lineage, same treatment.
_MEMBER_MATRIX_COLUMNS = ("feature_matrix_id", "feature_matrix_member_id")

_VALIDATION_TARGETS = "evaluation.l2f_validation_truth_registration_targets"
_REGISTER_VALIDATION_TRUTH = "evaluation.l2f_register_validation_truth_identity"
_REGISTER_VALIDATION_TRUTH_SIG = f"{_REGISTER_VALIDATION_TRUTH}(uuid, char, char, char, char)"

_CONTROL_PLANE = "minos_admin"
#: the runner never registers truth and never reads a truth target. It is denied both.
_TRUTH_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_runner")

_SQLSTATE_PARTITION = "MN040"
_SQLSTATE_CONFLICT = "MN041"


def _plans_partition_check() -> str:
    return f"partition IN ('{_TRAIN}', '{_VALIDATION}')"


def _plans_lineage_check() -> str:
    """TRAIN must carry every matrix identity; VALIDATION must carry none of them.

    Written as two exhaustive arms rather than one, so a row cannot satisfy it by being neither.
    """
    train_present = " AND ".join(f"{c} IS NOT NULL" for c in _PLAN_MATRIX_COLUMNS)
    validation_absent = " AND ".join(f"{c} IS NULL" for c in _PLAN_MATRIX_COLUMNS)
    return (
        f"(partition = '{_TRAIN}' AND {train_present}) OR "
        f"(partition = '{_VALIDATION}' AND {validation_absent})"
    )


def _members_lineage_check() -> str:
    train_present = " AND ".join(f"{c} IS NOT NULL" for c in _MEMBER_MATRIX_COLUMNS)
    validation_absent = " AND ".join(f"{c} IS NULL" for c in _MEMBER_MATRIX_COLUMNS)
    return (
        f"(partition = '{_TRAIN}' AND {train_present}) OR "
        f"(partition = '{_VALIDATION}' AND {validation_absent})"
    )


def _validation_targets_view() -> str:
    """The VALIDATION counterpart of 0009's TRAIN target projection.

    A separate view rather than a partition column on the existing one: an evaluator enumerating
    registration targets picks a relation, not a filter, and TRAIN remains structurally absent from
    this one exactly as validation is absent from that one.
    """
    return (
        f"CREATE VIEW {_VALIDATION_TARGETS} AS "
        "SELECT dr.id AS dataset_registry_id, dr.dataset_id, dr.round_id, dr.chromosome "
        "  FROM catalog.split_allocations sa "
        "  JOIN catalog.dataset_registry dr ON dr.id = sa.dataset_registry_id "
        f" WHERE sa.partition = '{_VALIDATION}';"
    )


def _register_validation_truth_function() -> str:
    """0009's registrar with ONE difference: the partition it will accept.

    The partition is READ from ``catalog.split_allocations`` rather than supplied, so a caller
    cannot register TRAIN or TEST truth through this interface by naming it differently. Identity
    is content hashes only — no path is stored, and none is accepted.

    Idempotent for identical bytes; a typed conflict for changed ones. Truth that changed under a
    registered identity is not a new registration, it is a contradiction.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_REGISTER_VALIDATION_TRUTH}("
        "p_dataset_registry_id uuid, p_truth_vcf char(64), p_truth_tbi char(64), "
        "p_mut_vcf char(64), p_mut_tbi char(64)) "
        "RETURNS TABLE(identity_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $regv$ "
        "DECLARE v_partition text; v_id uuid; v_row record; BEGIN "
        "SELECT sa.partition INTO v_partition FROM catalog.split_allocations sa "
        "  WHERE sa.dataset_registry_id = p_dataset_registry_id; "
        "IF v_partition IS NULL THEN "
        "  RAISE EXCEPTION 'dataset % is not in the accepted split', p_dataset_registry_id "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        f"IF v_partition <> '{_VALIDATION}' THEN "
        "  RAISE EXCEPTION 'L2-F2-F registers VALIDATION truth only; dataset % is %', "
        "    p_dataset_registry_id, v_partition "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        "SELECT * INTO v_row FROM evaluation.dataset_evaluation_identity d "
        "  WHERE d.dataset_registry_id = p_dataset_registry_id; "
        "IF FOUND THEN "
        "  IF v_row.truth_vcf_sha256 IS DISTINCT FROM p_truth_vcf "
        "     OR v_row.truth_tbi_sha256 IS DISTINCT FROM p_truth_tbi "
        "     OR v_row.mutations_vcf_sha256 IS DISTINCT FROM p_mut_vcf "
        "     OR v_row.mutations_tbi_sha256 IS DISTINCT FROM p_mut_tbi THEN "
        "    RAISE EXCEPTION 'truth identity for dataset % already registered with different "
        "bytes', p_dataset_registry_id "
        f"      USING ERRCODE = '{_SQLSTATE_CONFLICT}'; END IF; "
        "  RETURN QUERY SELECT v_row.id, false; RETURN; END IF; "
        "INSERT INTO evaluation.dataset_evaluation_identity "
        "  (dataset_registry_id, truth_vcf_sha256, truth_tbi_sha256, "
        "   mutations_vcf_sha256, mutations_tbi_sha256) "
        "VALUES (p_dataset_registry_id, p_truth_vcf, p_truth_tbi, p_mut_vcf, p_mut_tbi) "
        "RETURNING id INTO v_id; "
        "RETURN QUERY SELECT v_id, true; END; $regv$;"
    )


def _require_control_plane(conn: sa.Connection) -> None:
    row = (
        conn.execute(
            sa.text("SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": _CONTROL_PLANE},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError(f"role {_CONTROL_PLANE!r} does not exist; the control plane is absent")
    if row["rolsuper"] or row["rolcanlogin"]:
        raise RuntimeError(
            f"role {_CONTROL_PLANE!r} is a superuser or can log in; a SECURITY DEFINER function "
            "must not execute with that authority"
        )


def upgrade() -> None:
    _require_control_plane(op.get_bind())

    # ---- the plan graph admits VALIDATION, and TRAIN's lineage becomes explicit --------------
    for column in _PLAN_MATRIX_COLUMNS:
        op.alter_column(_PLANS_TABLE, column, nullable=True, schema=_SCHEMA)
    for column in _MEMBER_MATRIX_COLUMNS:
        op.alter_column(_MEMBERS_TABLE, column, nullable=True, schema=_SCHEMA)

    op.drop_constraint(_PLANS_PARTITION_CK, _PLANS_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PLANS_PARTITION_CK_0022, _PLANS_TABLE, _plans_partition_check(), schema=_SCHEMA
    )
    op.create_check_constraint(
        _PLANS_LINEAGE_CK, _PLANS_TABLE, _plans_lineage_check(), schema=_SCHEMA
    )

    op.drop_constraint(_MEMBERS_PARTITION_CK, _MEMBERS_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _MEMBERS_PARTITION_CK_0022, _MEMBERS_TABLE, _plans_partition_check(), schema=_SCHEMA
    )
    op.create_check_constraint(
        _MEMBERS_LINEAGE_CK, _MEMBERS_TABLE, _members_lineage_check(), schema=_SCHEMA
    )

    # ---- the VALIDATION truth-registration surface -------------------------------------------
    op.execute(_validation_targets_view())
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_register_validation_truth_function())
    op.execute("RESET ROLE")

    for obj in (_VALIDATION_TARGETS,):
        op.execute(f"REVOKE ALL ON {obj} FROM PUBLIC;")
        for role in _TRUTH_DENIED_ROLES:
            op.execute(f"REVOKE ALL ON {obj} FROM {role};")
        op.execute(f"GRANT SELECT ON {obj} TO minos_evaluator;")
    op.execute(f"REVOKE ALL ON FUNCTION {_REGISTER_VALIDATION_TRUTH_SIG} FROM PUBLIC;")
    for role in _TRUTH_DENIED_ROLES:
        op.execute(f"REVOKE ALL ON FUNCTION {_REGISTER_VALIDATION_TRUTH_SIG} FROM {role};")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REGISTER_VALIDATION_TRUTH_SIG} TO minos_evaluator;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REGISTER_VALIDATION_TRUTH_SIG} TO {_CONTROL_PLANE};")


def downgrade() -> None:
    """Restore 0021 exactly — but REFUSE while any 0022-owned scientific state exists."""
    conn = op.get_bind()
    # every check happens BEFORE anything is altered, so a refusal leaves the database as it was.
    validation_plans = conn.execute(
        sa.text(f"SELECT count(*) FROM {_PLANS} WHERE partition = '{_VALIDATION}'")  # noqa: S608
    ).scalar_one()
    phase_d = conn.execute(
        sa.text(f"SELECT count(*) FROM {_AUTHORITIES} WHERE phase = 'PHASE_D'")  # noqa: S608
    ).scalar_one()
    validation_truth = conn.execute(
        sa.text(
            "SELECT count(*) FROM evaluation.dataset_evaluation_identity d "
            "  JOIN catalog.split_allocations sa "
            "    ON sa.dataset_registry_id = d.dataset_registry_id "
            f" WHERE sa.partition = '{_VALIDATION}'"  # noqa: S608
        )
    ).scalar_one()
    if validation_plans or phase_d or validation_truth:
        raise RuntimeError(
            f"cannot downgrade to a TRAIN-only plan graph: {validation_plans} validation plan(s), "
            f"{phase_d} PHASE_D authority row(s) and {validation_truth} registered VALIDATION "
            "truth identity(ies) exist. That state is append-only scientific lineage, so there is "
            "no honest way back — dropping it or relabelling it as TRAIN would falsify the record "
            "of a validation confirmation."
        )

    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_REGISTER_VALIDATION_TRUTH_SIG};")
    op.execute("RESET ROLE")
    op.execute(f"DROP VIEW IF EXISTS {_VALIDATION_TARGETS};")

    op.drop_constraint(_MEMBERS_LINEAGE_CK, _MEMBERS_TABLE, schema=_SCHEMA, type_="check")
    op.drop_constraint(_MEMBERS_PARTITION_CK_0022, _MEMBERS_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _MEMBERS_PARTITION_CK, _MEMBERS_TABLE, f"partition = '{_TRAIN}'", schema=_SCHEMA
    )
    op.drop_constraint(_PLANS_LINEAGE_CK, _PLANS_TABLE, schema=_SCHEMA, type_="check")
    op.drop_constraint(_PLANS_PARTITION_CK_0022, _PLANS_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PLANS_PARTITION_CK, _PLANS_TABLE, f"partition = '{_TRAIN}'", schema=_SCHEMA
    )

    for column in _MEMBER_MATRIX_COLUMNS:
        op.alter_column(_MEMBERS_TABLE, column, nullable=False, schema=_SCHEMA)
    for column in _PLAN_MATRIX_COLUMNS:
        op.alter_column(_PLANS_TABLE, column, nullable=False, schema=_SCHEMA)
