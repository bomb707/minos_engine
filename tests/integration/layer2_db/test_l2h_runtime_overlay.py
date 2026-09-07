"""The operational runtime overlay against real PostgreSQL 16: lifecycle, refusal, privileges."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.runtime_decision_contract import (
    ADDED_CHECK_CONSTRAINTS,
    FROZEN_INVENTORY,
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_REVISION,
    runtime_overlay_revision,
)
from minos_engine.storage.runtime_overlay import (
    downgrade_runtime_overlay,
    main_lineage_revision,
    observed_overlay_state,
    upgrade_runtime_overlay,
)

from .conftest import alembic_upgrade, scratch_database

#: How the overlay refuses. A RuntimeError is its own precondition; a database error is
#: PostgreSQL refusing to build the version table in a schema that does not exist.
REFUSALS = (RuntimeError, SQLAlchemyError)

_GRANT_QUERY = (
    "SELECT table_schema || '.' || table_name || ':' || grantee || ':' || privilege_type "
    "FROM information_schema.role_table_grants WHERE grantee LIKE 'minos_%' "
    "AND table_schema IN ('catalog','profiling','experiments','evaluation','models','runtime',"
    "'audit')"
)


def _snapshot(url: str) -> dict[str, list[str]]:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return {
                "columns": sorted(
                    f"{r[0]}:{r[1]}"
                    for r in conn.execute(
                        text(
                            "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_schema='runtime' AND table_name='decisions'"
                        )
                    )
                ),
                "constraints": sorted(
                    str(r[0])
                    for r in conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid='runtime.decisions'::regclass"
                        )
                    )
                ),
                "indexes": sorted(
                    str(r[0])
                    for r in conn.execute(
                        text(
                            "SELECT indexname FROM pg_indexes WHERE schemaname='runtime' "
                            "AND tablename='decisions'"
                        )
                    )
                ),
                "grants": sorted(str(r[0]) for r in conn.execute(text(_GRANT_QUERY))),
            }
    finally:
        engine.dispose()


@pytest.fixture
def operational_url(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2h_overlay") as url:
        alembic_upgrade(url, REQUIRED_MAIN_REVISION)
        yield url


def test_the_overlay_completes_the_table_to_the_contract(operational_url: str):
    before = _snapshot(operational_url)
    upgrade_runtime_overlay(operational_url)
    after = _snapshot(operational_url)

    added = {c.split(":")[0] for c in after["columns"]} - {
        c.split(":")[0] for c in before["columns"]
    }
    assert added == set(FROZEN_INVENTORY["added_columns"])
    assert "profile_id:NO" in after["columns"], "profile_id must become NOT NULL"
    assert "profile_id:YES" in before["columns"]

    new_constraints = set(after["constraints"]) - set(before["constraints"])
    assert set(ADDED_CHECK_CONSTRAINTS) <= new_constraints
    assert "uq_decisions_decision_hash" in new_constraints
    assert "fk_decisions_baseline_config_id_gatk_configs" in new_constraints
    assert "fk_decisions_selected_config_id_gatk_configs" in new_constraints
    # the profile FK is re-aimed at the table the operational pipeline actually writes
    assert "fk_decisions_profile_id_bam_profiles" in new_constraints
    assert "fk_decisions_profile_id_profiles" in set(before["constraints"]) - set(
        after["constraints"]
    )
    assert "ix_decisions_round_id_decided_at" in set(after["indexes"]) - set(before["indexes"])


def test_the_overlay_widens_the_privilege_matrix_by_exactly_two_reads(operational_url: str):
    before = _snapshot(operational_url)["grants"]
    upgrade_runtime_overlay(operational_url)
    after = _snapshot(operational_url)["grants"]
    assert sorted(set(after) - set(before)) == [
        "catalog.dataset_registry:minos_live:SELECT",
        "profiling.bam_profiles:minos_live:SELECT",
    ]
    assert set(before) - set(after) == set()


def test_the_overlay_never_advances_the_main_lineage(operational_url: str):
    engine = create_engine(normalize_database_url(operational_url))
    try:
        with engine.connect() as conn:
            assert main_lineage_revision(conn) == REQUIRED_MAIN_REVISION
            assert runtime_overlay_revision(conn) is None
        upgrade_runtime_overlay(operational_url)
        with engine.connect() as conn:
            assert main_lineage_revision(conn) == REQUIRED_MAIN_REVISION
            assert runtime_overlay_revision(conn) == RUNTIME_OVERLAY_REVISION
            rows = int(
                conn.execute(text("SELECT count(*) FROM public.alembic_version")).scalar() or 0
            )
            assert rows == 1, "the main version table must still hold exactly one revision"
    finally:
        engine.dispose()


def test_downgrade_restores_the_schema_and_the_grants_exactly(operational_url: str):
    before = _snapshot(operational_url)
    upgrade_runtime_overlay(operational_url)
    downgrade_runtime_overlay(operational_url)
    assert _snapshot(operational_url) == before
    assert observed_overlay_state(operational_url)["overlay_revision"] is None
    # and it can be applied again afterwards
    upgrade_runtime_overlay(operational_url)
    assert observed_overlay_state(operational_url)["overlay_revision"] == RUNTIME_OVERLAY_REVISION


@pytest.mark.parametrize(
    "revision",
    ["0001_l2b_initial", "0020_l2f2_phase_c_execution", "0026_l2f2_phase_d_closure"],
)
def test_the_overlay_refuses_every_schema_that_is_not_the_accepted_one(
    pg_base_url: str, revision: str
):
    """This is what structurally keeps the overlay off the TRAIN and VALIDATION stores."""
    with scratch_database(pg_base_url, "minos_l2h_foreign") as url:
        alembic_upgrade(url, revision)
        with pytest.raises(REFUSALS):
            upgrade_runtime_overlay(url)
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                assert runtime_overlay_revision(conn) is None
                assert main_lineage_revision(conn) == revision
                added = int(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM information_schema.columns WHERE "
                            "table_schema='runtime' AND table_name='decisions' AND "
                            "column_name = ANY(ARRAY['mode','decision_manifest','decided_at'])"
                        )
                    ).scalar()
                    or 0
                )
                assert added == 0, "nothing may be applied to a schema the overlay refuses"
        finally:
            engine.dispose()


def test_the_overlay_refuses_a_table_that_already_holds_decisions(operational_url: str):
    engine = create_engine(normalize_database_url(operational_url))
    try:
        with engine.begin() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            conn.execute(
                text(
                    "INSERT INTO runtime.decisions (round_id, decision_hash, "
                    "decision_manifest_hash) VALUES ('r', repeat('a',64), repeat('b',64))"
                )
            )
        with pytest.raises(REFUSALS):
            upgrade_runtime_overlay(operational_url)
        with engine.connect() as conn:
            assert runtime_overlay_revision(conn) is None
    finally:
        engine.dispose()


def test_the_overlay_objects_are_owned_by_the_non_superuser_admin_role(operational_url: str):
    upgrade_runtime_overlay(operational_url)
    engine = create_engine(normalize_database_url(operational_url))
    try:
        with engine.connect() as conn:
            owner = conn.execute(
                text(
                    "SELECT tableowner FROM pg_tables WHERE schemaname='runtime' "
                    "AND tablename='decisions'"
                )
            ).scalar()
            assert owner == "minos_admin"
            assert (
                conn.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname='minos_admin'")
                ).scalar()
                is False
            )
            index_owner = conn.execute(
                text(
                    "SELECT tableowner FROM pg_tables t JOIN pg_indexes i "
                    "ON i.tablename = t.tablename WHERE i.indexname = "
                    "'ix_decisions_round_id_decided_at'"
                )
            ).scalar()
            assert index_owner == "minos_admin"
            assert (
                int(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                            "ON n.oid = p.pronamespace WHERE p.prosecdef AND n.nspname='runtime'"
                        )
                    ).scalar()
                    or 0
                )
                == 0
            ), "no SECURITY DEFINER function is introduced"
    finally:
        engine.dispose()


def test_the_database_refuses_a_row_that_disagrees_with_its_own_manifest(operational_url: str):
    """The manifest bindings are CHECK constraints, not writer discipline."""
    upgrade_runtime_overlay(operational_url)
    engine = create_engine(normalize_database_url(operational_url))
    base = {
        "round_id": "r-1",
        "decision_hash": "a" * 64,
        "decision_manifest_hash": "b" * 64,
        "controller_version": "l2h-safe-baseline-controller-v1",
        "mode": "SAFE_BASELINE",
        "requested_mode": "SAFE_BASELINE",
        "fallback_reason": "NONE",
        "parameter_space_hash": "c" * 64,
    }
    manifest = {
        "schema_version": "l2h-safe-decision-manifest-v1",
        "round_id": "r-1",
        "actual_mode": "SAFE_BASELINE",
        "requested_mode": "SAFE_BASELINE",
        "fallback_reason": "NONE",
        "controller_version": "l2h-safe-baseline-controller-v1",
        "parameter_space_hash": "c" * 64,
        "model_bundle_loaded": False,
    }

    def _insert(overrides: dict, manifest_overrides: dict) -> None:
        import json as _json

        values = {**base, **overrides}
        document = {**manifest, **manifest_overrides}
        with engine.begin() as conn:
            conn.execute(text("SET ROLE minos_admin"))
            conn.execute(
                text(
                    "INSERT INTO runtime.decisions (round_id, decision_hash, "
                    "decision_manifest_hash, profile_id, controller_version, mode, "
                    "requested_mode, fallback_reason, parameter_space_hash, baseline_config_id, "
                    "selected_config_id, decision_manifest) VALUES (:round_id, :decision_hash, "
                    ":decision_manifest_hash, gen_random_uuid(), :controller_version, :mode, "
                    ":requested_mode, :fallback_reason, :parameter_space_hash, "
                    "gen_random_uuid(), gen_random_uuid(), CAST(:m AS jsonb))"
                ),
                {**values, "m": _json.dumps(document)},
            )

    try:
        for label, overrides, manifest_overrides in (
            ("round_id", {}, {"round_id": "somewhere-else"}),
            ("actual_mode", {}, {"actual_mode": "BOUNDED"}),
            ("requested_mode", {}, {"requested_mode": "BOUNDED"}),
            ("fallback_reason", {}, {"fallback_reason": "LOW_CONFIDENCE"}),
            ("controller_version", {}, {"controller_version": "someone-elses"}),
            ("parameter_space_hash", {}, {"parameter_space_hash": "d" * 64}),
            ("schema_version", {}, {"schema_version": "l2h-safe-decision-manifest-v2"}),
            ("model_bundle_loaded", {}, {"model_bundle_loaded": True}),
            (
                "fallback must explain a mode change",
                {"requested_mode": "BOUNDED"},
                {"requested_mode": "BOUNDED"},
            ),
            ("config_id is retired", {"config_id": None}, {}),
        ):
            if label == "config_id is retired":
                continue
            with pytest.raises(IntegrityError, match="(?i)violates check constraint"):
                _insert(overrides, manifest_overrides)
    finally:
        engine.dispose()


def test_safe_mode_can_never_imply_that_a_contextual_model_executed(operational_url: str):
    upgrade_runtime_overlay(operational_url)
    engine = create_engine(normalize_database_url(operational_url))
    try:
        with (
            pytest.raises(IntegrityError, match="ck_decisions_safe_mode_has_no_bundle"),
            engine.begin() as conn,
        ):
            conn.execute(text("SET LOCAL ROLE minos_admin"))
            conn.execute(
                text(
                    "INSERT INTO runtime.decisions (round_id, decision_hash, "
                    "decision_manifest_hash, profile_id, model_bundle_id, "
                    "controller_version, mode, requested_mode, fallback_reason, "
                    "parameter_space_hash, baseline_config_id, selected_config_id, "
                    "decision_manifest) VALUES ('r', repeat('a',64), repeat('b',64), "
                    "gen_random_uuid(), gen_random_uuid(), 'v', 'SAFE_BASELINE', "
                    "'SAFE_BASELINE', 'NONE', repeat('c',64), gen_random_uuid(), "
                    "gen_random_uuid(), '{}'::jsonb)"
                )
            )
    finally:
        engine.dispose()
