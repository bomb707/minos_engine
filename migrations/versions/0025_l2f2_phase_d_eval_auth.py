"""Let a least-privilege evaluator prove which CAMPAIGN an execution belongs to.

The Phase-D evaluator must not score an execution merely because it looks like one of ours. It
has to prove the execution belongs to the EXACT frozen Phase-D plan — the campaign whose four
finalists were chosen from the TRAIN search — because evaluating an execution from some other
validation plan would attribute one campaign's numbers to another.

It could not. ``minos_evaluator`` sees 138 columns across every schema and ``plan_hash`` is in
none of them. The identity IS bound cryptographically — ``job_key`` and the execution result hash
are both domain-separated over ``plan_hash`` — but recomputing either needs ``profile_id``,
``content_hash`` and the plan-local ``member_index``, none of which the evaluator may read, and
which it must not be given: those are the search's own inputs.

So the strongest available check was "validation partition, a frozen member, a frozen config".
Two validation plans over the same ten members and the same four configurations are
indistinguishable under that test. This migration closes exactly that gap and nothing else.

One new view, two columns
-------------------------
``evaluation.l2f_phase_d_execution_authority`` answers one question — which plan does this
execution belong to — and answers nothing else. It carries no truth identity, no truth path, no
config payload, no profile feature, no matrix, no TRAIN or TEST row.

It is a NEW object rather than a column added to ``l2f_completed_execution_inputs``. That
projection is shared with TRAIN evaluation and has an accepted shape; widening it would hand every
existing caller a surprise column, make Phase-D authorization an implicit property of a generic
view, and force ``downgrade`` to reconstruct an accepted historical object rather than drop one
this migration owns. An additive view keeps each of those properties.

VALIDATION-only by construction
-------------------------------
The ``WHERE`` clause restricts the view to validation plans, so TRAIN and TEST executions are not
rows a caller could filter badly — they are not rows at all. The Python partition gate stays
exactly where it is; this is a second, structural layer, not a replacement.

``downgrade`` drops the one view this migration owns. It restores nothing, deletes no scientific
evidence and touches no accepted object, because this migration creates no scientific state — it
only makes an existing fact legible to the principal that needs it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025_l2f2_phase_d_eval_auth"
down_revision: str | None = "0024_l2f2_phase_d_anchor"
branch_labels = None
depends_on = None

_CONTROL_PLANE = "minos_admin"
_AUTHORITY_VIEW = "evaluation.l2f_phase_d_execution_authority"
_VALIDATION = "validation"

#: every service role except the evaluator. The runner executes and never evaluates; the trainer
#: and the live path have no business knowing which campaign an execution belongs to.
_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_runner")

_PLANS = "experiments.l2f_experiment_plans"
_RESULTS = "experiments.l2f_execution_results"


def _authority_view() -> str:
    """execution_result_id -> the plan_hash that execution actually belongs to. Nothing more.

    ``plan_hash`` is read from the plan the result is persisted against, through ``r.plan_id``.
    No caller, session parameter or function argument contributes to it, so this surface cannot
    associate execution A with plan B.
    """
    return (
        f"CREATE VIEW {_AUTHORITY_VIEW} AS "
        "SELECT r.id AS execution_result_id, p.plan_hash "
        f"  FROM {_RESULTS} r "
        f"  JOIN {_PLANS} p ON p.id = r.plan_id "
        f" WHERE p.partition = '{_VALIDATION}';"
    )


def _require_control_plane(conn: sa.Connection) -> None:
    """The view's owner must be the non-superuser control plane, as every accepted object is."""
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
            f"role {_CONTROL_PLANE!r} is a superuser or can log in; an owner-defined view must "
            "not read its base tables with that authority"
        )


def upgrade() -> None:
    conn = op.get_bind()
    _require_control_plane(conn)

    # created UNDER the control plane, so the view is owner-defined by minos_admin from the start
    # rather than created by the migration login and re-owned afterwards.
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_authority_view())
    op.execute("RESET ROLE")

    op.execute(f"REVOKE ALL ON {_AUTHORITY_VIEW} FROM PUBLIC;")
    for role in _DENIED_ROLES:
        op.execute(f"REVOKE ALL ON {_AUTHORITY_VIEW} FROM {role};")
    # SELECT only. There is no DML surface here and none is wanted: the evaluator reads an
    # authority fact, it does not assert one.
    op.execute(f"GRANT SELECT ON {_AUTHORITY_VIEW} TO minos_evaluator;")

    # The evaluator boundary pins the schema revision, exactly as the runner boundary does, and
    # cannot enforce a pin it may not read. ``0011`` granted this to ``minos_runner`` for the
    # same reason and recorded the same rationale: alembic_version carries no scientific data,
    # so SELECT is the whole grant.
    op.execute("GRANT SELECT ON public.alembic_version TO minos_evaluator;")


def downgrade() -> None:
    """Drop the one object this migration owns. It holds no scientific state to orphan."""
    op.execute("REVOKE SELECT ON public.alembic_version FROM minos_evaluator;")
    op.execute(f"DROP VIEW IF EXISTS {_AUTHORITY_VIEW};")
