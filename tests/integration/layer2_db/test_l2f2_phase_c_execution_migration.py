"""Migration ``0020`` — the runner boundary admits PHASE_C, and nothing else moves.

``0011`` admitted one phase and said a second would be a later migration. ``0016`` was that
migration; this is the third and last TRAIN one. The pattern is deliberately identical, because a
boundary that grows by a different shape each time is a boundary nobody can audit: one more value
in the phase vocabulary, one more arm on the canary rule, two more functions each fixed internally
to their phase, and no relation, role, membership or grant touched.

No GATK: outcomes come from ``FakeGatkRunner`` through the private test seam.
"""

from __future__ import annotations

import json
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

_DB = "minos_l2f2_baseline"
_PRIOR = "0019_l2f2_phase_b_bootstrap"
_HEAD = "0020_l2f2_phase_c_execution"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_RESOLVE_C = "experiments.l2f2_resolve_claimed_phase_c_execution(text, uuid, text)"
_BOOTSTRAP_C = "experiments.l2f2_resolve_phase_c_runner_bootstrap()"
_ADDED = (_RESOLVE_C, _BOOTSTRAP_C)
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


def _phase_c_authority(engine: Any) -> None:
    """A minimal PHASE_C authority row, inserted as the control plane may. Schema fixture only."""
    _admin(
        engine,
        "INSERT INTO experiments.l2f2_execution_authorities ("
        "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
        "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
        "  logical_job_count) "
        "SELECT :proto, 'PHASE_C', a.plan_id, a.plan_hash, a.train_schedule_sha256, "
        "       a.candidate_set_hash, a.parameter_space_hash, 50, 10, 500 "
        f"  FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A'",
        proto=_PROTOCOL,
    )


# --------------------------------------------------------------------------- #
# the migration itself
# --------------------------------------------------------------------------- #
def test_lifecycle_0019_0020_0019_0020_adds_exactly_two_functions(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0019 = _state(engine)
            assert all(_function(engine, s) is None for s in _ADDED)
            phase = _constraint(engine, "ck_l2f2_authority_phase")
            assert phase is not None and "PHASE_C" not in phase
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert _revision(engine) == _HEAD
            for signature in _ADDED:
                fn = _function(engine, signature)
                assert fn is not None, signature
                assert fn["owner"] == "minos_admin", signature
                assert fn["owner_superuser"] is False, signature
                assert fn["prosecdef"] is True, signature
                assert "search_path" in str(fn["config"]), signature
            phase = _constraint(engine, "ck_l2f2_authority_phase")
            assert phase is not None
            for admitted in ("PHASE_A", "PHASE_B", "PHASE_C"):
                assert admitted in phase
            canary = _constraint(engine, "ck_l2f2_authority_canary_phase")
            assert canary is not None and "PHASE_C" in canary
            at_0020 = _state(engine)
        finally:
            engine.dispose()

        for section in (
            "relations",
            "constraints",
            "indexes",
            "triggers",
            "roles",
            "role_memberships",
            "schema_security",
            "default_acls",
        ):
            before, after = at_0019.get(section), at_0020.get(section)
            if section == "constraints":
                continue  # the two CHECKs are the intended change; asserted above
            assert json.dumps(before, sort_keys=True, default=str) == json.dumps(
                after, sort_keys=True, default=str
            ), f"0020 altered {section!r}"

        def _by_name(state: Any) -> dict[str, Any]:
            return {f"{r['schema']}.{r['name']}": r for r in state["functions"]}

        added = sorted(set(_by_name(at_0020)) - set(_by_name(at_0019)))
        assert added == [
            "experiments.l2f2_resolve_claimed_phase_c_execution",
            "experiments.l2f2_resolve_phase_c_runner_bootstrap",
        ]
        assert not (set(_by_name(at_0019)) - set(_by_name(at_0020))), "0020 removed a function"
        redefined = sorted(
            key
            for key in set(_by_name(at_0019)) & set(_by_name(at_0020))
            if json.dumps(_by_name(at_0019)[key], sort_keys=True, default=str)
            != json.dumps(_by_name(at_0020)[key], sort_keys=True, default=str)
        )
        assert redefined == [], f"0020 redefined {redefined}"

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert all(_function(engine, s) is None for s in _ADDED)
            assert _state(engine) == at_0019, "downgrade did not restore 0019"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            assert all(_function(engine, s) is not None for s in _ADDED)
        finally:
            engine.dispose()


def test_the_phase_c_bootstrap_reads_nothing_in_the_evaluation_schema(
    isolated_pg_base_url: str,
) -> None:
    """A truth-free worker may execute this, so its body must never reach the answer key."""
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            definition = str(_function(engine, _BOOTSTRAP_C)["definition"])  # type: ignore[index]
            assert "evaluation." not in definition
            for forbidden in ("minos_score", "admission", "truth", "metrics"):
                assert forbidden not in definition
            for expected in (
                "l2f2_execution_authorities",
                "l2f_experiment_plans",
                "l2f_experiment_jobs",
                "l2f_execution_results",
                "l2f_execution_failures",
            ):
                assert expected in definition
        finally:
            engine.dispose()


@pytest.mark.parametrize("phase", ["PHASE_D", "VALIDATION", "TEST", "phase_c", ""])
def test_a_fourth_phase_is_still_a_later_migration(l2f2: Any, phase: str) -> None:
    from sqlalchemy.exc import IntegrityError

    definition = _constraint(l2f2.engine, "ck_l2f2_authority_phase")
    assert definition is not None
    assert "VALIDATION" not in definition and "PHASE_D" not in definition

    with (
        pytest.raises(IntegrityError, match="ck_l2f2_authority_(phase|canary_phase)"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count) "
                "SELECT a.baseline_protocol_hash, :phase, a.plan_id, a.plan_hash, "
                "       a.train_schedule_sha256, a.candidate_set_hash, a.parameter_space_hash, "
                "       50, 10, 500 "
                f"  FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A'"
            ),
            {"phase": phase},
        )


