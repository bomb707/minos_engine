"""Migration ``0021`` — the runner boundary admits PHASE_D, and nothing else moves.

``0011`` admitted one phase, ``0016`` a second, ``0020`` a third. This is the fourth and last of
this search, and the pattern is deliberately identical to its three predecessors: one more value in
the phase vocabulary, one more arm on the canary rule, two more functions each fixed internally to
their phase, and no relation, role, membership or grant touched.

One thing IS different, and it is the point of the migration: the Phase-D resolver's partition
predicate is ``validation`` where every TRAIN phase's is ``train``. Those predicates are mutually
exclusive, so the tests below prove the boundary in both directions — a TRAIN job is unreachable
through the Phase-D interface, and TEST is unreachable through any of them.

This migration is source support for a SEPARATE validation database. Nothing here touches the
completed TRAIN baseline store, which is scientifically closed; every test runs on a scratch
database created and dropped by the fixture.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_runner_boundary import l2f2 as _l2f2_fixture
from tests.integration.layer2_db.test_l2f2_runner_boundary import service as _service_fixture
from tests.integration.layer2_db.test_l2f_plan_store import _engine

l2f2 = _l2f2_fixture
service = _service_fixture

_DB = "minos_l2f2_validation_scratch"
_PRIOR = "0020_l2f2_phase_c_execution"
_HEAD = "0021_l2f2_validation_execution"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_RESOLVE_D = "experiments.l2f2_resolve_claimed_phase_d_execution(text, uuid, text)"
_BOOTSTRAP_D = "experiments.l2f2_resolve_phase_d_runner_bootstrap()"
_ADDED = (_RESOLVE_D, _BOOTSTRAP_D)
_PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _function(engine: Any, signature: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT pg_get_userbyid(p.proowner) AS owner, r.rolsuper AS owner_superuser, "
                    "       p.prosecdef, p.proconfig::text AS config, "
                    "       pg_get_functiondef(p.oid) AS definition "
                    "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    " WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": signature},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _constraint(engine: Any, name: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar_one_or_none()


def _admin(engine: Any, sql: str, **params: Any) -> Any:
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        return conn.execute(text(sql), params)


def _phase_d_authority(engine: Any) -> int:
    """A minimal PHASE_D authority row, inserted as the control plane may. Schema fixture only.

    Returns the number of rows written so a caller can assert the fixture actually did something:
    an INSERT ... SELECT that matches nothing writes nothing and fails silently.
    """
    return _admin(
        engine,
        "INSERT INTO experiments.l2f2_execution_authorities ("
        "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
        "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
        "  logical_job_count) "
        "SELECT :proto, 'PHASE_D', a.plan_id, a.plan_hash, a.train_schedule_sha256, "
        "       a.candidate_set_hash, a.parameter_space_hash, 10, 4, 40 "
        f"  FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A'",
        proto=_PROTOCOL,
    ).rowcount


# --------------------------------------------------------------------------- #
# the migration itself
# --------------------------------------------------------------------------- #
def test_lifecycle_0020_0021_0020_0021_adds_exactly_two_functions(
    isolated_pg_base_url: str,
) -> None:
    """Up, down and up again, comparing the FULL structural state at each stop."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0020 = _state(engine)
            assert all(_function(engine, s) is None for s in _ADDED)
            phase = _constraint(engine, "ck_l2f2_authority_phase")
            assert phase is not None and "PHASE_D" not in phase
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            for signature in _ADDED:
                fn = _function(engine, signature)
                assert fn is not None, signature
                # a SECURITY DEFINER function executes with its OWNER's authority
                assert fn["owner"] == "minos_admin", signature
                assert fn["owner_superuser"] is False, signature
                assert fn["prosecdef"] is True, signature
                assert "search_path" in str(fn["config"]), signature
            phase = _constraint(engine, "ck_l2f2_authority_phase")
            assert phase is not None
            for admitted in ("PHASE_A", "PHASE_B", "PHASE_C", "PHASE_D"):
                assert admitted in phase
            canary = _constraint(engine, "ck_l2f2_authority_canary_phase")
            assert canary is not None and "PHASE_D" in canary
            at_0021 = _state(engine)
        finally:
            engine.dispose()

        # nothing structural moved except the two functions and the two check constraints
        for section in (
            "relations",
            "indexes",
            "triggers",
            "roles",
            "role_memberships",
            "schema_security",
            "default_acls",
        ):
            assert at_0021[section] == at_0020[section], section

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert all(_function(engine, s) is None for s in _ADDED)
            back = _state(engine)
        finally:
            engine.dispose()
        assert back == at_0020

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            assert _state(engine) == at_0021
        finally:
            engine.dispose()


def test_this_migration_sits_on_0020_and_the_graph_stays_linear() -> None:
    """0021's own position in the chain, and the graph-level single-head invariant.

    This test used to assert that 0021 WAS the head. That was only ever true until the next
    migration existed, and asserting it again in every migration's suite would mean editing an
    accepted test each time one is added. What actually matters here is 0021's own edge — it
    descends from 0020 and exactly one revision descends from it — plus the invariant that the
    graph as a whole has one head, whichever revision currently holds that position. The newest
    migration's suite is what pins the identity of the head.
    """
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[3] / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in sorted(versions.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)', body, re.M)
        down = re.search(r'^down_revision(?::[^=]+)?\s*=\s*["\']?([^"\'\n]+)', body, re.M)
        if rev:
            value = down.group(1).strip() if down else None
            revisions[rev.group(1)] = None if value in (None, "None") else value
    children = {d for d in revisions.values() if d}
    heads = [r for r in revisions if r not in children]
    assert len(heads) == 1, heads  # one head, whichever revision currently holds it
    assert revisions[_HEAD] == _PRIOR, "0021 must still descend from 0020"
    descendants = [r for r, down in revisions.items() if down == _HEAD]
    assert len(descendants) <= 1, descendants  # the chain never forks at this revision


