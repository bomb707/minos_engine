"""Transaction boundaries: rollback, no unexpected commits, reusable sessions."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from minos_engine.storage.database import session_scope
from minos_engine.storage.repositories import ArtifactRepository

from . import _helpers as H


def test_repository_does_not_autocommit(session_factory):
    sha = "a" * 64
    s = session_factory()
    try:
        ArtifactRepository(s).add(uri="u", sha256=sha)
        s.rollback()  # discard without committing
    finally:
        s.close()
    check = session_factory()
    try:
        assert ArtifactRepository(check).get_by_sha256(sha) is None
    finally:
        check.close()


def test_session_scope_commits_on_success(session_factory):
    sha = "b" * 64
    with session_scope(session_factory) as s:
        ArtifactRepository(s).add(uri="u", sha256=sha)
    check = session_factory()
    try:
        got = ArtifactRepository(check).get_by_sha256(sha)
        assert got is not None and got.sha256 == sha
        # cleanup (admin/superuser) so the shared DB stays clean
        check.execute(text("DELETE FROM catalog.artifacts WHERE sha256 = :s"), {"s": sha})
        check.commit()
    finally:
        check.close()


def test_failed_insert_rolls_back_and_session_reusable(session_factory):
    with pytest.raises(IntegrityError), session_scope(session_factory) as s:
        ArtifactRepository(s).add(uri="u", sha256="c" * 64)
        ArtifactRepository(s).add(uri="u2", sha256="c" * 64)  # duplicate -> IntegrityError
    # nothing persisted, and a fresh session is fully usable
    reuse = session_factory()
    try:
        assert ArtifactRepository(reuse).get_by_sha256("c" * 64) is None
        H.insert_profile(reuse.connection())  # raw op on a reused session works
        reuse.rollback()
    finally:
        reuse.close()
