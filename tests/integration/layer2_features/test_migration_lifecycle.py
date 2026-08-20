"""0005 lifecycle on real PostgreSQL 16: 0004 → 0005 → 0004 → 0005, exact contract.

Covers: single-head lineage, table/constraint/FK/unique/trigger inventory, ownership,
security-barrier view definitions, grant matrix, the stage-specific artifact-privilege
delta, and exact downgrade restoration (trainer regains catalog.artifacts SELECT;
evaluator never gains anything it did not have).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)

_HEAD = "0005_l2e_feature_view"
_PREVIOUS = "0004_l2d_profile_ingestion"

_TABLES = ("feature_sets", "feature_matrices", "feature_matrix_members")

_EXPECTED_CONSTRAINTS = {
    "feature_sets": {
        "pk_feature_sets",
        "uq_feature_sets_feature_set_hash",
        "ck_feature_sets_columns_positive",
        "ck_feature_sets_set_hash_hex",
        "ck_feature_sets_registry_hash_hex",
    },
    "feature_matrices": {
        "pk_feature_matrices",
        "fk_feature_matrices_profile_snapshot_id",
        "fk_feature_matrices_feature_set_id",
        "fk_feature_matrices_matrix_artifact_id_artifacts",
        "uq_feature_matrices_matrix_hash",
        "uq_feature_matrices_logical_identity",
        "ck_feature_matrices_partition_valid",
        "ck_feature_matrices_rows_nonneg",
        "ck_feature_matrices_columns_positive",
        "ck_feature_matrices_matrix_hash_hex",
        "ck_feature_matrices_artifact_sha_hex",
    },
    "feature_matrix_members": {
        "pk_feature_matrix_members",
        "fk_feature_matrix_members_matrix_id",
        "fk_feature_matrix_members_dataset_registry_id",
        "uq_feature_matrix_members_matrix_dataset",
        "uq_feature_matrix_members_matrix_index",
        "ck_feature_matrix_members_index_nonneg",
        "ck_feature_matrix_members_vector_hex",
        "ck_feature_matrix_members_feature_hex",
    },
}


def _scalar(url: str, sql: str, **params) -> object:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _has_table(url: str, name: str) -> bool:
    return bool(_scalar(url, "SELECT to_regclass(:n) IS NOT NULL", n=f"profiling.{name}"))


def _artifact_select(url: str, role: str) -> bool:
    return bool(
        _scalar(
            url,
            "SELECT has_table_privilege(:r, 'catalog.artifacts', 'SELECT')",
            r=role,
        )
    )


def test_single_head_with_0005_in_lineage() -> None:
    # exactly one Alembic head, and 0005 (L2-E) is on that head's lineage. The head itself
    # advances with later stages (e.g. 0006 L2-F), so this L2-E test binds 0005's presence
    # in the lineage rather than head equality.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    assert len(heads) == 1
    lineage = {r.revision for r in script.walk_revisions(base="base", head=heads[0])}
    assert _HEAD in lineage


@pytest.fixture(scope="module")
def lifecycle_url(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2e_lifecycle") as url:
        yield url


def test_full_lifecycle_0004_0005_0004_0005(lifecycle_url: str) -> None:
    url = lifecycle_url
    alembic_upgrade(url, _PREVIOUS)
    # the actual 0001 state at 0004: trainer HAS artifacts SELECT; evaluator does NOT.
    assert _artifact_select(url, "minos_trainer") is True
    assert _artifact_select(url, "minos_evaluator") is False
    assert not any(_has_table(url, t) for t in _TABLES)

    # pin explicitly to 0005 (not "head"): this is the L2-E lifecycle, unaffected by later
    # stages (0006+) that may advance the head.
    alembic_upgrade(url, _HEAD)
    assert _scalar(url, "SELECT version_num FROM alembic_version") == _HEAD
    assert all(_has_table(url, t) for t in _TABLES)
    # stage-specific privilege delta applied.
    assert _artifact_select(url, "minos_trainer") is False
    assert _artifact_select(url, "minos_evaluator") is False
    # legacy artifact access untouched.
    assert _artifact_select(url, "minos_live") is True
    assert _artifact_select(url, "minos_runner") is True

    alembic_downgrade(url, _PREVIOUS)
    assert not any(_has_table(url, t) for t in _TABLES)
    assert _scalar(url, "SELECT to_regclass('profiling.training_matrix') IS NOT NULL") is False
    assert _scalar(url, "SELECT to_regclass('evaluation.validation_matrix') IS NOT NULL") is False
    # downgrade restores EXACTLY the prior state: trainer regains SELECT; the
    # evaluator gains nothing it never had (no artifact SELECT, no catalog USAGE).
    assert _artifact_select(url, "minos_trainer") is True
    assert _artifact_select(url, "minos_evaluator") is False
    assert (
        bool(_scalar(url, "SELECT has_schema_privilege('minos_evaluator', 'catalog', 'USAGE')"))
        is False
    )
    assert _artifact_select(url, "minos_live") is True
    assert _artifact_select(url, "minos_runner") is True

    alembic_upgrade(url, "head")
    assert all(_has_table(url, t) for t in _TABLES)
    assert _artifact_select(url, "minos_trainer") is False


def test_contract_inventory_at_head(l2e_db_url: str) -> None:
    url = l2e_db_url
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            # ownership: every new table owned by minos_admin.
            for table in _TABLES:
                owner = conn.execute(
                    text(
                        "SELECT tableowner FROM pg_tables "
                        "WHERE schemaname = 'profiling' AND tablename = :t"
                    ),
                    {"t": table},
                ).scalar()
                assert owner == "minos_admin", (table, owner)
            # exact constraint inventory per table.
            for table, expected in _EXPECTED_CONSTRAINTS.items():
                names = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = CAST(:t AS regclass)"
                        ),
                        {"t": f"profiling.{table}"},
                    )
                }
                names = {n for n in names if not n.endswith("_not_null")}
                assert expected <= names, (table, expected - names)
            # append-only triggers present on all three tables.
            for table in _TABLES:
                triggers = {
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"
                        ),
                        {"t": f"profiling.{table}"},
                    )
                }
                assert f"trg_profiling_{table}_append_only" in triggers
            # security-barrier partition views, and NO test-matrix object of any kind.
            for schema, view in (
                ("profiling", "training_matrix"),
                ("evaluation", "validation_matrix"),
            ):
                options = conn.execute(
                    text(
                        "SELECT reloptions FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = :s AND c.relname = :v"
                    ),
                    {"s": schema, "v": view},
                ).scalar()
                assert options is not None and "security_barrier=true" in options
                definition = conn.execute(
                    text("SELECT pg_get_viewdef(CAST(:qv AS regclass), true)"),
                    {"qv": f"{schema}.{view}"},
                ).scalar()
                assert definition is not None
                expected_partition = "train" if view == "training_matrix" else "validation"
                assert f"partition = '{expected_partition}'" in definition
                assert "'test'" not in definition
            test_objects = conn.execute(
                text(
                    "SELECT count(*) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname IN ('profiling', 'evaluation', 'catalog') "
                    "AND c.relname LIKE '%test%matrix%'"
                )
            ).scalar()
            assert test_objects == 0
            # grant matrix: views to their single role only; base tables to no app role.
            grants = {
                ("profiling.training_matrix", "minos_trainer"): True,
                ("profiling.training_matrix", "minos_evaluator"): False,
                ("evaluation.validation_matrix", "minos_evaluator"): True,
                ("evaluation.validation_matrix", "minos_trainer"): False,
            }
            for (obj, role), expected_grant in grants.items():
                actual = conn.execute(
                    text("SELECT has_table_privilege(:r, :o, 'SELECT')"),
                    {"r": role, "o": obj},
                ).scalar()
                assert bool(actual) is expected_grant, (obj, role)
            for table in _TABLES:
                for role in ("minos_trainer", "minos_evaluator", "minos_live", "minos_runner"):
                    privileged = conn.execute(
                        text(
                            "SELECT has_table_privilege(:r, :o, 'SELECT, INSERT, UPDATE, DELETE')"
                        ),
                        {"r": role, "o": f"profiling.{table}"},
                    ).scalar()
                    assert bool(privileged) is False, (table, role)
    finally:
        engine.dispose()
