"""Take SUPERUSER authority away from the legacy 0011 runner boundary.

A ``SECURITY DEFINER`` function executes with its OWNER's authority. ``0008`` creates its writers
under ``SET ROLE minos_admin`` for exactly that reason, and ``0016`` created the Phase-B resolver
the same way. ``0011`` did not: it created

* ``experiments.l2f2_resolve_claimed_execution(text, uuid, text)`` and
* ``experiments.l2f2_register_execution_artifact(text, char, text, integer)``

as whatever principal ran the migration — in practice a SUPERUSER. Both are called by the
least-privilege runner on every execution, so the boundary whose entire purpose is to need no
administrative authority has been running two of its calls with more authority than the control
plane itself has. The runner's own grants were never wrong; the definer was.

This migration changes **ownership metadata and nothing else**. The functions are not recreated,
so their OIDs, signatures, bodies, ``SECURITY DEFINER`` flag, ``search_path`` and ACLs are
untouched — ``ALTER FUNCTION ... OWNER TO`` is the whole change. ``minos_admin`` already holds
every privilege the two bodies need: it owns ``experiments``, ``catalog`` and ``profiling`` and
their tables, and ``0011`` explicitly granted it ``SELECT, INSERT`` on the authority table.

Two deliberate NON-changes:

* **``experiments.l2f2_execution_authorities`` keeps its owner.** Re-owning it would implicitly
  hand the control plane ``UPDATE``, ``DELETE``, ``TRUNCATE``, ``ALTER`` and ``DROP`` over
  append-only scientific lineage, which is precisely what ``0011`` withheld ("UPDATE/DELETE are
  withheld and additionally refused by the append-only trigger"). Nothing needs it: a table has no
  definer semantics, and the re-owned resolver reads it through the grant it already has.
* **The four superuser-owned ``evaluation`` definers from ``0009``/``0010`` are left alone.** They
  have the same defect and are reported as a separate finding; they are evaluator-facing, not
  runner-facing, and widening a privileged corrective past the boundary it was authorized for is
  how privileged changes go wrong.

The downgrade returns both functions to the principal running the migration — which is the
principal that created them in ``0011``, and is asserted as such by the lifecycle test. A store
whose ``0011`` objects were re-owned outside Alembic is out of scope for that reversal; nothing
here guesses a historical owner or stores one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_l2f2_owner_corrective"
down_revision: str | None = "0016_l2f2_phase_b_execution"
branch_labels = None
depends_on = None

_CONTROL_PLANE = "minos_admin"

#: exactly the two 0011 objects this corrective is authorized to touch, by their EXACT existing
#: identity arguments. A signature that does not resolve is a refusal, never a guess.
#: the roles 0011 granted EXECUTE on both functions. Restored verbatim on downgrade.
_EXECUTORS = ("minos_runner", _CONTROL_PLANE)

_LEGACY_DEFINERS = (
    "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
    "experiments.l2f2_register_execution_artifact(text, char, text, integer)",
)


def _require_control_plane(conn: sa.Connection) -> None:
    """The control plane must exist and must NOT be a superuser, or this would fix nothing."""
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
    if row["rolsuper"]:
        raise RuntimeError(
            f"role {_CONTROL_PLANE!r} is a SUPERUSER; re-owning a SECURITY DEFINER function to it "
            "would leave the runner boundary exactly as wide as it is today"
        )
    if row["rolcanlogin"]:
        raise RuntimeError(
            f"role {_CONTROL_PLANE!r} has LOGIN; the control plane is a group role and this "
            "migration will not make a login principal the definer of a privileged function"
        )


def _require_resolvable(conn: sa.Connection) -> None:
    """Every named signature must resolve to exactly one existing function. No guessing."""
    for signature in _LEGACY_DEFINERS:
        found = conn.execute(
            sa.text("SELECT count(*) FROM pg_proc WHERE oid = to_regprocedure(:s)"),
            {"s": signature},
        ).scalar_one()
        if not found:
            raise RuntimeError(
                f"{signature} does not exist in this database; this corrective alters the EXACT "
                "0011 functions and refuses to invent a signature"
            )


def upgrade() -> None:
    conn = op.get_bind()
    _require_control_plane(conn)
    _require_resolvable(conn)
    for signature in _LEGACY_DEFINERS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {_CONTROL_PLANE};")


def downgrade() -> None:
    """Return both functions to the migration principal — who created them in 0011.

    ``CURRENT_USER`` is used rather than a recorded owner: recording one would mean adding
    administrative state to a scientific store, and hard-coding one would mean guessing. The
    lifecycle test proves the assumption it rests on, namely that the principal running these
    migrations is the principal that owned the 0011 objects before the upgrade.

    The two ``EXECUTE`` grants are re-issued afterwards, and that is not belt-and-braces. While
    ``minos_admin`` owns a function its explicit grant is absorbed into the implicit owner entry;
    handing ownership back drops that entry with it, and ``minos_admin`` — correctly not a
    superuser — would silently lose the ability to execute the very functions ``0011`` granted it.
    Re-granting restores ``0011``'s ACL exactly.
    """
    conn = op.get_bind()
    _require_resolvable(conn)
    for signature in _LEGACY_DEFINERS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO CURRENT_USER;")
        for role in _EXECUTORS:
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role};")