def test_phase_c_must_not_carry_a_canary(l2f2: Any) -> None:
    """The canary is a Phase-A concept; Phase C inherits a proven chain, like Phase B."""
    from sqlalchemy.exc import IntegrityError

    with (
        pytest.raises(IntegrityError, match="ck_l2f2_authority_canary_phase"),
        l2f2.engine.connect() as conn,
        conn.begin(),
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                "  baseline_protocol_hash, phase, plan_id, plan_hash, train_schedule_sha256, "
                "  candidate_set_hash, parameter_space_hash, member_count, candidate_count, "
                "  logical_job_count, canary_job_key) "
                "SELECT a.baseline_protocol_hash, 'PHASE_C', a.plan_id, a.plan_hash, "
                "       a.train_schedule_sha256, a.candidate_set_hash, a.parameter_space_hash, "
                "       50, 10, 500, a.canary_job_key "
                f"  FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A'"
            )
        )
    _phase_c_authority(l2f2.engine)  # ... and the legitimate shape is accepted
    with l2f2.engine.connect() as conn:
        row = (
            conn.execute(
                text(f"SELECT canary_job_key FROM {_AUTHORITIES} WHERE phase = 'PHASE_C'")  # noqa: S608
            )
            .mappings()
            .one()
        )
    assert row["canary_job_key"] is None


def test_the_downgrade_refuses_while_a_phase_c_authority_exists(l2f2: Any) -> None:
    """Append-only lineage is not squeezed back into an A/B-only CHECK."""
    _phase_c_authority(l2f2.engine)
    l2f2.engine.dispose()

    with pytest.raises(Exception, match="PHASE_C") as excinfo:
        alembic_downgrade(l2f2.url, _PRIOR)
    assert "append-only" in str(excinfo.value)

    l2f2.engine = _engine(l2f2.url)
    assert _revision(l2f2.engine) == _HEAD
    assert all(_function(l2f2.engine, s) is not None for s in _ADDED)


def test_only_the_runner_and_the_control_plane_may_execute_the_phase_c_functions(
    l2f2: Any,
) -> None:
    with l2f2.engine.connect() as conn:
        execute = {
            signature: {
                role: bool(
                    conn.execute(
                        text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                        {"r": role, "f": signature},
                    ).scalar_one()
                )
                for role in (*_ROLES, "public")
            }
            for signature in _ADDED
        }
        runner_tables = sorted(
            table
            for table in (
                "experiments.l2f2_execution_authorities",
                "experiments.l2f_experiment_plans",
                "experiments.l2f_experiment_jobs",
                "experiments.l2f_execution_results",
            )
            if conn.execute(
                text("SELECT has_table_privilege('minos_runner', :t, 'SELECT')"), {"t": table}
            ).scalar_one()
        )
        evaluation_usage = bool(
            conn.execute(
                text("SELECT has_schema_privilege('minos_runner', 'evaluation', 'USAGE')")
            ).scalar_one()
        )

    for signature, granted in execute.items():
        assert granted == {
            "minos_runner": True,
            "minos_admin": True,
            "minos_evaluator": False,
            "minos_trainer": False,
            "minos_live": False,
            "public": False,
        }, signature
    assert runner_tables == [], "0020 gave the runner direct table access"
    assert evaluation_usage is False
