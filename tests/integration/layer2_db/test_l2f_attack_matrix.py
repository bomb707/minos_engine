"""F3-A4: direct-SQL constraint attack matrix against a seeded valid L2-F graph.

On real PostgreSQL 16 (scratch only) this:

* seeds a complete, internally valid upstream + L2-F graph (``l2f_seed``);
* proves the 49-case attack manifest is exactly 49 unique, correctly grouped cases whose
  build order matches the frozen ``ATTACK_NAMES``;
* executes every attack independently in its own SAVEPOINT and asserts the exact PostgreSQL
  failure mechanism — SQLSTATE plus the named constraint (FK/UNIQUE/CHECK) or the stable
  trigger exception message (append-only / job-identity) — that each attack reaches;
* proves the triggers are not over-restrictive (permitted job status/claim updates succeed,
  scientific identity is preserved, every table accepts a valid new row); and
* proves the valid graph is fully intact and queryable after every rejected attack.

The named constraint each case reaches was verified empirically; the assertions below fail
if PostgreSQL ever reaches a different mechanism, so the schema cannot silently weaken.
"""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_attacks import (
    ATTACK_NAMES,
    GROUP_COUNTS,
    TOTAL_ATTACKS,
    Attack,
    attacks_by_name,
    build_attacks,
)
from tests.integration.layer2_db.l2f_seed import (
    EXPECTED_ROW_COUNTS,
    SeededGraph,
    seed_valid_graph,
)

_HEAD = "0006_l2f_experiment_plan"


def _run(conn: Connection, atk: Attack) -> None:
    if atk.op == "insert":
        assert atk.row is not None
        cols = list(atk.row)
        vals = ", ".join(f":{c}" for c in cols)
        conn.execute(
            text(f"INSERT INTO experiments.{atk.target} ({', '.join(cols)}) VALUES ({vals})"),  # noqa: S608
            atk.row,
        )
    else:
        assert atk.sql is not None
        conn.execute(text(atk.sql), atk.params or {})


def _attempt(conn: Connection, atk: Attack) -> DBAPIError | None:
    """Run one attack in a savepoint; return the raised DBAPIError (or None if it succeeded)."""
    sp = conn.begin_nested()
    err: DBAPIError | None = None
    try:
        _run(conn, atk)
    except DBAPIError as exc:
        err = exc
    finally:
        sp.rollback()
    return err


