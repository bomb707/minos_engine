"""The real decision write path against real PostgreSQL 16.

The operational profile rows are seeded here from the PROVEN corpus identities -- every field the
resolver cross-checks comes from ``OwnedRoundProfile``, the rest is structurally valid filler
standing in for what Layer 1 ingestion writes. The scratch database has to be named
``minos_engine_db``: the write path refuses any other database by live ``current_database()``.
"""

from __future__ import annotations

import hashlib
import json
import threading

import pytest
from sqlalchemy import text

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.layer2.contracts import ControlMode
from minos_engine.layer2.round_profile_authority import (
    OwnedRoundProfile,
    load_verified_round_profile_corpus,
)
from minos_engine.layer2.safe_controller import (
    load_verified_safe_baseline_authority,
    safe_decision_manifest_identity,
)
from minos_engine.layer2.safe_controller_qualification import _request_for
from minos_engine.storage.database import create_db_engine
from minos_engine.storage.decision_persistence import (
    SafeDecisionPersistenceError,
    persist_safe_decision,
    provision_safe_config_row,
    resolve_owned_profile_row,
    resolve_safe_config_row,
)
from minos_engine.storage.runtime_decision_contract import (
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_REVISION,
)
from minos_engine.storage.runtime_overlay import upgrade_runtime_overlay
from tests.conftest import REPO_ROOT

from .conftest import alembic_upgrade, scratch_database

_HEX = "0123456789abcdef"


def _filler(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _seed_owned_profile(engine, owned) -> None:
    """Write the operational rows Layer 1 ingestion would have written for this member."""
    registry_id = _uuid(engine)
    artifacts = {}
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                "INSERT INTO catalog.dataset_registry (id, dataset_id, round_id, chromosome, "
                "region_source, region_start0, region_end0_exclusive, region_length_bp, "
                "region_coordinate_system, region_hash, bam_sha256, bai_sha256, "
                "reference_sha256, fai_sha256, bam_size_bytes, parameter_space_hash, "
                "feature_registry_hash, identity_tuple_hash, manifest_hash, "
                "split_algorithm_version, split_salt, allocation_digest) "
                "VALUES (:id, :dataset_id, :round_id, :chromosome, 'seeded:0-1000', 0, 1000, "
                "1000, 'zero_based_half_open', :region_hash, :bam, :bai, :ref, :fai, 1, :ps, "
                ":fr, :ith, :mh, 'v', 's', :ad)"
            ),
            {
                "id": registry_id,
                "dataset_id": owned.dataset_id,
                "round_id": owned.round_id,
                "chromosome": owned.chromosome,
                "region_hash": owned.region_hash,
                "bam": owned.bam_sha256,
                "bai": owned.bai_sha256,
                "ref": owned.reference_sha256,
                "fai": owned.fai_sha256,
                "ps": _filler(f"ps-{owned.round_id}"),
                "fr": _filler(f"fr-{owned.round_id}"),
                "ith": owned.identity_tuple_hash,
                "mh": _filler(f"mh-{owned.round_id}"),
                "ad": _filler(f"ad-{owned.round_id}"),
            },
        )
        for kind in ("profile", "manifest", "windows"):
            artifact_id = _uuid(engine)
            artifacts[kind] = artifact_id
            conn.execute(
                text(
                    "INSERT INTO catalog.artifacts (id, uri, sha256, media_type, size_bytes, "
                    "provenance) VALUES (:id, :uri, :sha, 'application/json', 1, 'seed')"
                ),
                {
                    "id": artifact_id,
                    "uri": f"seed://{owned.round_id}/{kind}",
                    "sha": _filler(f"{kind}-{owned.round_id}"),
                },
            )
        conn.execute(
            text(
                "INSERT INTO profiling.bam_profiles (dataset_registry_id, profile_id, "
                "bam_sha256, bai_sha256, reference_sha256, fai_sha256, region_hash, "
                "identity_tuple_hash, m5_status, integrity_degraded, attestation_hash, "
                "registry_snapshot_hash, profile_status, profiler_version, "
                "profiler_config_hash, windows_row_count, feature_values_hash, "
                "l1_feature_values_hash, eligible_value_count, profile_document, "
                "profile_sha256, profile_manifest_sha256, windows_sha256, profile_artifact_id, "
                "profile_manifest_artifact_id, windows_artifact_id, ingestion_key, "
                "content_hash) VALUES (:reg, :pid, :bam, :bai, :ref, :fai, :region_hash, :ith, "
                ":m5, :degraded, :att, :snap, 'COMPLETE', 'layer1-profiler-v1', :pch, 1, :fvh, "
                ":lfvh, 1, '{}'::jsonb, :psha, :pmsha, :wsha, :a1, :a2, :a3, :ik, :ch)"
            ),
            {
                "reg": registry_id,
                "pid": owned.profile_id,
                "bam": owned.bam_sha256,
                "bai": owned.bai_sha256,
                "ref": owned.reference_sha256,
                "fai": owned.fai_sha256,
                "region_hash": owned.region_hash,
                "ith": owned.identity_tuple_hash,
                "m5": "ABSENT" if owned.integrity_degraded else "MATCH",
                "degraded": owned.integrity_degraded,
                "att": owned.attestation_hash,
                "snap": owned.registry_snapshot_hash,
                "pch": _filler(f"pch-{owned.round_id}"),
                "fvh": _filler(f"fvh-{owned.round_id}"),
                "lfvh": _filler(f"lfvh-{owned.round_id}"),
                "psha": owned.profile_sha256,
                "pmsha": owned.profile_manifest_sha256,
                "wsha": _filler(f"w-{owned.round_id}"),
                "a1": artifacts["profile"],
                "a2": artifacts["manifest"],
                "a3": artifacts["windows"],
                "ik": _filler(f"ik-{owned.round_id}"),
                "ch": _filler(f"ch-{owned.round_id}"),
            },
        )


