"""Least-privilege role isolation, proven by executing denied ops as each role."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

from minos_engine.storage.constants import ROLES, SCHEMAS

from . import _helpers as H


def _denied(conn: Connection, role: str, sql: str, **params: object) -> None:
    """Assert PostgreSQL raises a permission error running ``sql`` as ``role``."""
    with pytest.raises(ProgrammingError), conn.begin_nested():
        conn.execute(text(f"SET ROLE {role}"))
        conn.execute(text(sql), params)


def _as_role(conn: Connection, role: str, sql: str, **params: object) -> None:
    """Run a permitted op as ``role`` (raises if denied), then reset the role."""
    conn.execute(text(f"SET ROLE {role}"))
    try:
        conn.execute(text(sql), params)
    finally:
        conn.execute(text("RESET ROLE"))


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
def test_all_five_roles_exist(rollback_conn: Connection):
    rows = set(
        rollback_conn.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:r)"), {"r": list(ROLES)}
        ).scalars()
    )
    assert rows == set(ROLES)


def test_public_has_no_table_privileges(rollback_conn: Connection):
    n = rollback_conn.execute(
        text(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee = 'PUBLIC' AND table_schema = ANY(:s)"
        ),
        {"s": list(SCHEMAS)},
    ).scalar()
    assert n == 0


def test_public_has_no_schema_usage(rollback_conn: Connection):
    for schema in SCHEMAS:
        has = rollback_conn.execute(
            text("SELECT has_schema_privilege('public', :s, 'USAGE')"), {"s": schema}
        ).scalar()
        assert has is False, schema


# --------------------------------------------------------------------------- #
# minos_live isolation
# --------------------------------------------------------------------------- #
def test_live_cannot_access_evaluation_schema(rollback_conn: Connection):
    _denied(rollback_conn, "minos_live", "SELECT * FROM evaluation.evaluations")


def test_live_cannot_read_experiments(rollback_conn: Connection):
    _denied(rollback_conn, "minos_live", "SELECT * FROM experiments.results")


def test_live_cannot_update_or_delete_decisions(rollback_conn: Connection):
    H.insert_decision(rollback_conn, round_id="r1")
    _denied(
        rollback_conn, "minos_live", "UPDATE runtime.decisions SET decision_hash = :h", h="0" * 64
    )
    _denied(rollback_conn, "minos_live", "DELETE FROM runtime.decisions")


def test_live_cannot_mutate_catalog_or_models(rollback_conn: Connection):
    _denied(
        rollback_conn,
        "minos_live",
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('u', :h)",
        h="a" * 64,
    )
    _denied(rollback_conn, "minos_live", "UPDATE models.model_bundles SET bundle_key = 'x'")


# --------------------------------------------------------------------------- #
# other roles
# --------------------------------------------------------------------------- #
def test_evaluator_cannot_mutate_runtime_catalog_models(rollback_conn: Connection):
    H.insert_decision(rollback_conn, round_id="r1")
    _denied(
        rollback_conn,
        "minos_evaluator",
        "UPDATE runtime.decisions SET decision_hash = :h",
        h="0" * 64,
    )
    _denied(
        rollback_conn,
        "minos_evaluator",
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('u', :h)",
        h="a" * 64,
    )
    _denied(rollback_conn, "minos_evaluator", "SELECT * FROM models.model_bundles")


def test_runner_cannot_obtain_admin(rollback_conn: Connection):
    _denied(rollback_conn, "minos_runner", "CREATE TABLE catalog.sneaky (id int)")
    _denied(rollback_conn, "minos_runner", "SELECT * FROM evaluation.evaluations")


def test_trainer_cannot_read_evaluation(rollback_conn: Connection):
    _denied(rollback_conn, "minos_trainer", "SELECT * FROM evaluation.evaluations")


# --------------------------------------------------------------------------- #
# permitted operations succeed for each role
# --------------------------------------------------------------------------- #
def test_permitted_operations_succeed(rollback_conn: Connection):
    # parents created as superuser
    aid = H.insert_artifact(rollback_conn, uri="u1", sha256="a" * 64)
    pid = H.insert_profile(rollback_conn)
    cid = H.insert_config(rollback_conn)
    jid = H.insert_job(rollback_conn, pid, cid)
    rid = H.insert_result(rollback_conn, jid)

    # live may read catalog + append a decision and an audit event
    _as_role(rollback_conn, "minos_live", "SELECT * FROM catalog.artifacts")
    _as_role(
        rollback_conn,
        "minos_live",
        "INSERT INTO runtime.decisions (round_id, decision_hash, decision_manifest_hash) "
        "VALUES ('rlive', :h, :m)",
        h="7" * 64,
        m="8" * 64,
    )
    _as_role(
        rollback_conn,
        "minos_live",
        "INSERT INTO audit.events (actor_role, action, payload_hash) VALUES ('minos_live','x',:h)",
        h="9" * 64,
    )
    # runner may read configs and append a job
    _as_role(rollback_conn, "minos_runner", "SELECT * FROM catalog.gatk_configs")
    _as_role(
        rollback_conn,
        "minos_runner",
        "INSERT INTO experiments.jobs (job_key, profile_id, config_id) VALUES ('jr', :p, :c)",
        p=pid,
        c=cid,
    )
    # evaluator may append isolated evaluation evidence
    _as_role(
        rollback_conn,
        "minos_evaluator",
        "INSERT INTO evaluation.evaluations (experiment_result_id, evaluation_hash) VALUES (:r, :h)",
        r=rid,
        h="6" * 64,
    )
    # trainer may append a model bundle referencing a catalog artifact
    _as_role(
        rollback_conn,
        "minos_trainer",
        "INSERT INTO models.model_bundles (bundle_key, artifact_id) VALUES ('mt', :a)",
        a=aid,
    )
