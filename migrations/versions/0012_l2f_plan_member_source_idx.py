"""Separate the plan-local member ordinal from the source feature-matrix ordinal.

``0006`` gave ``experiments.l2f_experiment_plan_members.member_index`` two incompatible jobs at
once:

* the **plan-local ordinal** — contiguous ``0..N-1`` in plan order, unique per plan, and part of
  the scientific identity that ``plan_hash`` and ``job_key`` are computed over; and
* the **source ordinal** — because ``fk_l2f_pm_matrix_member`` bound that same column to
  ``profiling.feature_matrix_members.member_index``, the database asserted that a plan member's
  local position *is* the position of the matrix row it references.

For a plan covering the COMPLETE live TRAIN inventory the two are necessarily equal, so the
conflation was invisible and harmless. It stops being either as soon as a plan is an authorized
SUBSET of that inventory: L2-F2 Phase A is five members of the accepted fifty, at local indices
``0..4`` referencing matrix rows ``0/10/20/30/40`` — one per chromosome batch. Under ``0011``
that plan is literally unrepresentable; the FK rejects local index 1 against matrix row 10.

This migration is additive and repairs only that lineage representation. It introduces a second
column for the source ordinal, backfills it from ``member_index`` (correct for every historical
row precisely because the two namespaces were forced equal), and re-points the composite FK at
it. The plan-local column keeps its name, its ``UNIQUE(plan_id, member_index)``, its non-negative
CHECK and its exclusive role in plan identity — no hash, job key, role privilege, execution
function, evaluation table or job state machine is touched.

(The revision identifier is ``0012_l2f_plan_member_source_idx`` rather than ``…_source_index``
because Alembic's ``public.alembic_version.version_num`` is ``varchar(32)``; every revision in
this repository has always fit that budget, and widening a shared bookkeeping column as a side
effect of a domain migration would make the downgrade irreversible.)

``source_matrix_member_index`` is persistence lineage metadata. It is deliberately NOT part of
``ExperimentPlan``, ``plan_hash``, ``job_key`` or the Phase-A authority manifest.

The downgrade **fails closed**. Revision ``0011`` can represent a plan-member row only when its
two indices coincide, so if any row disagrees the downgrade refuses rather than deleting rows,
rewriting an index or silently collapsing the namespaces back together.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_l2f_plan_member_source_idx"
down_revision: str | None = "0011_l2f2_runner_boundary"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_MEMBERS = "l2f_experiment_plan_members"
_QUALIFIED = f"{_SCHEMA}.{_MEMBERS}"
_MATRIX_MEMBERS = "profiling.feature_matrix_members"

_LOCAL = "member_index"
_SOURCE = "source_matrix_member_index"

#: the composite lineage FK keeps its conceptual name across both revisions — only the local
#: column it binds through changes, from the plan-local ordinal to the source ordinal.
_MATRIX_FK = "fk_l2f_pm_matrix_member"
_SOURCE_NONNEG = "ck_l2f_pm_source_matrix_member_index_nonneg"

#: reused from 0001 — the shared append-only rejection trigger this table already carries. The
#: one-shot backfill below is the only UPDATE this table will ever legitimately receive, so the
#: trigger is suspended for exactly that statement and restored immediately.
_APPEND_ONLY_TRIGGER = f"trg_{_SCHEMA}_{_MEMBERS}_append_only"

#: the referenced side never changes: the matrix member is still identified by its own id,
#: matrix, dataset and feature_values_hash, and now by its own ordinal under its own name.
_FK_REFERENT_COLUMNS = (
    "id",
    "feature_matrix_id",
    "dataset_registry_id",
    "member_index",
    "feature_values_hash",
)
_FK_LOCAL_PREFIX = ("feature_matrix_member_id", "feature_matrix_id", "dataset_registry_id")
_FK_LOCAL_SUFFIX = ("feature_values_hash",)


def _create_matrix_fk(index_column: str) -> None:
    op.create_foreign_key(
        _MATRIX_FK,
        _MEMBERS,
        "feature_matrix_members",
        [*_FK_LOCAL_PREFIX, index_column, *_FK_LOCAL_SUFFIX],
        [*_FK_REFERENT_COLUMNS],
        source_schema=_SCHEMA,
        referent_schema="profiling",
    )


def upgrade() -> None:
    # 1. the second namespace, nullable for the moment so existing rows stay valid.
    op.add_column(_MEMBERS, sa.Column(_SOURCE, sa.BigInteger(), nullable=True), schema=_SCHEMA)

    # 2. backfill. Every pre-existing row is a full-inventory plan member whose source ordinal
    #    the OLD FK forced to equal its local ordinal, so this is a restatement of a fact the
    #    database was already enforcing — not a guess. The append-only trigger is suspended for
    #    this single statement and restored before anything else runs.
    op.execute(f"ALTER TABLE {_QUALIFIED} DISABLE TRIGGER {_APPEND_ONLY_TRIGGER};")
    op.execute(f"UPDATE {_QUALIFIED} SET {_SOURCE} = {_LOCAL} WHERE {_SOURCE} IS NULL;")  # noqa: S608
    op.execute(f"ALTER TABLE {_QUALIFIED} ENABLE TRIGGER {_APPEND_ONLY_TRIGGER};")

    # 3. it is now total, and non-negative like the ordinal it mirrors.
    op.alter_column(_MEMBERS, _SOURCE, nullable=False, schema=_SCHEMA)
    op.create_check_constraint(_SOURCE_NONNEG, _MEMBERS, f"{_SOURCE} >= 0", schema=_SCHEMA)

    # 4. the lineage FK now binds the SOURCE ordinal to the matrix row's ordinal. The matrix row
    #    remains declaratively cross-bound on id/matrix/dataset/feature_values_hash exactly as
    #    before; only the ordinal column on this side changes.
    op.drop_constraint(_MATRIX_FK, _MEMBERS, schema=_SCHEMA, type_="foreignkey")
    _create_matrix_fk(_SOURCE)


def downgrade() -> None:
    """Restore 0011 — but ONLY for a database 0011 can actually represent.

    ``0011`` has a single ordinal column carrying both meanings, so a row whose two indices
    differ has no ``0011`` representation at all. Collapsing it would mean either corrupting the
    plan-local index (breaking ``plan_hash`` / ``job_key`` lineage) or corrupting the source
    reference. Both are unacceptable, so this refuses instead. The guard runs BEFORE any DDL.
    """
    conn = op.get_bind()
    split = conn.execute(
        sa.text(f"SELECT count(*) FROM {_QUALIFIED} WHERE {_SOURCE} <> {_LOCAL}")  # noqa: S608
    ).scalar_one()
    if split:
        raise RuntimeError(
            f"cannot downgrade {revision} -> {down_revision}: {split} plan-member row(s) have a "
            f"{_SOURCE} that differs from their {_LOCAL} (an authorized SUBSET plan, e.g. L2-F2 "
            f"Phase A at local 0..4 over source 0/10/20/30/40). Revision {down_revision} stores "
            "one ordinal for both namespaces and cannot represent those rows; downgrading would "
            "have to corrupt either the plan-local identity or the source matrix reference. "
            "Remove the subset plan(s) deliberately first, or stay at this revision."
        )

    op.drop_constraint(_MATRIX_FK, _MEMBERS, schema=_SCHEMA, type_="foreignkey")
    _create_matrix_fk(_LOCAL)
    op.drop_constraint(_SOURCE_NONNEG, _MEMBERS, schema=_SCHEMA, type_="check")
    op.drop_column(_MEMBERS, _SOURCE, schema=_SCHEMA)
