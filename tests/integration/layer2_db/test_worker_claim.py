"""Two concurrent PostgreSQL sessions: FOR UPDATE SKIP LOCKED claim safety."""

from __future__ import annotations

from sqlalchemy import text

from minos_engine.storage.database import create_db_engine, make_session_factory
from minos_engine.storage.repositories import claim_next_job

from . import _helpers as H
from .conftest import alembic_upgrade, scratch_database


def _seed_jobs(engine, n: int) -> None:
    with engine.begin() as c:
        pid = H.insert_profile(c)
        cid = H.insert_config(c)
        for i in range(n):
            H.insert_job(c, pid, cid, job_key=f"job{i}")


def test_single_claim_and_skip_locked(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2b_claim1") as url:
        alembic_upgrade(url, "0008_l2f_execution_results")
        engine = create_db_engine(url)
        try:
            _seed_jobs(engine, 1)
            sf = make_session_factory(engine)
            s1, s2 = sf(), sf()
            try:
                j1 = claim_next_job(s1, "w1")  # locks the row
                j2 = claim_next_job(s2, "w2")  # skips the locked row
                assert j1 is not None and j1.claimed_by == "w1"
                assert j2 is None  # no duplicate claim
                s1.commit()
                s2.rollback()
            finally:
                s1.close()
                s2.close()
            with engine.connect() as c:
                status = c.execute(text("SELECT status FROM experiments.jobs")).scalar()
                assert status == "CLAIMED"
        finally:
            engine.dispose()


def test_two_workers_claim_distinct_jobs(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2b_claim2") as url:
        alembic_upgrade(url, "0008_l2f_execution_results")
        engine = create_db_engine(url)
        try:
            _seed_jobs(engine, 2)
            sf = make_session_factory(engine)
            s1, s2 = sf(), sf()
            try:
                j1 = claim_next_job(s1, "w1")
                j2 = claim_next_job(s2, "w2")
                assert j1 is not None and j2 is not None
                assert j1.id != j2.id  # distinct rows, no duplicate
                s1.commit()
                s2.commit()
            finally:
                s1.close()
                s2.close()
        finally:
            engine.dispose()


def test_rollback_releases_claim(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2b_claim3") as url:
        alembic_upgrade(url, "0008_l2f_execution_results")
        engine = create_db_engine(url)
        try:
            _seed_jobs(engine, 1)
            sf = make_session_factory(engine)
            s1 = sf()
            claimed_id = None
            try:
                j1 = claim_next_job(s1, "w1")
                assert j1 is not None
                claimed_id = j1.id
                s1.rollback()  # release without committing
            finally:
                s1.close()
            s2 = sf()
            try:
                j2 = claim_next_job(s2, "w2")
                assert j2 is not None and j2.id == claimed_id  # available again
                s2.commit()
            finally:
                s2.close()
        finally:
            engine.dispose()