def _uuid(engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT gen_random_uuid()")).scalar())


@pytest.fixture(scope="module")
def authority():
    return load_verified_safe_baseline_authority(repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def ownership():
    return load_verified_round_profile_corpus(root=REPO_ROOT)


def _live_role_url(url: str) -> str:
    """The same database on a connection that has ASSUMED minos_live."""
    from sqlalchemy.engine import make_url

    from minos_engine.storage.database import normalize_database_url

    return (
        make_url(normalize_database_url(url))
        .update_query_dict({"options": "-c role=minos_live"})
        .render_as_string(hide_password=False)
    )


@pytest.fixture
def store(isolated_pg_base_url: str, authority, ownership):
    """A provisioned operational store at the overlay HEAD, plus an admin engine for drills."""
    with scratch_database(isolated_pg_base_url, "minos_engine_db") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        upgrade_runtime_overlay(url)
        admin = create_db_engine(url)
        try:
            provision_safe_config_row(
                admin,
                config_hash=authority.baseline_config_hash,
                parameter_space_hash=authority.parameter_space_hash,
            )
            for round_id in ownership.rounds()[:3]:
                _seed_owned_profile(admin, ownership.owned(round_id))
            yield url, admin
        finally:
            admin.dispose()


@pytest.fixture
def live(store):
    """The engine the write path actually runs on: least privilege, not superuser.

    v1 ran its whole campaign as the owner. That is why it could certify grants it had never
    exercised, and why it never noticed that the live role could not read the schema-version
    tables at all.
    """
    url, _admin = store
    engine = create_db_engine(_live_role_url(url))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def admin(store):
    _url, engine = store
    return engine


def _persist(engine, authority, ownership, round_id, mode=ControlMode.SAFE_BASELINE):
    owned = ownership.owned(round_id)
    return persist_safe_decision(
        request=_request_for(owned, authority=authority, mode=mode),
        authority=authority,
        ownership=ownership,
        engine=engine,
        root=REPO_ROOT,
    )


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_a_decision_is_committed_and_reads_back_as_itself(live, authority, ownership):
    round_id = ownership.rounds()[0]
    persisted = _persist(live, authority, ownership, round_id)
    assert not persisted.converged
    assert persisted.mode == "SAFE_BASELINE"
    assert persisted.row["model_bundle_id"] is None
    assert persisted.row["config_id"] is None
    assert persisted.row["selected_config_id"] == persisted.row["baseline_config_id"]

    with live.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM runtime.decisions WHERE decision_hash = :h"),
                {"h": persisted.decision_hash},
            )
            .mappings()
            .first()
        )
    assert row is not None
    manifest = dict(row["decision_manifest"])
    # both hashes re-derive from the stored JSONB: the identity AND the exact bytes
    assert safe_decision_manifest_identity(manifest) == persisted.decision_hash
    assert (
        hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
        == persisted.decision_manifest_hash
    )
    assert persisted.decision_hash != persisted.decision_manifest_hash
    assert row["decided_at"] is not None
    assert manifest["selected_config_hash"] == authority.baseline_config_hash


