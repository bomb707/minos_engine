"""PostgreSQL rejects invalid inserts (constraints enforced at the database)."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DataError, IntegrityError

from . import _helpers as H

_ERRORS = (IntegrityError, DataError)


def _expect_error(conn: Connection, sql: str, **params: object) -> None:
    with pytest.raises(_ERRORS), conn.begin_nested():
        conn.execute(text(sql), params)


def test_malformed_hash_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('u', 'not-a-hash')",
    )


def test_uppercase_hash_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('u', :h)",
        h="A" * 64,
    )


def test_empty_uri_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('', :h)",
        h="a" * 64,
    )


def test_negative_size_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.artifacts (uri, sha256, size_bytes) VALUES ('u', :h, -1)",
        h="a" * 64,
    )


def test_duplicate_artifact_hash_rejected(rollback_conn: Connection):
    H.insert_artifact(rollback_conn, uri="u1", sha256="a" * 64)
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.artifacts (uri, sha256) VALUES ('u2', :h)",
        h="a" * 64,
    )


def test_duplicate_config_hash_rejected(rollback_conn: Connection):
    H.insert_config(rollback_conn, config_hash="b" * 64)
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.gatk_configs (config_hash, parameter_space_hash) VALUES (:h, :p)",
        h="b" * 64,
        p="c" * 64,
    )


def test_duplicate_profile_identity_tuple_rejected(rollback_conn: Connection):
    H.insert_profile(rollback_conn, profile_id="p1")
    # Same input identity tuple, different profile_id and identity_tuple_hash.
    _expect_error(
        rollback_conn,
        "INSERT INTO profiling.profiles "
        "(profile_id, bam_sha256, bai_sha256, reference_sha256, fai_sha256, region_hash, "
        " profile_manifest_hash, fingerprint_hash, identity_tuple_hash) "
        "VALUES ('p2', :t, :t2, :t3, :t4, :t5, :m, :f, :it)",
        t="d" * 64,
        t2="e" * 64,
        t3="f" * 64,
        t4="0" * 64,
        t5="1" * 64,
        m="2" * 64,
        f="3" * 64,
        it="5" * 64,
    )


def test_empty_dataset_id_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO catalog.datasets (dataset_id) VALUES ('')",
    )


def test_orphan_foreign_key_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO experiments.jobs (job_key, profile_id, config_id) "
        "VALUES ('j', gen_random_uuid(), gen_random_uuid())",
    )


def test_invalid_job_status_rejected(rollback_conn: Connection):
    pid = H.insert_profile(rollback_conn)
    cid = H.insert_config(rollback_conn)
    _expect_error(
        rollback_conn,
        "INSERT INTO experiments.jobs (job_key, profile_id, config_id, status) "
        "VALUES ('j', :p, :c, 'BOGUS')",
        p=pid,
        c=cid,
    )


def test_duplicate_round_decision_rejected(rollback_conn: Connection):
    H.insert_decision(rollback_conn, round_id="r1", decision_hash="7" * 64)
    _expect_error(
        rollback_conn,
        "INSERT INTO runtime.decisions (round_id, decision_hash, decision_manifest_hash) "
        "VALUES ('r1', :h, :m)",
        h="7" * 64,
        m="8" * 64,
    )


def test_empty_round_id_rejected(rollback_conn: Connection):
    _expect_error(
        rollback_conn,
        "INSERT INTO runtime.decisions (round_id, decision_hash, decision_manifest_hash) "
        "VALUES ('', :h, :m)",
        h="7" * 64,
        m="8" * 64,
    )