@pytest.fixture(scope="module")
def seeded(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2f_attacks") as url:
        alembic_upgrade(url, _HEAD)
        engine: Engine = create_engine(normalize_database_url(url))
        conn = engine.connect()
        tx = conn.begin()
        graph = seed_valid_graph(conn)
        # own every subsequent DML as the table owner (attacks must reach constraints/
        # triggers, not a privilege error). SET ROLE survives nested-savepoint rollbacks.
        conn.execute(text("SET ROLE minos_admin"))
        try:
            yield conn, graph
        finally:
            tx.rollback()
            conn.close()
            engine.dispose()


def test_seed_row_counts(seeded: tuple[Connection, SeededGraph]) -> None:
    conn, _ = seeded
    for table, expected in EXPECTED_ROW_COUNTS.items():
        n = conn.execute(text(f"SELECT count(*) FROM experiments.{table}")).scalar_one()  # noqa: S608
        assert n == expected, f"{table}: seeded {n}, expected {expected}"


def test_manifest_is_exactly_49_unique_grouped(seeded: tuple[Connection, SeededGraph]) -> None:
    _, graph = seeded
    attacks = build_attacks(graph)
    names = [a.name for a in attacks]
    assert len(names) == TOTAL_ATTACKS == 49
    assert len(set(names)) == 49, "case names must be unique"
    assert tuple(names) == ATTACK_NAMES, "build order must match the frozen manifest"
    assert (
        dict(Counter(a.group for a in attacks))
        == GROUP_COUNTS
        == {
            "plan": 12,
            "member": 12,
            "config": 10,
            "job": 15,
        }
    )


def _assert_isolation(conn: Connection, atk: Attack) -> None:
    """Prove the attack can only reach its declared mechanism before running it.

    Non-target composite-FK/unique targets the row satisfies must already exist; the single
    target tuple the row is missing must be absent; any fixed-value CHECK it violates must be
    false. This makes the isolated cases correct independent of FK evaluation order.
    """
    iso = atk.isolation
    if iso is None:
        return
    for sql, params in iso.present:
        n = conn.execute(text(sql), params).scalar_one()
        assert n >= 1, f"{atk.name}: isolation 'present' tuple missing ({sql!r})"
    for sql, params in iso.absent:
        n = conn.execute(text(sql), params).scalar_one()
        assert n == 0, f"{atk.name}: isolation 'absent' tuple unexpectedly present ({sql!r})"
    if iso.check_false is not None:
        assert iso.check_false is False, f"{atk.name}: the violated CHECK is not actually false"


@pytest.mark.parametrize("name", ATTACK_NAMES)
def test_attack_reaches_named_invariant(seeded: tuple[Connection, SeededGraph], name: str) -> None:
    conn, graph = seeded
    atk = attacks_by_name(graph)[name]
    _assert_isolation(conn, atk)
    err = _attempt(conn, atk)

    assert err is not None, f"{name}: expected rejection but the statement succeeded"
    orig = err.orig
    sqlstate = getattr(orig, "sqlstate", None)
    assert sqlstate == atk.sqlstate, (
        f"{name}: expected SQLSTATE {atk.sqlstate}, observed {sqlstate}"
    )
    diag = orig.diag  # type: ignore[union-attr]
    if atk.mechanism == "constraint":
        assert diag.constraint_name == atk.expect, (
            f"{name}: expected constraint {atk.expect}, observed {diag.constraint_name}"
        )
    else:
        message = diag.message_primary or ""
        assert atk.expect in message, (
            f"{name}: expected trigger message containing {atk.expect!r}, observed {message!r}"
        )


def test_all_attacks_rejected_and_graph_intact(seeded: tuple[Connection, SeededGraph]) -> None:
    """Belt-and-suspenders: run every attack in sequence, then prove the graph is untouched."""
    conn, graph = seeded
    for atk in build_attacks(graph):
        err = _attempt(conn, atk)
        assert err is not None, f"{atk.name}: unexpectedly succeeded"
    # the valid graph is fully intact and queryable after every rejected attack
    for table, expected in EXPECTED_ROW_COUNTS.items():
        n = conn.execute(text(f"SELECT count(*) FROM experiments.{table}")).scalar_one()  # noqa: S608
        assert n == expected, f"{table}: {n} rows after attacks, expected {expected}"
    # a representative deep query still resolves the full job lineage
    lineage = conn.execute(
        text(
            "SELECT count(*) FROM experiments.l2f_experiment_jobs j "
            "JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
            "JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
            "JOIN experiments.l2f_experiment_plan_configs c ON c.id = j.plan_config_id"
        )
    ).scalar_one()
    assert lineage == EXPECTED_ROW_COUNTS["l2f_experiment_jobs"]


def test_positive_controls_permitted_mutations_and_valid_inserts(
    seeded: tuple[Connection, SeededGraph],
) -> None:
    conn, g = seeded
    identity_cols = "id, plan_id, plan_member_id, plan_config_id, job_key, created_at"

    # 1) permitted status/claim update succeeds without altering scientific identity.
    sp = conn.begin_nested()
    before = conn.execute(
        text(f"SELECT {identity_cols} FROM experiments.l2f_experiment_jobs WHERE id = :i"),
        {"i": g.jb},
    ).one()
    conn.execute(
        text(
            "UPDATE experiments.l2f_experiment_jobs "
            "SET status = 'CLAIMED', claimed_by = 'runner-x', claimed_at = now(), "
            "updated_at = now() WHERE id = :i"
        ),
        {"i": g.jb},
    )
    after = conn.execute(
        text(
            f"SELECT {identity_cols}, status, claimed_by "
            "FROM experiments.l2f_experiment_jobs WHERE id = :i"
        ),
        {"i": g.jb},
    ).one()
    assert tuple(before) == tuple(after[:6]), "scientific job identity must be preserved"
    assert after.status == "CLAIMED" and after.claimed_by == "runner-x"
    # still references the same plan/member/config/job_key
    assert (
        str(after.plan_id),
        str(after.plan_member_id),
        str(after.plan_config_id),
        after.job_key,
    ) == (g.pb, g.pm_d5, g.pcb, g.jkb)
    sp.rollback()

    # 2) every table accepts a valid new row (triggers/constraints are not over-restrictive).
    for table, row in (
        ("l2f_experiment_plans", g.new_plan()),
        ("l2f_experiment_plan_members", g.new_member()),
        ("l2f_config_payloads", g.new_config_payload()),
        ("l2f_experiment_plan_configs", g.new_plan_config()),
        ("l2f_experiment_jobs", g.new_job()),
    ):
        sp = conn.begin_nested()
        cols = list(row)
        vals = ", ".join(f":{c}" for c in cols)
        conn.execute(
            text(f"INSERT INTO experiments.{table} ({', '.join(cols)}) VALUES ({vals})"),  # noqa: S608
            row,
        )
        n = conn.execute(text(f"SELECT count(*) FROM experiments.{table}")).scalar_one()  # noqa: S608
        assert n == EXPECTED_ROW_COUNTS[table] + 1, f"{table}: valid insert should succeed"
        sp.rollback()