def test_the_four_requested_modes_make_four_decisions_for_one_round(live, authority, ownership):
    round_id = ownership.rounds()[0]
    made = [_persist(live, authority, ownership, round_id, mode) for mode in ControlMode]
    assert len({p.decision_hash for p in made}) == 4
    assert {p.mode for p in made} == {"SAFE_BASELINE"}
    with live.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT requested_mode, fallback_reason FROM runtime.decisions "
                    "WHERE round_id = :r ORDER BY decided_at DESC"
                ),
                {"r": round_id},
            )
        )
    assert len(rows) == 4
    assert {r[0] for r in rows} == {m.value for m in ControlMode}
    assert {r[1] for r in rows} == {"NONE", "SAFE_BASELINE_FORCED"}


def test_the_same_decision_converges_instead_of_duplicating(live, authority, ownership):
    round_id = ownership.rounds()[1]
    first = _persist(live, authority, ownership, round_id)
    again = _persist(live, authority, ownership, round_id)
    assert not first.converged and again.converged
    assert again.persisted_id == first.persisted_id
    assert again.row["decided_at"] == first.row["decided_at"], "the first writer's time stands"


def test_concurrent_identical_writers_converge_on_one_row(live, authority, ownership):
    round_id = ownership.rounds()[2]
    results, errors = [], []
    barrier = threading.Barrier(4)

    def _worker() -> None:
        try:
            barrier.wait(timeout=60)
            results.append(_persist(live, authority, ownership, round_id))
        except Exception as error:
            errors.append(repr(error))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert errors == []
    assert len(results) == 4
    assert len({r.persisted_id for r in results}) == 1
    assert sum(1 for r in results if not r.converged) == 1
    with live.connect() as conn:
        assert (
            int(
                conn.execute(
                    text("SELECT count(*) FROM runtime.decisions WHERE round_id = :r"),
                    {"r": round_id},
                ).scalar()
                or 0
            )
            == 1
        )


# --------------------------------------------------------------------------- #
# fail-closed
# --------------------------------------------------------------------------- #
def test_a_missing_safe_config_row_fails_closed(isolated_pg_base_url, authority, ownership):
    with scratch_database(isolated_pg_base_url, "minos_engine_db") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        upgrade_runtime_overlay(url)
        engine = create_db_engine(url)
        try:
            _seed_owned_profile(engine, ownership.owned(ownership.rounds()[0]))
            with pytest.raises(SafeDecisionPersistenceError, match="catalog.gatk_configs"):
                _persist(engine, authority, ownership, ownership.rounds()[0])
        finally:
            engine.dispose()


def test_a_missing_profile_row_fails_closed(live, authority, ownership):
    unseeded = ownership.rounds()[10]
    with pytest.raises(SafeDecisionPersistenceError, match="does not resolve for round"):
        _persist(live, authority, ownership, unseeded)


def test_a_profile_row_that_disagrees_is_refused(live, ownership):
    owned = ownership.owned(ownership.rounds()[0])
    with live.connect() as conn:
        assert resolve_owned_profile_row(conn, owned=owned)
        for field in ("bam_sha256", "profile_sha256", "attestation_hash", "region_hash"):
            mutated = OwnedRoundProfile(**{**owned.content(), field: "0" * 64})
            with pytest.raises(SafeDecisionPersistenceError, match=field):
                resolve_owned_profile_row(conn, owned=mutated)
        wrong_dataset = OwnedRoundProfile(**{**owned.content(), "dataset_id": "not-this-one"})
        with pytest.raises(SafeDecisionPersistenceError, match="dataset"):
            resolve_owned_profile_row(conn, owned=wrong_dataset)


def test_a_config_row_with_a_foreign_parameter_space_is_refused(live, authority):
    with live.connect() as conn:
        assert resolve_safe_config_row(
            conn,
            config_hash=authority.baseline_config_hash,
            parameter_space_hash=authority.parameter_space_hash,
        )
        with pytest.raises(SafeDecisionPersistenceError, match="parameter space"):
            resolve_safe_config_row(
                conn,
                config_hash=authority.baseline_config_hash,
                parameter_space_hash="0" * 64,
            )


def test_a_caller_supplied_config_uuid_is_never_used(live, authority, ownership):
    """Resolution is by accepted hash; the row's UUID is discovered, never accepted."""
    persisted = _persist(live, authority, ownership, ownership.rounds()[0])
    with live.connect() as conn:
        expected = resolve_safe_config_row(
            conn,
            config_hash=authority.baseline_config_hash,
            parameter_space_hash=authority.parameter_space_hash,
        )
    assert persisted.selected_config_id == expected


