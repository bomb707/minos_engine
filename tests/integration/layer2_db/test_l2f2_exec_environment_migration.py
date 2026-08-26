"""Migration ``0015`` — every durable execution outcome says which runtime produced it.

The refusal is the point. A store that already holds execution rows recorded them before the
runtime was part of their identity, and there is no honest value to give those rows: a default
would be a lie and a backfill a guess. Refusing forces the contaminated campaign to be quarantined
and a fresh one built, which is exactly the outcome the first Phase-A attempt needed and did not
get.

No GATK, no scoring: outcomes are produced by ``FakeGatkRunner`` through the private test seam.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT
from tests.integration.layer2_db.l2f_introspect import full_structural_state
from tests.integration.layer2_db.test_l2f2_runner_boundary import l2f2 as _l2f2_fixture
from tests.integration.layer2_db.test_l2f2_runner_boundary import service as _service_fixture
from tests.integration.layer2_db.test_l2f_plan_store import _engine

l2f2 = _l2f2_fixture
service = _service_fixture

_DB = "minos_l2f2_baseline"
_PRIOR = "0014_l2f2_exec_failure_runtime"
_HEAD = "0015_l2f2_exec_environment"

_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"
_ENV = "execution_environment_hash"
_ROLES = ["minos_admin", "minos_evaluator", "minos_runner", "minos_trainer", "minos_live"]

_NEW_COMPLETE_ARGS = (
    "p_plan_hash text, p_job_id uuid, p_worker_id text, p_job_key text, p_config_hash text, "
    "p_parameter_space_hash text, p_input_identity_hash text, p_logical_argv_hash text, "
    "p_gatk_executable_sha256 text, p_gatk_version text, p_vcf_artifact_id uuid, "
    "p_vcf_sha256 text, p_manifest_artifact_id uuid, p_manifest_sha256 text, "
    "p_result_hash text, p_runtime_ms bigint, p_execution_environment_hash text"
)
_NEW_FAIL_ARGS = (
    "p_plan_hash text, p_job_id uuid, p_worker_id text, p_failure_code text, "
    "p_exit_code integer, p_stderr_sha256 text, p_runtime_ms bigint, "
    "p_execution_environment_hash text"
)


def _revision(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _state(engine: Any) -> Any:
    with engine.connect() as conn:
        return full_structural_state(conn, _ROLES, dbname=_DB)


def _columns(engine: Any, table: str) -> dict[str, str]:
    schema, name = table.split(".")
    with engine.connect() as conn:
        return {
            str(r["column_name"]): str(r["is_nullable"])
            for r in conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    " WHERE table_schema=:s AND table_name=:t"
                ),
                {"s": schema, "t": name},
            ).mappings()
        }


def _signatures(engine: Any, proname: str) -> list[str]:
    with engine.connect() as conn:
        return sorted(
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
                    "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                    " WHERE n.nspname='experiments' AND p.proname=:n"
                ),
                {"n": proname},
            )
        )


def _without_column_positions(state: Any) -> str:
    import copy

    stripped = copy.deepcopy(state)
    for table in (_RESULTS, _FAILURES):
        for column in stripped["relations"][table]["columns"]:
            column["position"] = None
    return json.dumps(stripped, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# lifecycle on an EMPTY store
# --------------------------------------------------------------------------- #
def test_empty_lifecycle_0014_0015_0014_0015(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            at_0014 = _state(engine)
            assert _ENV not in _columns(engine, _RESULTS)
            assert _ENV not in _columns(engine, _FAILURES)
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            at_0015 = _state(engine)
            assert _revision(engine) == _HEAD
            # mandatory on BOTH ledgers: a failure that cannot name its runtime is the row that
            # misled the first campaign.
            assert _columns(engine, _RESULTS)[_ENV] == "NO"
            assert _columns(engine, _FAILURES)[_ENV] == "NO"
            assert _signatures(engine, "minos_l2f_complete_job_success") == [_NEW_COMPLETE_ARGS]
            assert _signatures(engine, "minos_l2f_fail_job") == [_NEW_FAIL_ARGS]
            with engine.connect() as conn:
                checks = {
                    str(r["conname"]): str(r["definition"])
                    for r in conn.execute(
                        text(
                            "SELECT conname, pg_get_constraintdef(oid) AS definition "
                            "  FROM pg_constraint WHERE conname LIKE '%env_hash_hex'"
                        )
                    ).mappings()
                }
            assert len(checks) == 2
            assert all("[0-9a-f]{64}" in d for d in checks.values())
        finally:
            engine.dispose()

        alembic_downgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            assert _revision(engine) == _PRIOR
            assert _state(engine) == at_0014, "downgrade did not restore 0014"
        finally:
            engine.dispose()

        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            again = _state(engine)
            # add -> drop -> add moves the attnum; that ordinal is PostgreSQL bookkeeping and the
            # ONLY tolerated difference.
            assert _without_column_positions(again) == _without_column_positions(at_0015)
        finally:
            engine.dispose()


def test_0015_changes_only_the_two_outcome_ledgers_and_their_writers(
    isolated_pg_base_url: str,
) -> None:
    with scratch_database(isolated_pg_base_url, _DB) as url:
        alembic_upgrade(url, _PRIOR)
        engine = _engine(url)
        try:
            before = _state(engine)
        finally:
            engine.dispose()
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            after = _state(engine)
        finally:
            engine.dispose()

    for section in ("roles", "role_memberships", "schema_security", "default_acls", "triggers"):
        assert before.get(section) == after.get(section), f"0015 altered {section!r}"
    changed = sorted(
        name
        for name in set(before["relations"]) | set(after["relations"])
        if json.dumps(before["relations"].get(name), sort_keys=True, default=str)
        != json.dumps(after["relations"].get(name), sort_keys=True, default=str)
    )
    assert changed == sorted([_FAILURES, _RESULTS]), changed

    def _by_name(state: Any) -> dict[str, Any]:
        return {f"{r['name']}({r['identity_arguments']})": r for r in state["functions"]}

    moved = sorted(
        k
        for k in set(_by_name(before)) | set(_by_name(after))
        if json.dumps(_by_name(before).get(k), sort_keys=True, default=str)
        != json.dumps(_by_name(after).get(k), sort_keys=True, default=str)
    )
    assert all("minos_l2f_complete_job_success" in k or "minos_l2f_fail_job" in k for k in moved)
    # both widened writers keep 0008's ownership and SECURITY DEFINER shape.
    for key, row in _by_name(after).items():
        if "minos_l2f_fail_job" in key or "minos_l2f_complete_job_success" in key:
            assert row["owner"] == "minos_admin"
            assert row["security_definer"] is True


# --------------------------------------------------------------------------- #
# THE refusal
# --------------------------------------------------------------------------- #
def test_the_upgrade_refuses_a_store_that_already_holds_a_success(service: Any, l2f2: Any) -> None:
    """A pre-0015 success has no runtime identity, and 0015 will not invent one."""
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-existing-success",
        runner=FakeGatkRunner(),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    l2f2.engine.dispose()

    alembic_downgrade(l2f2.url, _PRIOR)
    with pytest.raises(Exception, match="already exist") as excinfo:
        alembic_upgrade(l2f2.url, _HEAD)
    assert "Quarantine this database" in str(excinfo.value)

    engine = _engine(l2f2.url)
    try:
        # refused BEFORE any schema mutation.
        assert _revision(engine) == _PRIOR
        assert _ENV not in _columns(engine, _RESULTS)
        assert _ENV not in _columns(engine, _FAILURES)
        with engine.connect() as conn:
            assert int(conn.execute(text(f"SELECT count(*) FROM {_RESULTS}")).scalar_one()) == 1  # noqa: S608
    finally:
        engine.dispose()
    l2f2.engine = _engine(l2f2.url)


def test_the_upgrade_refuses_a_store_that_already_holds_a_failure(service: Any, l2f2: Any) -> None:
    """Exactly the shape of the contaminated real store: five failures and no successes."""
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-existing-failure",
        runner=FakeGatkRunner(exit_code=127),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )
    assert dispatched is not None and dispatched.status == "FAILED"
    l2f2.engine.dispose()

    alembic_downgrade(l2f2.url, _PRIOR)
    with pytest.raises(Exception, match="already exist"):
        alembic_upgrade(l2f2.url, _HEAD)

    engine = _engine(l2f2.url)
    try:
        assert _revision(engine) == _PRIOR
        assert _ENV not in _columns(engine, _FAILURES)
        with engine.connect() as conn:
            assert int(conn.execute(text(f"SELECT count(*) FROM {_FAILURES}")).scalar_one()) == 1  # noqa: S608
    finally:
        engine.dispose()
    l2f2.engine = _engine(l2f2.url)


# --------------------------------------------------------------------------- #
# what the widened writers demand
# --------------------------------------------------------------------------- #
def test_the_writers_refuse_an_absent_or_malformed_environment_identity(
    service: Any, l2f2: Any
) -> None:
    """There is no NULL and no free-text escape hatch at the persistence boundary."""
    from minos_engine.storage.l2f2_runner import authorize_baseline_runner_connection

    job = l2f2.count("SELECT count(*) FROM experiments.l2f_experiment_jobs")
    assert job >= 1
    with l2f2.engine.connect() as conn:
        job_id = conn.execute(
            text("SELECT id FROM experiments.l2f_experiment_jobs LIMIT 1")
        ).scalar_one()

    for bad in (None, "not-a-hash", "A" * 64):
        with (
            pytest.raises(DatabaseError) as excinfo,
            service.connect() as conn,
            conn.begin(),
        ):
            authorize_baseline_runner_connection(conn)
            conn.execute(
                text(
                    "SELECT * FROM experiments.minos_l2f_fail_job("
                    ":h, :j, :w, :c, NULL, NULL, 1, :ee)"
                ),
                {
                    "h": l2f2.authority.plan_hash,
                    "j": job_id,
                    "w": "ci-bad-env",
                    "c": "EXECUTION_ERROR",
                    "ee": bad,
                },
            )
        assert "lowercase sha256" in str(excinfo.value)


def test_the_runner_still_holds_no_dml_on_either_ledger(service: Any) -> None:
    """0015 widened two functions; it granted nothing."""
    with service.connect() as conn:
        for table in (_RESULTS, _FAILURES):
            for privilege in ("INSERT", "UPDATE", "DELETE", "SELECT"):
                assert (
                    conn.execute(
                        text("SELECT has_table_privilege('minos_runner', :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar_one()
                    is False
                ), f"{table}.{privilege}"
        for role in ("minos_evaluator", "minos_trainer", "minos_live"):
            assert (
                conn.execute(
                    text(
                        "SELECT has_function_privilege(:r, "
                        "'experiments.minos_l2f_fail_job(text, uuid, text, text, integer, text, "
                        "bigint, text)', 'EXECUTE')"
                    ),
                    {"r": role},
                ).scalar_one()
                is False
            ), role
