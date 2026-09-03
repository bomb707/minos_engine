"""Let a least-privilege closer rank the four finalists without touching the search's inputs.

``0025`` let the evaluator prove WHICH CAMPAIGN an execution belongs to. Closure needs something
different: the terminal scientific state of all forty Phase-D jobs, in one place, complete enough
to rebuild every ``BaselineObservation`` and apply the frozen total order.

Why a new view and not a wider old one
--------------------------------------
The frozen tie-break is four levels deep and its SECOND level is mean GATK runtime.
``evaluation.l2f_completed_execution_inputs`` does not expose execution runtime at all, so a
least-privilege closer literally could not apply the order it is required to apply. Dropping the
level because exact ties "are unlikely" would be choosing a different total order than the one
that was frozen, so the runtime is exposed here instead.

That projection and ``evaluation.l2f_phase_d_execution_authority`` are accepted historical
surfaces with existing callers; widening either would change what every current reader sees.
This migration adds a third, owns it outright, and its ``downgrade`` drops exactly what it made.

What it does NOT expose
-----------------------
No truth path, truth hash, mutation path, CONFIG payload, BAM path, profile feature or feature
matrix. Closure ranks already-decided outcomes; it never re-derives one, so it needs none of the
search's inputs and is not given them.

VALIDATION-only by construction
-------------------------------
Admission is the PLAN's persisted partition, joined through the job's own plan. There is no
partition parameter to get wrong: TRAIN and TEST jobs are not rows a caller could filter badly,
they are not rows at all.

One row per job, and the scoring contract is not collapsed away
---------------------------------------------------------------
A job has at most one execution result and at most one execution failure; an execution may in
principle carry terminal evaluations under more than one scoring contract. The view therefore
keeps ``scoring_contract_hash`` on every evaluation column set and does NOT pick a winner among
contracts — no "latest", no "highest", no "first row". A job evaluated under two contracts appears
as two rows, which the reader is required to notice and refuse rather than silently average.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026_l2f2_phase_d_closure"
down_revision: str | None = "0025_l2f2_phase_d_eval_auth"
branch_labels = None
depends_on = None

_CONTROL_PLANE = "minos_admin"
_CLOSURE_VIEW = "evaluation.l2f_phase_d_closure_inputs"
_VALIDATION = "validation"
_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_runner")


def _closure_view() -> str:
    """One row per (Phase-D validation job x terminal evaluation identity).

    Every column is an immutable ledger fact. ``member_index`` and ``config_index`` come from the
    persisted plan rows, so the frozen pair a row belongs to is not something a caller asserts.
    """
    return f"""
        CREATE VIEW {_CLOSURE_VIEW} AS
        SELECT p.plan_hash                        AS plan_hash,
               j.id                               AS job_id,
               j.job_key                          AS job_key,
               j.status                           AS job_status,
               pm.member_index                    AS member_index,
               pc.config_index                    AS config_index,
               pc.config_hash                     AS config_hash,
               d.dataset_id                       AS dataset_id,
               d.round_id                         AS round_id,
               d.chromosome                       AS chromosome,
               er.id                              AS execution_result_id,
               er.result_hash                     AS execution_result_hash,
               er.runtime_ms                      AS execution_runtime_ms,
               er.execution_environment_hash       AS execution_environment_hash,
               ef.id                              AS execution_failure_id,
               ef.failure_code                    AS execution_failure_code,
               ef.runtime_ms                      AS execution_failure_runtime_ms,
               ef.execution_environment_hash       AS execution_failure_environment_hash,
               ev.id                              AS evaluation_id,
               ev.evaluation_hash                 AS evaluation_hash,
               ev.scoring_contract_hash           AS scoring_contract_hash,
               ev.minos_score                     AS minos_score,
               ev.admitted                        AS admitted,
               ev.admission_code                  AS admission_code,
               evf.id                             AS evaluation_failure_id,
               evf.failure_code                   AS evaluation_failure_code,
               evf.scoring_contract_hash          AS evaluation_failure_scoring_contract_hash
          FROM experiments.l2f_experiment_jobs j
          JOIN experiments.l2f_experiment_plans p
            ON p.id = j.plan_id
          JOIN experiments.l2f_experiment_plan_members pm
            ON pm.id = j.plan_member_id
          JOIN experiments.l2f_experiment_plan_configs pc
            ON pc.id = j.plan_config_id
          JOIN catalog.dataset_registry d
            ON d.id = pm.dataset_registry_id
          LEFT JOIN experiments.l2f_execution_results er
            ON er.job_id = j.id
          LEFT JOIN experiments.l2f_execution_failures ef
            ON ef.job_id = j.id
          LEFT JOIN evaluation.l2f_evaluation_results ev
            ON ev.execution_result_id = er.id
          LEFT JOIN evaluation.l2f_evaluation_failures evf
            ON evf.execution_result_id = er.id
         WHERE p.partition = '{_VALIDATION}'
           AND pm.partition = '{_VALIDATION}';
    """


def _require_control_plane(conn: sa.Connection) -> None:
    """The view's owner must be the non-superuser control plane, as every accepted object is."""
    row = (
        conn.execute(
            sa.text(
                "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolbypassrls "
                "  FROM pg_roles WHERE rolname = :r"
            ),
            {"r": _CONTROL_PLANE},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError(f"role {_CONTROL_PLANE!r} does not exist; the control plane is absent")
    elevated = [
        name
        for name, held in (
            ("SUPERUSER", row["rolsuper"]),
            ("LOGIN", row["rolcanlogin"]),
            ("CREATEDB", row["rolcreatedb"]),
            ("CREATEROLE", row["rolcreaterole"]),
            ("BYPASSRLS", row["rolbypassrls"]),
        )
        if held
    ]
    if elevated:
        raise RuntimeError(
            f"role {_CONTROL_PLANE!r} holds {elevated}; an owner-defined view must not read its "
            "base tables with that authority"
        )


def upgrade() -> None:
    conn = op.get_bind()
    _require_control_plane(conn)

    # created UNDER the control plane so the view is owner-defined by minos_admin from the start
    # rather than created by the migration login and re-owned afterwards.
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_closure_view())
    op.execute("RESET ROLE")

    op.execute(f"REVOKE ALL ON {_CLOSURE_VIEW} FROM PUBLIC;")
    for role in _DENIED_ROLES:
        op.execute(f"REVOKE ALL ON {_CLOSURE_VIEW} FROM {role};")
    # SELECT only. Closure reads decided outcomes and asserts none of them.
    op.execute(f"GRANT SELECT ON {_CLOSURE_VIEW} TO minos_evaluator;")


def downgrade() -> None:
    """Drop the one object this migration owns. It holds no scientific state to orphan."""
    op.execute(f"DROP VIEW IF EXISTS {_CLOSURE_VIEW};")