def test_the_write_path_refuses_a_database_that_is_not_the_operational_store(
    pg_base_url, authority, ownership
):
    with scratch_database(pg_base_url, "minos_not_the_operational_store") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        upgrade_runtime_overlay(url)
        engine = create_db_engine(_live_role_url(url))
        try:
            with pytest.raises(SafeDecisionPersistenceError, match="canonical operational store"):
                _persist(engine, authority, ownership, ownership.rounds()[0])
        finally:
            engine.dispose()


@pytest.mark.parametrize("stop_at", [None, RUNTIME_OVERLAY_REVISION])
def test_the_write_path_refuses_a_store_below_the_overlay_head(
    isolated_pg_base_url, authority, ownership, stop_at
):
    """Including r0001: a store there still grants the live role the raw identity tables."""
    with scratch_database(isolated_pg_base_url, "minos_engine_db") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        if stop_at is not None:
            upgrade_runtime_overlay(url, stop_at)
        engine = create_db_engine(url)
        try:
            with pytest.raises(SafeDecisionPersistenceError, match="runtime overlay"):
                _persist(engine, authority, ownership, ownership.rounds()[0])
        finally:
            engine.dispose()


def test_a_divergent_stored_row_under_the_same_identity_fails_closed(
    live, admin, authority, ownership
):
    round_id = ownership.rounds()[1]
    owned = ownership.owned(round_id)
    from minos_engine.layer2.safe_controller import safe_decision_manifest_content

    request = _request_for(owned, authority=authority, mode=ControlMode.REFINEMENT)
    manifest = safe_decision_manifest_content(
        request=request, authority=authority, ownership=ownership
    )
    identity = safe_decision_manifest_identity(manifest)
    forged = {**manifest, "profile_sha256": "0" * 64}
    with live.connect() as conn:
        config_id = resolve_safe_config_row(
            conn,
            config_hash=authority.baseline_config_hash,
            parameter_space_hash=authority.parameter_space_hash,
        )
        profile_id = resolve_owned_profile_row(conn, owned=owned)
    with admin.begin() as conn:
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                "INSERT INTO runtime.decisions (round_id, decision_hash, "
                "decision_manifest_hash, profile_id, controller_version, mode, requested_mode, "
                "fallback_reason, parameter_space_hash, baseline_config_id, selected_config_id, "
                "decision_manifest) VALUES (:r, :h, :mh, :p, :cv, :m, :rm, :fr, :ps, :b, :s, "
                "CAST(:doc AS jsonb))"
            ),
            {
                "r": round_id,
                "h": identity,
                "mh": "e" * 64,
                "p": profile_id,
                "cv": manifest["controller_version"],
                "m": manifest["actual_mode"],
                "rm": manifest["requested_mode"],
                "fr": manifest["fallback_reason"],
                "ps": manifest["parameter_space_hash"],
                "b": config_id,
                "s": config_id,
                "doc": json.dumps(forged),
            },
        )
    with pytest.raises(SafeDecisionPersistenceError, match="not the manifest that was decided"):
        _persist(live, authority, ownership, round_id, ControlMode.REFINEMENT)


def test_one_decision_identity_may_name_only_one_decision(live, admin, authority, ownership):
    from sqlalchemy.exc import IntegrityError

    persisted = _persist(live, authority, ownership, ownership.rounds()[0])
    with live.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM runtime.decisions WHERE decision_hash = :h"),
                {"h": persisted.decision_hash},
            )
            .mappings()
            .first()
        )
    assert row is not None
    with (
        pytest.raises(IntegrityError, match="uq_decisions_decision_hash"),
        admin.begin() as conn,
    ):
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        conn.execute(
            text(
                "INSERT INTO runtime.decisions (round_id, decision_hash, "
                "decision_manifest_hash, profile_id, controller_version, mode, "
                "requested_mode, fallback_reason, parameter_space_hash, baseline_config_id, "
                "selected_config_id, decision_manifest) SELECT 'another-round', "
                "decision_hash, decision_manifest_hash, profile_id, controller_version, "
                "mode, requested_mode, fallback_reason, parameter_space_hash, "
                "baseline_config_id, selected_config_id, "
                "jsonb_set(decision_manifest, '{round_id}', '\"another-round\"') "
                "FROM runtime.decisions WHERE decision_hash = :h"
            ),
            {"h": persisted.decision_hash},
        )


