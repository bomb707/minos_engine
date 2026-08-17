"""Append-only evidence: database triggers reject UPDATE/DELETE even for the owner."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from . import _helpers as H


def _expect_blocked(conn: Connection, sql: str, **params: object) -> None:
    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(text(sql), params)


def test_profile_update_blocked(rollback_conn: Connection):
    H.insert_profile(rollback_conn, profile_id="p1")
    _expect_blocked(
        rollback_conn, "UPDATE profiling.profiles SET fingerprint_hash = :h", h="0" * 64
    )


def test_profile_delete_blocked(rollback_conn: Connection):
    H.insert_profile(rollback_conn, profile_id="p1")
    _expect_blocked(rollback_conn, "DELETE FROM profiling.profiles")


def test_identity_hash_rewrite_blocked(rollback_conn: Connection):
    H.insert_profile(rollback_conn, profile_id="p1")
    _expect_blocked(rollback_conn, "UPDATE profiling.profiles SET bam_sha256 = :h", h="1" * 64)


def test_decision_update_and_delete_blocked(rollback_conn: Connection):
    H.insert_decision(rollback_conn, round_id="r1")
    _expect_blocked(rollback_conn, "UPDATE runtime.decisions SET decision_hash = :h", h="0" * 64)
    _expect_blocked(rollback_conn, "DELETE FROM runtime.decisions")


def test_audit_event_overwrite_blocked(rollback_conn: Connection):
    H.insert_audit(rollback_conn)
    _expect_blocked(rollback_conn, "UPDATE audit.events SET action = 'tamper'")
    _expect_blocked(rollback_conn, "DELETE FROM audit.events")


def test_evaluation_update_delete_blocked(rollback_conn: Connection):
    pid = H.insert_profile(rollback_conn)
    cid = H.insert_config(rollback_conn)
    jid = H.insert_job(rollback_conn, pid, cid)
    rid = H.insert_result(rollback_conn, jid)
    H.insert_evaluation(rollback_conn, rid)
    _expect_blocked(
        rollback_conn, "UPDATE evaluation.evaluations SET evaluation_hash = :h", h="0" * 64
    )
    _expect_blocked(rollback_conn, "DELETE FROM evaluation.evaluations")


def test_model_bundle_and_result_update_blocked(rollback_conn: Connection):
    aid = H.insert_artifact(rollback_conn)
    H.insert_model_bundle(rollback_conn, aid)
    _expect_blocked(rollback_conn, "UPDATE models.model_bundles SET bundle_key = 'x'")
    pid = H.insert_profile(rollback_conn)
    cid = H.insert_config(rollback_conn)
    jid = H.insert_job(rollback_conn, pid, cid)
    H.insert_result(rollback_conn, jid)
    _expect_blocked(rollback_conn, "UPDATE experiments.results SET result_hash = :h", h="0" * 64)


def test_job_identity_immutable_but_status_mutable(rollback_conn: Connection):
    pid = H.insert_profile(rollback_conn)
    cid = H.insert_config(rollback_conn)
    jid = H.insert_job(rollback_conn, pid, cid)
    # Identity change blocked...
    _expect_blocked(
        rollback_conn, "UPDATE experiments.jobs SET job_key = 'other' WHERE id = :i", i=jid
    )
    # ...but working-state transition is allowed.
    rollback_conn.execute(
        text("UPDATE experiments.jobs SET status = 'RUNNING' WHERE id = :i"), {"i": jid}
    )
    status = rollback_conn.execute(
        text("SELECT status FROM experiments.jobs WHERE id = :i"), {"i": jid}
    ).scalar()
    assert status == "RUNNING"
