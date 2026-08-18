"""Revision 0001 is an immutable, self-contained snapshot (Defect 1)."""

from __future__ import annotations

import ast

import sqlalchemy as sa
from sqlalchemy import create_engine, text

from minos_engine.storage.constants import SCHEMAS
from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.metadata import Base
from minos_engine.storage.migration_contract import FROZEN_INVENTORY, contract_hash
from tests.conftest import REPO_ROOT

from .conftest import alembic_downgrade, alembic_upgrade, scratch_database

_MIGRATION = REPO_ROOT / "migrations" / "versions" / "0001_l2b_initial.py"


def _counts(url: str) -> dict[str, int]:
    eng = create_engine(normalize_database_url(url))
    S = list(SCHEMAS)
    try:
        with eng.connect() as c:

            def n(q: str) -> int:
                return int(c.execute(text(q), {"s": S}).scalar() or 0)

            return {
                "schemas": n("SELECT count(*) FROM pg_namespace WHERE nspname = ANY(:s)"),
                "tables": n(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = ANY(:s)"
                ),
                "primary_keys": n(
                    "SELECT count(*) FROM pg_constraint WHERE contype='p' "
                    "AND connamespace::regnamespace::text = ANY(:s)"
                ),
                "foreign_keys": n(
                    "SELECT count(*) FROM pg_constraint WHERE contype='f' "
                    "AND connamespace::regnamespace::text = ANY(:s)"
                ),
                "unique_constraints": n(
                    "SELECT count(*) FROM pg_constraint WHERE contype='u' "
                    "AND connamespace::regnamespace::text = ANY(:s)"
                ),
                "check_constraints": n(
                    "SELECT count(*) FROM pg_constraint WHERE contype='c' "
                    "AND connamespace::regnamespace::text = ANY(:s)"
                ),
                "indexes": n("SELECT count(*) FROM pg_indexes WHERE schemaname = ANY(:s)"),
                "functions": n("SELECT count(*) FROM pg_proc WHERE proname LIKE 'minos_reject_%'"),
                "triggers": n("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"),
                "roles": n("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'minos_%'"),
            }
    finally:
        eng.dispose()


# --- #1/#2 source contains no ORM metadata dependency ---------------------------
def test_source_has_no_orm_bulk_or_metadata_tokens():
    src = _MIGRATION.read_text(encoding="utf-8")
    for token in ("Base.metadata", "create_all", "drop_all", "MetaData("):
        assert token not in src, token


def test_source_imports_no_storage_models_or_metadata():
    tree = ast.parse(_MIGRATION.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    for mod in imported:
        assert not mod.startswith("minos_engine.storage.models"), mod
        assert mod != "minos_engine.storage.metadata", mod
        assert "Base" not in mod


# --- #3/#4 runtime metadata changes do not affect revision 0001 -----------------
def test_runtime_metadata_change_does_not_affect_migration(pg_base_url: str):
    # Mutate the runtime ORM metadata (add a synthetic table). The frozen migration
    # must ignore it entirely: upgrade neither creates it (nor any 11th table), and
    # downgrade removes exactly the frozen inventory. The metadata mutation is undone
    # in ``finally`` so the shared Base.metadata is restored net-zero.
    t = sa.Table(
        "synthetic_l2c_preview",
        Base.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        schema="catalog",
    )
    try:
        with scratch_database(pg_base_url, "minos_imm_add") as url:
            alembic_upgrade(url, "head")
            eng = create_engine(normalize_database_url(url))
            try:
                with eng.connect() as c:
                    present = c.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_name = 'synthetic_l2c_preview'"
                        )
                    ).scalar()
                assert present == 0  # migration ignores runtime metadata
            finally:
                eng.dispose()
            assert _counts(url)["tables"] == 10  # exactly the frozen ten
            alembic_downgrade(url, "base")
            assert _counts(url)["tables"] == 0  # downgrade output also independent
    finally:
        Base.metadata.remove(t)


# --- #5/#6/#7 frozen inventory + lifecycle --------------------------------------
def test_clean_upgrade_produces_frozen_inventory(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_frozen") as url:
        alembic_upgrade(url, "head")
        counts = _counts(url)
        assert counts == FROZEN_INVENTORY["counts"]
        eng = create_engine(normalize_database_url(url))
        try:
            with eng.connect() as c:
                tables = set(
                    c.execute(
                        text(
                            "SELECT table_schema || '.' || table_name "
                            "FROM information_schema.tables WHERE table_schema = ANY(:s)"
                        ),
                        {"s": list(SCHEMAS)},
                    ).scalars()
                )
                funcs = set(
                    c.execute(
                        text("SELECT proname FROM pg_proc WHERE proname LIKE 'minos_reject_%'")
                    ).scalars()
                )
                triggers = set(
                    c.execute(
                        text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
                    ).scalars()
                )
        finally:
            eng.dispose()
        assert tables == set(FROZEN_INVENTORY["tables"])
        assert funcs == {"minos_reject_mutation", "minos_reject_identity_change"}
        assert triggers == set(FROZEN_INVENTORY["triggers"])


def test_downgrade_removes_exactly_the_frozen_inventory(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_frozen_down") as url:
        alembic_upgrade(url, "head")
        alembic_downgrade(url, "base")
        counts = _counts(url)
        assert counts["schemas"] == 0
        assert counts["tables"] == 0
        assert counts["functions"] == 0


def test_upgrade_downgrade_reupgrade(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_frozen_cycle") as url:
        alembic_upgrade(url, "head")
        alembic_downgrade(url, "base")
        alembic_upgrade(url, "head")
        assert _counts(url) == FROZEN_INVENTORY["counts"]


# --- contract hash --------------------------------------------------------------
def test_contract_hash_is_deterministic():
    import hashlib

    sha = hashlib.sha256(_MIGRATION.read_bytes()).hexdigest()
    assert contract_hash(sha) == contract_hash(sha)
    assert contract_hash(sha) != contract_hash("0" * 64)