def test_downgrade_refuses_while_a_phase_d_authority_exists(l2f2: Any) -> None:
    """Append-only scientific lineage has no honest way back. The refusal changes nothing.

    Uses the seeded baseline fixture because an authority row is bound by foreign key to a real
    persisted plan; an authority that referenced nothing would not exercise the refusal at all.
    """
    l2f2.engine.dispose()
    alembic_upgrade(l2f2.url, _HEAD)
    l2f2.engine = _engine(l2f2.url)

    inserted = _phase_d_authority(l2f2.engine)
    assert inserted == 1, "the PHASE_D authority row was not created; the test would be vacuous"
    l2f2.engine.dispose()

    with pytest.raises(Exception, match="PHASE_D") as excinfo:
        alembic_downgrade(l2f2.url, _PRIOR)
    assert "append-only" in str(excinfo.value)

    l2f2.engine = _engine(l2f2.url)
    assert _revision(l2f2.engine) == _HEAD
    assert all(_function(l2f2.engine, s) is not None for s in _ADDED)


# --------------------------------------------------------------------------- #
# the partition boundary, in both directions
# --------------------------------------------------------------------------- #
def test_the_phase_d_resolver_admits_validation_and_refuses_train_and_test(
    isolated_pg_base_url: str,
) -> None:
    """The predicate is read out of the function body itself, not inferred from behaviour."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_function(engine, _RESOLVE_D)["definition"])
        finally:
            engine.dispose()
    assert "pm.partition = 'validation'" in definition
    assert "pm.partition = 'train'" not in definition
    assert "'test'" not in definition
    assert "a.phase = 'PHASE_D'" in definition


def test_every_train_resolver_still_refuses_validation(isolated_pg_base_url: str) -> None:
    """0021 must not have loosened the three TRAIN resolvers while adding a fourth."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            for signature in (
                "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
                "experiments.l2f2_resolve_claimed_phase_b_execution(text, uuid, text)",
                "experiments.l2f2_resolve_claimed_phase_c_execution(text, uuid, text)",
            ):
                fn = _function(engine, signature)
                assert fn is not None, signature
                definition = str(fn["definition"])
                assert "pm.partition = 'train'" in definition, signature
                assert "validation" not in definition, signature
        finally:
            engine.dispose()


def test_the_phase_d_bootstrap_is_truth_free_and_takes_no_arguments(
    isolated_pg_base_url: str,
) -> None:
    """A validation worker cannot nominate a plan, a runtime, a partition or a truth path."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_function(engine, _BOOTSTRAP_D)["definition"])
        finally:
            engine.dispose()
    # two strings out, nothing in
    assert "RETURNS TABLE(plan_hash text, execution_environment_hash text)" in definition.replace(
        "\n", " "
    ).replace("  ", " ")
    for forbidden in ("truth_vcf", "truth_sha256", "truth_tbi", "mutations_vcf", "l2f_evaluation"):
        assert forbidden not in definition, forbidden
    # and the frozen 4 x 10 = 40 shape is enforced inside
    assert "4 x 10 = 40" in definition


def test_the_phase_d_bootstrap_requires_a_wholly_validation_plan(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_function(engine, _BOOTSTRAP_D)["definition"])
        finally:
            engine.dispose()
    assert "pm.partition <> 'validation'" in definition
    assert "non-VALIDATION member" in definition


def test_only_the_runner_and_the_control_plane_may_execute_the_new_functions(
    isolated_pg_base_url: str,
) -> None:
    """Least privilege: no evaluator, no trainer, no live role, and no PUBLIC."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for signature in _ADDED:
                    for role in ("minos_evaluator", "minos_trainer", "minos_live", "public"):
                        granted = conn.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": signature},
                        ).scalar_one()
                        assert granted is False, f"{role} may execute {signature}"
                    for role in ("minos_runner", "minos_admin"):
                        granted = conn.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": signature},
                        ).scalar_one()
                        assert granted is True, f"{role} may not execute {signature}"
        finally:
            engine.dispose()


def test_the_canary_rule_requires_phase_d_to_carry_none(l2f2: Any) -> None:
    """Phase A carries the canary; every later phase, Phase D included, must not."""
    l2f2.engine.dispose()
    alembic_upgrade(l2f2.url, _HEAD)
    l2f2.engine = _engine(l2f2.url)

    canary = _constraint(l2f2.engine, "ck_l2f2_authority_canary_phase")
    assert canary is not None
    assert "PHASE_D" in canary and "canary_job_key IS NULL" in canary

    with pytest.raises(Exception, match="ck_l2f2_authority_canary_phase"):
        _admin(
            l2f2.engine,
            "INSERT INTO experiments.l2f2_execution_authorities ("
            "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
            "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
            "  logical_job_count, canary_job_key) "
            "SELECT :proto, 'PHASE_D', a.plan_id, a.plan_hash, a.train_schedule_sha256, "
            "       a.candidate_set_hash, a.parameter_space_hash, 10, 4, 40, :canary "
            f"  FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A'",
            proto=_PROTOCOL,
            canary="e" * 64,
        )

    # ... and the legitimate shape, carrying no canary at all, is accepted
    assert _phase_d_authority(l2f2.engine) == 1
