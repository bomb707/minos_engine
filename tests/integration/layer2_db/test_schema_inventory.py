"""Schema / table / constraint / index / function / trigger inventory on real PG16."""

from __future__ import annotations

from sqlalchemy import Connection, text

from minos_engine.storage.constants import SCHEMAS
from minos_engine.storage.fingerprint import table_names
from minos_engine.storage.triggers import append_only_trigger_names, identity_trigger_names


def test_postgres_major_version_is_16(rollback_conn: Connection):
    v = rollback_conn.execute(text("SELECT current_setting('server_version_num')")).scalar()
    assert v is not None and 160000 <= int(v) < 170000


def test_exactly_seven_application_schemas(rollback_conn: Connection):
    rows = set(
        rollback_conn.execute(
            text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:s)"), {"s": list(SCHEMAS)}
        ).scalars()
    )
    assert rows == set(SCHEMAS)
    assert len(SCHEMAS) == 7


def test_required_tables_in_correct_schemas(rollback_conn: Connection):
    present = set(
        rollback_conn.execute(
            text(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema = ANY(:s)"
            ),
            {"s": list(SCHEMAS)},
        ).scalars()
    )
    for qualified in table_names():
        assert qualified in present, qualified


def test_pk_and_unique_constraints_present(rollback_conn: Connection):
    names = set(rollback_conn.execute(text("SELECT conname FROM pg_constraint")).scalars())
    for expected in (
        "pk_artifacts",
        "uq_artifacts_sha256",
        "uq_gatk_configs_config_hash",
        "uq_profiles_identity_tuple",
        "uq_decisions_round_id_decision_hash",
        "ck_artifacts_sha256_hex",
        "ck_jobs_status_valid",
    ):
        assert expected in names, expected


def test_worker_claim_index_present(rollback_conn: Connection):
    n = rollback_conn.execute(
        text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_jobs_status_created_at'")
    ).scalar()
    assert n == 1


def test_immutability_functions_and_triggers_exist(rollback_conn: Connection):
    funcs = set(
        rollback_conn.execute(
            text("SELECT proname FROM pg_proc WHERE proname LIKE 'minos_reject_%'")
        ).scalars()
    )
    assert {"minos_reject_mutation", "minos_reject_identity_change"} <= funcs
    triggers = set(
        rollback_conn.execute(
            text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        ).scalars()
    )
    for name in (*append_only_trigger_names(), *identity_trigger_names()):
        assert name in triggers, name
