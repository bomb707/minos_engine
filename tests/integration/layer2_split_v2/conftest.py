"""Real PostgreSQL 16 fixtures for L2-C SPLIT-FROZEN **v2** (epoched) integration tests.

Provisions a session database upgraded to the v2 head (``0003``) with the synthetic
75-sample registry persisted (via the frozen v1 ``persist_manifest``) and the v2 epoch-1
snapshot persisted on top of those exact identities. Reuses the L2-B helpers; SQLite is
never used; tests skip locally only when neither ``MINOS_DATABASE_URL`` nor bundled
``pgserver`` is available.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

from minos_engine.storage.database import create_db_engine, normalize_database_url
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_split.conftest import pg_base_url  # noqa: F401  (session fixture)


def synthetic_v1_manifest() -> dict:
    """The synthetic v1-format manifest (fabricated but well-formed identities)."""
    from tests.layer2c_synth import synthetic_manifest

    return synthetic_manifest().to_canonical()


def synthetic_epoch1_manifest() -> dict:
    """Build the v2 epoch-1 manifest by INHERITING the synthetic v1 partitions verbatim.

    The DB registry is populated from ``synthetic_manifest()``; epoch 1 inherits those
    partitions exactly (zero transitions), binding the same identities ``persist_epoch``
    resolves in the registry.
    """
    from minos_engine.layer2.split_v2.generator import epoch1_from_v1_manifest

    return epoch1_from_v1_manifest(synthetic_v1_manifest())


@pytest.fixture(scope="session")
def l2c_v2_url(pg_base_url: str) -> Iterator[str]:  # noqa: F811
    with scratch_database(pg_base_url, "minos_l2c_v2_main") as url:
        alembic_upgrade(url, "0008_l2f_execution_results")
        from minos_engine.storage.dataset_split import persist_manifest
        from minos_engine.storage.dataset_split_v2 import persist_epoch
        from tests.layer2c_synth import synthetic_manifest

        eng = create_engine(normalize_database_url(url))
        try:
            with eng.begin() as conn:
                persist_manifest(conn, synthetic_manifest())
                persist_epoch(
                    conn, synthetic_epoch1_manifest(), v1_manifest=synthetic_v1_manifest()
                )
        finally:
            eng.dispose()
        yield url


@pytest.fixture(scope="session")
def l2c_v2_engine(l2c_v2_url: str) -> Iterator[Engine]:
    eng = create_db_engine(l2c_v2_url)
    try:
        yield eng
    finally:
        eng.dispose()