def test_the_decision_is_never_reported_before_it_is_committed(live, authority, ownership):
    """A driver-level failure after the INSERT must leave nothing visible."""
    from sqlalchemy import event

    round_id = ownership.rounds()[2]
    state = {"inserted": False, "fired": False}

    def _poison(conn, cursor, statement, parameters, context, executemany):
        lowered = " ".join(str(statement).split()).lower()
        if lowered.startswith("insert into runtime.decisions"):
            state["inserted"] = True
        elif lowered.startswith("select") and "runtime.decisions" in lowered:
            state["fired"] = True
            raise RuntimeError("injected failure between INSERT and COMMIT")

    with live.connect() as conn:
        before = int(conn.execute(text("SELECT count(*) FROM runtime.decisions")).scalar() or 0)
    event.listen(live, "before_cursor_execute", _poison)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            _persist(live, authority, ownership, round_id)
    finally:
        event.remove(live, "before_cursor_execute", _poison)
    assert state["inserted"] and state["fired"]
    with live.connect() as conn:
        after = int(conn.execute(text("SELECT count(*) FROM runtime.decisions")).scalar() or 0)
    assert after == before


# --------------------------------------------------------------------------- #
# privileges
# --------------------------------------------------------------------------- #
def _denied(conn, role: str, sql: str, sqlstate: str = "42501") -> bool:
    from sqlalchemy.exc import DBAPIError

    savepoint = conn.begin_nested()
    try:
        conn.execute(text(f"SET ROLE {role}"))
        conn.execute(text(sql))
    except DBAPIError as error:
        return str(getattr(getattr(error, "orig", None), "sqlstate", None)) == sqlstate
    else:
        return False
    finally:
        savepoint.rollback()
        conn.execute(text("RESET ROLE"))


def test_the_live_role_can_append_a_decision_and_do_nothing_else_to_it(live, authority, ownership):
    _persist(live, authority, ownership, ownership.rounds()[0])
    with live.connect() as conn:
        transaction = conn.begin()
        try:
            savepoint = conn.begin_nested()
            conn.execute(text("SET ROLE minos_live"))
            conn.execute(
                text(
                    "INSERT INTO runtime.decisions (round_id, decision_hash, "
                    "decision_manifest_hash, profile_id, controller_version, mode, "
                    "requested_mode, fallback_reason, parameter_space_hash, baseline_config_id, "
                    "selected_config_id, decision_manifest) SELECT round_id, repeat('a', 64), "
                    "decision_manifest_hash, profile_id, controller_version, mode, "
                    "requested_mode, fallback_reason, parameter_space_hash, baseline_config_id, "
                    "selected_config_id, decision_manifest FROM runtime.decisions LIMIT 1"
                )
            )
            savepoint.rollback()
            conn.execute(text("RESET ROLE"))
            assert _denied(conn, "minos_live", "UPDATE runtime.decisions SET round_id = 'x'")
            assert _denied(conn, "minos_live", "DELETE FROM runtime.decisions")
            assert _denied(conn, "minos_live", "TRUNCATE runtime.decisions")
            assert _denied(conn, "minos_live", "SELECT count(*) FROM evaluation.evaluations")
            assert _denied(
                conn,
                "minos_live",
                "INSERT INTO catalog.gatk_configs (config_hash, parameter_space_hash) "
                "VALUES (repeat('c',64), repeat('d',64))",
            )
            assert _denied(conn, "minos_live", "UPDATE profiling.bam_profiles SET profile_id = 'x'")
        finally:
            transaction.rollback()


@pytest.mark.parametrize("role", ["minos_runner", "minos_evaluator", "minos_trainer"])
def test_no_other_role_can_forge_a_live_decision(admin, role: str):
    with admin.connect() as conn:
        transaction = conn.begin()
        try:
            assert _denied(conn, role, "INSERT INTO runtime.decisions (round_id) VALUES ('forged')")
            assert _denied(conn, role, "SELECT count(*) FROM runtime.decisions")
        finally:
            transaction.rollback()


def test_even_the_owner_cannot_rewrite_a_persisted_decision(live, admin, authority, ownership):
    _persist(live, authority, ownership, ownership.rounds()[0])
    with admin.connect() as conn:
        transaction = conn.begin()
        try:
            assert _denied(
                conn, "minos_admin", "UPDATE runtime.decisions SET round_id = 'x'", "23001"
            )
            assert _denied(conn, "minos_admin", "DELETE FROM runtime.decisions", "23001")
        finally:
            transaction.rollback()


def test_public_holds_nothing_in_the_application_schemas(admin):
    with admin.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT table_schema, table_name FROM information_schema.role_table_grants "
                    "WHERE grantee = 'PUBLIC' AND table_schema IN ('catalog','profiling',"
                    "'experiments','evaluation','models','runtime','audit')"
                )
            )
        )
    assert rows == []
