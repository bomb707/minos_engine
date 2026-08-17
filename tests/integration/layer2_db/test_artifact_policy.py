"""Artifact policy: URI + SHA-256 only, unique, valid shape — no binary payloads."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DataError, IntegrityError

from . import _helpers as H


def test_no_binary_or_large_object_columns(rollback_conn: Connection):
    rows = rollback_conn.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'catalog' AND table_name = 'artifacts'"
        )
    ).all()
    types = {r[0]: r[1] for r in rows}
    assert "bytea" not in types.values()  # never store bytes in PostgreSQL
    assert {"uri", "sha256"} <= set(types)
    assert types["sha256"] == "character"  # CHAR(64)


def test_artifact_unique_sha_enforced(rollback_conn: Connection):
    H.insert_artifact(rollback_conn, uri="u1", sha256="a" * 64)
    with pytest.raises((IntegrityError, DataError)), rollback_conn.begin_nested():
        rollback_conn.execute(
            text("INSERT INTO catalog.artifacts (uri, sha256) VALUES ('u2', :h)"),
            {"h": "a" * 64},
        )


def test_artifact_size_and_media_metadata_optional(rollback_conn: Connection):
    rollback_conn.execute(
        text(
            "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes) "
            "VALUES ('u', :h, 'application/json', 1024)"
        ),
        {"h": "b" * 64},
    )
    got = rollback_conn.execute(
        text("SELECT media_type, size_bytes FROM catalog.artifacts WHERE sha256 = :h"),
        {"h": "b" * 64},
    ).one()
    assert got[0] == "application/json" and got[1] == 1024
