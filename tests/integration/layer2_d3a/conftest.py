"""Scratch fixtures for DB-V2 D3-A: a V1 database at 0005, a shadow database at 0009.

Both live in a dedicated bundled cluster whose roles this suite provisions, and both are named
``minos_engine_db`` inside their own cluster — the production boundary requires that name, and an
isolated cluster is how a test satisfies it without going near the operational store.

Every corpus, recovery root and artifact root is a temporary directory. Nothing here reads or
writes ``127.0.0.1:5433``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url

from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.dbv2_recovery import ArtifactRoots
from minos_engine.storage.dbv2_recovery_store import RecoveryRoot

_ENV = "MINOS_DATABASE_URL"

ROLE_CONFIGURATION: dict[str, str] = {
    "minos_migrate": "LOGIN",
    "minos_owner": "NOLOGIN",
    "minos_planner": "LOGIN",
    "minos_enqueue": "LOGIN",
    "minos_runner": "LOGIN",
    "minos_verifier": "LOGIN",
    "minos_trainer": "LOGIN",
    "minos_evaluator": "LOGIN",
    "minos_live": "LOGIN",
}

#: the canonical operational name the accepted production boundary requires. Inside a throwaway
#: cluster this is a scratch database that no operational process can reach.
CANONICAL_NAME = "minos_engine_db"


@pytest.fixture(scope="session")
def d3a_cluster_url() -> Iterator[str]:
    try:
        import pgserver
    except Exception:  # pragma: no cover - pgserver is a dev dependency, present in CI
        pytest.skip("pgserver is required for the D3-A cluster")
    workspace = tempfile.mkdtemp(prefix="minos_d3a_")
    server = pgserver.get_server(workspace)
    try:
        base = server.get_uri()
        admin = create_engine(normalize_database_url(base), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            for role, login in ROLE_CONFIGURATION.items():
                conn.execute(
                    text(
                        "DO $$ BEGIN "
                        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
                        f"CREATE ROLE {role} {login}; ELSE ALTER ROLE {role} {login}; "
                        "END IF; END $$;"
                    )
                )
            conn.execute(text("GRANT minos_owner TO minos_migrate"))
        admin.dispose()
        yield base
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


@contextlib.contextmanager
def scratch_database(base_url: str, name: str) -> Iterator[str]:
    admin = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            conn.execute(text(f'GRANT CREATE ON DATABASE "{name}" TO minos_owner'))
    finally:
        admin.dispose()
    url = (
        make_url(normalize_database_url(base_url))
        .set(database=name)
        .render_as_string(hide_password=False)
    )
    try:
        yield url
    finally:
        cleanup = create_engine(normalize_database_url(base_url), isolation_level="AUTOCOMMIT")
        with cleanup.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        cleanup.dispose()


def alembic_upgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get(_ENV)
    os.environ[_ENV] = url
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = previous


def alembic_downgrade(url: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get(_ENV)
    os.environ[_ENV] = url
    try:
        command.downgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = previous


# --------------------------------------------------------------------------- #
# synthetic corpora
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SyntheticCorpus:
    """A V1 artifact corpus on disk, and the rows that describe it."""

    name: str
    roots: dict[str, Path]
    rows: tuple[dict[str, Any], ...]

    @property
    def artifact_count(self) -> int:
        return len(self.rows)

    @property
    def total_bytes(self) -> int:
        return sum(int(row["size_bytes"]) for row in self.rows)

    def env_value(self) -> str:
        return ",".join(f"{key}={path}" for key, path in sorted(self.roots.items()))

    def artifact_roots(self) -> ArtifactRoots:
        return ArtifactRoots.from_environment({"MINOS_DBV2_ARTIFACT_ROOTS": self.env_value()})

    def path_of(self, row: dict[str, Any]) -> Path:
        locator = str(row["uri"])
        raw = locator[len("file://") :] if locator.startswith("file://") else locator
        return Path(raw)


def build_corpus(
    workspace: Path,
    *,
    name: str,
    counts: dict[str, int],
    bare_path_roots: tuple[str, ...] = (),
) -> SyntheticCorpus:
    """An uneven corpus: several roots, several kinds, several sizes, both locator forms."""
    roots: dict[str, Path] = {}
    rows: list[dict[str, Any]] = []
    for index, (backend_key, count) in enumerate(sorted(counts.items())):
        root = workspace / f"corpus-{name}-{backend_key}"
        root.mkdir(parents=True)
        roots[backend_key] = root
        for n in range(count):
            kind = ("json", "parquet", "vcf")[n % 3]
            payload = f"{name}:{backend_key}:{n}:".encode() + bytes(range((n * 7) % 251 + 1))
            relative = Path(f"{n % 4}") / f"artifact-{n:04d}.{kind}"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            bare = backend_key in bare_path_roots
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "media_type": {
                        "json": "application/json",
                        "parquet": "application/vnd.apache.parquet",
                        "vcf": "application/vnd.ga4gh.vcf",
                    }[kind],
                    "provenance": f"synthetic:{kind}",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                    "uri": str(target) if bare else f"file://{target}",
                }
            )
        del index
    return SyntheticCorpus(name=name, roots=roots, rows=tuple(rows))


def seed_v1_artifacts(url: str, corpus: SyntheticCorpus) -> None:
    """Insert exactly the corpus into the V1 ``catalog.artifacts`` of a scratch database."""
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM catalog.artifacts"))
            for row in corpus.rows:
                conn.execute(
                    text(
                        "INSERT INTO catalog.artifacts (id, uri, sha256, media_type, size_bytes, "
                        "provenance) VALUES (:i, :u, :h, :m, :s, :p)"
                    ),
                    {
                        "i": row["id"],
                        "u": row["uri"],
                        "h": row["sha256"],
                        "m": row["media_type"],
                        "s": row["size_bytes"],
                        "p": row["provenance"],
                    },
                )
    finally:
        engine.dispose()


@pytest.fixture
def recovery_root(tmp_path: Path) -> RecoveryRoot:
    root = tmp_path / "recovery-root"
    root.mkdir(mode=0o2750)
    for child in ("backups", "snapshots", "recovery"):
        (root / child).mkdir(mode=0o2750)
    os.chmod(root, 0o2750)
    return RecoveryRoot(root)


@pytest.fixture(scope="session")
def pg_dump_executable() -> str:
    """The provisioned dump binary. pgserver ships one; a system pg_dump also works."""
    try:
        import pgserver

        candidate = Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_dump"
        if candidate.is_file():
            return str(candidate)
    except Exception:  # pragma: no cover - fall through to the system binary
        pass
    found = shutil.which("pg_dump")
    if not found:
        pytest.skip("no pg_dump available for the R1 dump tests")
    return found


@pytest.fixture
def v1_url(d3a_cluster_url: str, request: pytest.FixtureRequest) -> Iterator[str]:
    """A scratch V1 database at 0005, named exactly like the operational store."""
    name = f"{CANONICAL_NAME}"
    with scratch_database(d3a_cluster_url, name) as url:
        alembic_upgrade(url, "0005_l2e_feature_view")
        yield url


@pytest.fixture
def shadow_url(d3a_cluster_url: str) -> Iterator[str]:
    """A scratch database walked all the way to 0009."""
    with scratch_database(d3a_cluster_url, f"{CANONICAL_NAME}_shadow") as url:
        alembic_upgrade(url, "0009_dbv2_shadow_schema")
        yield url


def connect(url: str) -> tuple[Any, Connection]:
    engine = create_engine(normalize_database_url(url))
    return engine, engine.connect()
