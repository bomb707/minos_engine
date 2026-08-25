"""Let evaluation persistence store exactly what the pinned upstream scorer exposes.

``0009`` modelled the evaluation ledger on MINOS_ENGINE's own reimplementation of the Minos
score, which produced the four AdvancedScorer components as first-class values and stored them
``NOT NULL``. Production scoring is now the pinned MINOS_SUBNET implementation itself, and that
implementation's public surface is deliberately narrower:

    AdvancedScorer.compute_advanced_score(metrics) -> float

The four components — core, completeness, FP-rate and quality — are local variables inside that
function. They are never returned, never attached to the metrics dictionary, and never exposed by
any other upstream entry point. Keeping the columns ``NOT NULL`` would therefore force
MINOS_ENGINE to recompute them locally purely to satisfy a schema, which is precisely the
duplicated scientific authority this corrective exists to remove.

So the four columns become nullable and their range CHECK becomes NULL-tolerant. Nothing is
dropped: historical rows written under the old path keep their values, and if a future upstream
revision does expose the components they can be populated again without another migration.

What stays ``NOT NULL`` is what upstream genuinely produces: ``minos_score_100`` (the
``compute_advanced_score`` return value), ``minos_score`` (the validator's own
``advanced_score / 100.0`` normalization) and ``overcall_penalty`` (read from the upstream
metrics dictionary, where upstream itself put it). The score-consistency CHECK between the two
score columns is untouched, as is every admission and lineage constraint.

Purely a column-nullability and CHECK change: no table is created or dropped, no function is
redefined, no grant is issued or revoked, and no role gains or loses anything.
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_l2f2_upstream_score_oracle"
down_revision: str | None = "0012_l2f_plan_member_source_idx"
branch_labels = None
depends_on = None

_SCHEMA = "evaluation"
_RESULTS = "l2f_evaluation_results"
_QUALIFIED = f"{_SCHEMA}.{_RESULTS}"

#: the four AdvancedScorer components. Upstream computes them as local variables and returns only
#: the combined score, so MINOS_ENGINE can observe them only by reimplementing the formula.
_COMPONENTS: tuple[str, ...] = (
    "core_score",
    "completeness_score",
    "fp_score",
    "quality_score",
)

_COMPONENT_RANGE = "ck_l2f_eval_results_components_range"

#: the 0009 predicate, which requires every component to be present and in [0, 1].
_RANGE_0009 = (
    "core_score >= 0 AND core_score <= 1 "
    "AND completeness_score >= 0 AND completeness_score <= 1 "
    "AND fp_score >= 0 AND fp_score <= 1 "
    "AND quality_score >= 0 AND quality_score <= 1 "
    "AND overcall_penalty >= 0"
)

#: the same bounds, but a component may legitimately be absent. ``overcall_penalty`` is NOT
#: relaxed: upstream really does expose it, so an absent value there would be a defect.
_RANGE_0013 = (
    "(core_score IS NULL OR (core_score >= 0 AND core_score <= 1)) "
    "AND (completeness_score IS NULL OR (completeness_score >= 0 AND completeness_score <= 1)) "
    "AND (fp_score IS NULL OR (fp_score >= 0 AND fp_score <= 1)) "
    "AND (quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 1)) "
    "AND overcall_penalty >= 0"
)


def _set_component_nullability(*, nullable: bool) -> None:
    for column in _COMPONENTS:
        op.alter_column(_RESULTS, column, nullable=nullable, schema=_SCHEMA)


def upgrade() -> None:
    op.drop_constraint(_COMPONENT_RANGE, _RESULTS, schema=_SCHEMA, type_="check")
    _set_component_nullability(nullable=True)
    op.create_check_constraint(_COMPONENT_RANGE, _RESULTS, _RANGE_0013, schema=_SCHEMA)


def downgrade() -> None:
    """Restore 0009's stricter shape — but only for a ledger 0009 can represent.

    A row written by the upstream oracle has no component values at all, so re-imposing NOT NULL
    would have to invent them. It refuses instead: the guard runs before any DDL, and a database
    holding upstream-scored evaluations stays at this revision rather than acquiring fabricated
    scientific values.
    """
    import sqlalchemy as sa

    conn = op.get_bind()
    predicate = " OR ".join(f"{column} IS NULL" for column in _COMPONENTS)
    missing = conn.execute(
        sa.text(f"SELECT count(*) FROM {_QUALIFIED} WHERE {predicate}")  # noqa: S608
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"cannot downgrade {revision} -> {down_revision}: {missing} evaluation row(s) have no "
            "AdvancedScorer component values because the pinned upstream scorer does not expose "
            f"them. Revision {down_revision} requires all four columns NOT NULL, and inventing "
            "them locally would reintroduce a second implementation of the Minos score."
        )

    op.drop_constraint(_COMPONENT_RANGE, _RESULTS, schema=_SCHEMA, type_="check")
    _set_component_nullability(nullable=False)
    op.create_check_constraint(_COMPONENT_RANGE, _RESULTS, _RANGE_0009, schema=_SCHEMA)
