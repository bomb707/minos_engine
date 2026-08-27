"""Take SUPERUSER authority away from the legacy 0009/0010 evaluator boundary.

``0017`` did this for the runner. This is the same defect, on the other side of the truth
boundary: ``0009`` and ``0010`` created their objects without ``SET ROLE minos_admin``, so four
``SECURITY DEFINER`` functions the evaluator calls on every evaluation — and the two outcome
ledgers they write — inherited whatever principal ran the migration, in practice a SUPERUSER.
``0008`` wrapped its entire upgrade in ``SET ROLE minos_admin`` / ``RESET ROLE`` and its execution
ledgers and writers are ``minos_admin``-owned; this migration restores the evaluation side to that
same model rather than inventing one.

**The two ledgers are re-owned here, and the execution-authority table was not re-owned in 0017.**
That is not an inconsistency. ``experiments.l2f2_execution_authorities`` is a control-plane
relation on which ``0011`` deliberately restricted ``minos_admin`` to explicit ``INSERT, SELECT``,
and no definer needed more. ``evaluation.l2f_evaluation_results`` and
``evaluation.l2f_evaluation_failures`` are the evaluator's append-only outcome ledgers, exactly
analogous to the ``0008`` execution ledgers — and the re-owned writers genuinely need INSERT
authority on them, which ``minos_admin`` does not have today by any grant at all.

Nothing about what the evaluator itself may do changes. No application role gains a direct write
anywhere: the evaluator still writes only through these functions, and still reads only what it
already reads. The change is ownership metadata — ``ALTER FUNCTION``/``ALTER TABLE ... OWNER TO``,
never a recreate — so OIDs, bodies, signatures, ``SECURITY DEFINER``, ``search_path``, columns,
constraints, indexes, triggers and rows are all untouched.

The downgrade returns everything to the principal running the migration and re-issues the grants
PostgreSQL absorbs on the way: while ``minos_admin`` owns a function its explicit ``EXECUTE`` is
folded into the implicit owner entry, and handing ownership back would otherwise drop it silently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018_l2f2_eval_owner_fix"
down_revision: str | None = "0017_l2f2_owner_corrective"
branch_labels = None
depends_on = None

_CONTROL_PLANE = "minos_admin"

#: the four evaluator-facing definers, by their EXACT existing identity arguments. A signature
#: that does not resolve is a refusal, never a guess.
_LEGACY_DEFINERS = (
    "evaluation.l2f_register_train_truth_identity(uuid, char, char, char, char)",
    (
        "evaluation.l2f_record_evaluation_result(uuid, char, text, char, char, text, text, uuid, "
        "char, text, double precision, double precision, double precision, double precision, "
        "double precision, double precision, double precision, text, char)"
    ),
    "evaluation.l2f_record_evaluation_failure(uuid, char, text, integer, char)",
    "evaluation.l2f_register_metrics_artifact(char, text, integer)",
)

#: the append-only outcome ledgers those writers insert into.
_LEDGERS = (
    "evaluation.l2f_evaluation_results",
    "evaluation.l2f_evaluation_failures",
)

#: what 0009/0010 granted. Restored verbatim on downgrade; minos_admin is deliberately absent from
#: the TABLE list, because it held no table grant at 0017 and must not acquire one here.
_FUNCTION_EXECUTORS = ("minos_evaluator", _CONTROL_PLANE)
_LEDGER_READERS = ("minos_evaluator",)


def _require_control_plane(conn: sa.Connection) -> None:
    """The control plane must exist and must be exactly the group role it has always been."""
    row = (
        conn.execute(
            sa.text(
                "SELECT rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolbypassrls, "
                "       rolreplication "
                "  FROM pg_roles WHERE rolname = :r"
            ),
            {"r": _CONTROL_PLANE},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError(f"role {_CONTROL_PLANE!r} does not exist; the control plane is absent")
    offending = sorted(attribute for attribute, held in row.items() if held)
    if offending:
        raise RuntimeError(
            f"role {_CONTROL_PLANE!r} holds {offending}; a definer's authority is its owner's, so "
            "this migration will not hand privileged functions to a role that is a superuser, can "
            "log in, or can create roles or databases"
        )


def _require_resolvable(conn: sa.Connection) -> None:
    """Every named function and table must resolve to exactly one existing object."""
    for signature in _LEGACY_DEFINERS:
        if not conn.execute(
            sa.text("SELECT count(*) FROM pg_proc WHERE oid = to_regprocedure(:s)"),
            {"s": signature},
        ).scalar_one():
            raise RuntimeError(
                f"{signature} does not exist in this database; this corrective alters the EXACT "
                "0009/0010 functions and refuses to invent a signature"
            )
    for table in _LEDGERS:
        if conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": table}).scalar_one() is None:
            raise RuntimeError(f"{table} does not exist in this database")


def upgrade() -> None:
    conn = op.get_bind()
    _require_control_plane(conn)
    _require_resolvable(conn)
    for signature in _LEGACY_DEFINERS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {_CONTROL_PLANE};")
    # the writers' INSERT authority comes from owning the ledgers they append to, exactly as the
    # 0008 execution writers' does. No application role is granted anything.
    for table in _LEDGERS:
        op.execute(f"ALTER TABLE {table} OWNER TO {_CONTROL_PLANE};")


def downgrade() -> None:
    """Return everything to the migration principal — who created it in 0009/0010.

    ``CURRENT_USER`` is used rather than a recorded owner: recording one would mean adding
    administrative state to a scientific store, and hard-coding one would mean guessing. The
    lifecycle test proves the assumption it rests on.

    The re-grants are not belt-and-braces. An owner's privileges are implicit, so while
    ``minos_admin`` owns these objects its explicit grants are absorbed into the owner entry and
    handing ownership back drops them with it. ``minos_admin`` — correctly not a superuser — would
    otherwise silently lose the ability to execute the very functions ``0009``/``0010`` granted it.
    """
    conn = op.get_bind()
    _require_resolvable(conn)
    for signature in _LEGACY_DEFINERS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO CURRENT_USER;")
        for role in _FUNCTION_EXECUTORS:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role};")
    for table in _LEDGERS:
        op.execute(f"ALTER TABLE {table} OWNER TO CURRENT_USER;")
        for role in _LEDGER_READERS:
            op.execute(f"GRANT SELECT ON {table} TO {role};")
