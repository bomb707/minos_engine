"""DB-V2 D3-A: B0 bootstrap, R2 registration and the non-mutating verifier."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.storage.dbv2_artifact_bootstrap import (
    NON_ARTIFACT_SHADOW_TABLES,
    B0Error,
    bootstrap_artifacts,
)
from minos_engine.storage.dbv2_d3a_verifier import verify_d3a
from minos_engine.storage.dbv2_recovery import (
    R1Error,
    R2Error,
    build_r1,
    register_r2,
)
from minos_engine.storage.dbv2_recovery_store import RecoveryRoot

from .conftest import (
    alembic_downgrade,
    alembic_upgrade,
    build_corpus,
    connect,
    seed_v1_artifacts,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

#: two corpora with different counts. Neither is the live 227, which is a derived result.
CORPUS_SMALL = {"counts": {"primary": 5}, "bare_path_roots": ()}
CORPUS_UNEVEN = {"counts": {"corpus": 9, "features": 4}, "bare_path_roots": ("features",)}


def _build_r1(v1_url: str, corpus: Any, root: RecoveryRoot, pg_dump: str) -> Any:
    engine, conn = connect(v1_url)
    try:
        environ = dict(os.environ)
        environ["MINOS_DBV2_PG_DUMP"] = pg_dump
        bundle = build_r1(
            conn,
            dsn=v1_url,
            root=root,
            roots=corpus.artifact_roots(),
            recovery_set_id=str(uuid.uuid4()),
            quiesce_started_at="2026-08-22T00:00:00+00:00",
            quiesce_ended_at="2026-08-22T00:05:00+00:00",
            created_at="2026-08-22T00:10:00+00:00",
            environ=environ,
        )
        conn.rollback()
        return bundle
    finally:
        conn.close()
        engine.dispose()


@pytest.fixture
def prepared(
    v1_url: str,
    shadow_url: str,
    recovery_root: RecoveryRoot,
    pg_dump_executable: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    """A seeded V1 corpus and a published R1 bundle, ready for B0."""
    spec = getattr(request, "param", CORPUS_SMALL)
    corpus = build_corpus(tmp_path, name="b0", **spec)
    seed_v1_artifacts(v1_url, corpus)
    bundle = _build_r1(v1_url, corpus, recovery_root, pg_dump_executable)
    return {
        "bundle": bundle,
        "corpus": corpus,
        "recovery_root": recovery_root,
        "shadow_url": shadow_url,
        "v1_url": v1_url,
    }


def _bootstrap(prepared: dict[str, Any]) -> Any:
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            return bootstrap_artifacts(
                conn, bundle=prepared["bundle"], roots=prepared["corpus"].artifact_roots()
            )
    finally:
        conn.close()
        engine.dispose()


def _register(prepared: dict[str, Any]) -> Any:
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            return register_r2(conn, bundle=prepared["bundle"], root=prepared["recovery_root"])
    finally:
        conn.close()
        engine.dispose()


def _counts(url: str) -> dict[str, int]:
    engine, conn = connect(url)
    try:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in (
                "dbv2_catalog.storage_backends",
                "dbv2_catalog.artifacts",
                "dbv2_catalog.artifact_locations",
                "dbv2_catalog.backup_sets",
                "dbv2_audit.events",
                "dbv2_audit.admin_operations",
            )
        }
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# K9-K13: B0
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "prepared", [CORPUS_SMALL, CORPUS_UNEVEN], indirect=True, ids=["small", "uneven"]
)
def test_b0_creates_exactly_the_artifact_graph(prepared: dict[str, Any]) -> None:
    """K9: one backend per declared root, one artifact and one location per V1 row."""
    corpus = prepared["corpus"]
    result = _bootstrap(prepared)
    assert result.backends == len(corpus.roots)
    assert result.artifacts_registered == corpus.artifact_count
    assert result.locations_registered == corpus.artifact_count
    assert result.artifacts_verified == corpus.artifact_count
    counts = _counts(prepared["shadow_url"])
    assert counts["dbv2_catalog.storage_backends"] == len(corpus.roots)
    assert counts["dbv2_catalog.artifacts"] == corpus.artifact_count
    assert counts["dbv2_catalog.artifact_locations"] == corpus.artifact_count
    assert counts["dbv2_catalog.backup_sets"] == 0


def test_b0_replay_is_idempotent(prepared: dict[str, Any]) -> None:
    """K10."""
    first = _bootstrap(prepared)
    before = _counts(prepared["shadow_url"])
    second = _bootstrap(prepared)
    after = _counts(prepared["shadow_url"])
    assert second.artifacts_registered == 0
    assert second.already_present == first.artifacts_registered
    assert after == before


def test_two_concurrent_b0_callers_produce_one_graph(prepared: dict[str, Any]) -> None:
    """K11: the advisory lock is deterministic in the recovery set's identity."""
    barrier = threading.Barrier(2)
    outcomes: dict[int, str] = {}

    def run(index: int) -> None:
        engine, conn = connect(prepared["shadow_url"])
        try:
            conn.execute(text("SELECT 1"))
            conn.rollback()
            barrier.wait()
            with conn.begin():
                bootstrap_artifacts(
                    conn, bundle=prepared["bundle"], roots=prepared["corpus"].artifact_roots()
                )
            outcomes[index] = "ok"
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcomes[index] = f"error: {str(error).splitlines()[0]}"
        finally:
            conn.close()
            engine.dispose()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes.values()) == ["ok", "ok"], outcomes
    counts = _counts(prepared["shadow_url"])
    assert counts["dbv2_catalog.artifacts"] == prepared["corpus"].artifact_count
    assert counts["dbv2_catalog.artifact_locations"] == prepared["corpus"].artifact_count


def test_a_b0_conflict_rolls_back_completely(prepared: dict[str, Any]) -> None:
    """K12: a payload that no longer matches R1 leaves no partial graph behind."""
    corpus = prepared["corpus"]
    target = corpus.path_of(corpus.rows[-1])
    target.write_bytes(target.read_bytes() + b"drift")
    with pytest.raises(B0Error, match="no longer matches R1"):
        _bootstrap(prepared)
    counts = _counts(prepared["shadow_url"])
    assert counts["dbv2_catalog.artifacts"] == 0
    assert counts["dbv2_catalog.artifact_locations"] == 0
    assert counts["dbv2_catalog.storage_backends"] == 0


def test_b0_populates_no_other_business_table(prepared: dict[str, Any]) -> None:
    """K13."""
    _bootstrap(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        populated = [
            table
            for table in NON_ARTIFACT_SHADOW_TABLES
            if int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
        ]
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    assert populated == []


def test_b0_refuses_a_partially_transformed_shadow_schema(prepared: dict[str, Any]) -> None:
    """H4: another phase already wrote a business row, so B0 will not add to it."""
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO dbv2_catalog.releases (release_key, release_hash, "
                    "component_manifest, created_by_role) VALUES ('r', :h, '{}'::jsonb, 'x')"
                ),
                {"h": "a" * 64},
            )
    finally:
        conn.close()
        engine.dispose()
    with pytest.raises(B0Error, match="already holds 1 rows"):
        _bootstrap(prepared)


# --------------------------------------------------------------------------- #
# K14-K22: R2
# --------------------------------------------------------------------------- #
def test_r2_before_b0_fails(prepared: dict[str, Any]) -> None:
    """K14: the shadow catalog is empty, so R2 has nothing to be exact against."""
    with pytest.raises(R2Error, match="requires a completed B0"):
        _register(prepared)
    assert _counts(prepared["shadow_url"])["dbv2_catalog.backup_sets"] == 0


def test_b0_then_r2_succeeds(prepared: dict[str, Any]) -> None:
    """K15."""
    _bootstrap(prepared)
    result = _register(prepared)
    assert result.backup_set_id
    counts = _counts(prepared["shadow_url"])
    assert counts["dbv2_catalog.backup_sets"] == 1
    assert counts["dbv2_audit.admin_operations"] == 1
    engine, conn = connect(prepared["shadow_url"])
    try:
        recovery = int(
            conn.execute(
                text("SELECT count(*) FROM dbv2_catalog.artifacts WHERE backup_scope = 'recovery'")
            ).scalar_one()
        )
        operational = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM dbv2_catalog.artifacts WHERE backup_scope = 'operational'"
                )
            ).scalar_one()
        )
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    assert recovery == 3
    assert operational == prepared["corpus"].artifact_count


def test_r2_replay_is_idempotent(prepared: dict[str, Any]) -> None:
    """K16."""
    _bootstrap(prepared)
    first = _register(prepared)
    before = _counts(prepared["shadow_url"])
    second = _register(prepared)
    after = _counts(prepared["shadow_url"])
    assert second.backup_set_id == first.backup_set_id
    assert second.already_registered is True
    assert after == before
    assert after["dbv2_audit.admin_operations"] == 1


@pytest.mark.parametrize("kind", ["recovery", "snapshot", "backup"])
def test_a_changed_r1_file_conflicts(prepared: dict[str, Any], kind: str) -> None:
    """K17, K18, K19: any tampered published file is caught before anything is registered."""
    _bootstrap(prepared)
    _register(prepared)
    bundle = prepared["bundle"]
    digest = {
        "recovery": bundle.recovery_manifest_sha256,
        "snapshot": bundle.snapshot_manifest_sha256,
        "backup": bundle.dump_sha256,
    }[kind]
    root: RecoveryRoot = prepared["recovery_root"]
    path = root.path / root.relative_path_for(kind, digest)
    os.chmod(path, 0o640)
    path.write_bytes(b"tampered evidence")
    with pytest.raises(Exception, match="hashes to"):
        root.read(kind, digest)


def test_an_incomplete_snapshot_fails(prepared: dict[str, Any]) -> None:
    """K20: B0 registered every artifact, so a snapshot missing one is not the exact set."""
    _bootstrap(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            extra = hashlib.sha256(b"an artifact the snapshot never listed").hexdigest()
            conn.execute(
                text(
                    "SELECT dbv2_catalog.get_or_verify_external_artifact(:d, 12, "
                    "'application/json', 'k', 'operational', 'standard', 'v1', '{}'::jsonb)"
                ),
                {"d": extra},
            )
    finally:
        conn.close()
        engine.dispose()
    with pytest.raises(R2Error, match="is not the R1 set"):
        _register(prepared)


def test_a_dump_without_a_verified_location_fails(prepared: dict[str, Any]) -> None:
    """K21: the dump is external, so the completeness gate demands a present location."""
    _bootstrap(prepared)
    _register(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        present = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM dbv2_catalog.artifacts AS a "
                    "JOIN dbv2_catalog.artifact_locations AS l ON l.artifact_id = a.id "
                    "WHERE a.content_sha256 = :d AND l.location_state = 'present' "
                    "  AND l.is_primary"
                ),
                {"d": prepared["bundle"].dump_sha256},
            ).scalar_one()
        )
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    assert present == 1


def test_a_database_only_recovery_set_cannot_authorize_b1(prepared: dict[str, Any]) -> None:
    """K22: the declared row shape says so, and the contract records it."""
    _bootstrap(prepared)
    _register(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        completeness = str(
            conn.execute(text("SELECT completeness FROM dbv2_catalog.backup_sets")).scalar_one()
        )
        shape = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_backup_sets_shape'"
            )
        ).scalar_one()
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    assert completeness == "complete"
    assert "database_only" in str(shape)

    import importlib.util
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("dbv2_audit", root / "scripts" / "dbv2_audit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contract = module.load_strict(root / "reports" / "database" / "MINOS_DATABASE_V2_CONTRACT.json")
    shapes = next(
        t
        for s in contract["schemas"]
        if s["schema"] == "catalog"
        for t in s["tables"]
        if t["table"] == "backup_sets"
    )["row_shapes"]
    assert shapes["database_only"]["may_authorize_migration"] is False
    assert shapes["complete"]["may_authorize_migration"] is True


# --------------------------------------------------------------------------- #
# K23-K28: the verifier and the invariants
# --------------------------------------------------------------------------- #
def _verify(prepared: dict[str, Any], *, expect_r2: bool = True) -> Any:
    v1_engine, v1_conn = connect(prepared["v1_url"])
    shadow_engine, shadow_conn = connect(prepared["shadow_url"])
    try:
        return verify_d3a(
            v1_conn,
            shadow_conn,
            root=prepared["recovery_root"],
            roots=prepared["corpus"].artifact_roots(),
            recovery_manifest_sha256=prepared["bundle"].recovery_manifest_sha256,
            expect_r2=expect_r2,
        )
    finally:
        v1_conn.rollback()
        shadow_conn.rollback()
        v1_conn.close()
        shadow_conn.close()
        v1_engine.dispose()
        shadow_engine.dispose()


@pytest.mark.parametrize(
    "prepared", [CORPUS_SMALL, CORPUS_UNEVEN], indirect=True, ids=["small", "uneven"]
)
def test_the_verifier_passes_on_the_accepted_graph(prepared: dict[str, Any]) -> None:
    """K23."""
    _bootstrap(prepared)
    _register(prepared)
    result = _verify(prepared)
    assert result.passed, [str(check) for check in result.failures]
    assert len(result.checks) >= 20


def test_the_verifier_detects_a_missing_shadow_row(prepared: dict[str, Any]) -> None:
    """K24: a row attack."""
    _bootstrap(prepared)
    _register(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE dbv2_catalog.artifacts SET lifecycle_state = 'archived' "
                    "WHERE content_sha256 = :d"
                ),
                {"d": prepared["corpus"].rows[0]["sha256"]},
            )
    finally:
        conn.close()
        engine.dispose()
    result = _verify(prepared)
    assert not result.passed
    assert any(check.name == "b0.exact_v1_artifact_set" for check in result.failures)


def test_the_verifier_detects_a_changed_payload(prepared: dict[str, Any]) -> None:
    """K24: a file/digest attack."""
    _bootstrap(prepared)
    _register(prepared)
    target = prepared["corpus"].path_of(prepared["corpus"].rows[0])
    target.write_bytes(target.read_bytes() + b"attack")
    result = _verify(prepared)
    assert not result.passed
    assert any(check.name == "b0.payloads_rehash" for check in result.failures)


def test_the_verifier_detects_a_permission_attack(prepared: dict[str, Any]) -> None:
    """K24."""
    _bootstrap(prepared)
    _register(prepared)
    root: RecoveryRoot = prepared["recovery_root"]
    path = root.path / root.relative_path_for(
        "recovery", prepared["bundle"].recovery_manifest_sha256
    )
    os.chmod(path, 0o644)
    result = _verify(prepared)
    assert not result.passed
    assert any(check.name == "r1.file_permissions" for check in result.failures)


def test_the_verifier_detects_a_missing_audit_row(prepared: dict[str, Any]) -> None:
    """K24: an audit attack. The audit tables refuse DELETE, so the attack is a new row."""
    _bootstrap(prepared)
    _register(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO dbv2_audit.admin_operations (operation_kind, outcome) "
                    "VALUES ('restore', 'succeeded')"
                )
            )
    finally:
        conn.close()
        engine.dispose()
    result = _verify(prepared)
    assert not result.passed
    assert any(check.name == "r2.administrative_audit_row" for check in result.failures)


def test_the_verifier_detects_b1_having_started(prepared: dict[str, Any]) -> None:
    """K24."""
    _bootstrap(prepared)
    _register(prepared)
    engine, conn = connect(prepared["shadow_url"])
    try:
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO dbv2_catalog.releases (release_key, release_hash, "
                    "component_manifest, created_by_role) VALUES ('r', :h, '{}'::jsonb, 'x')"
                ),
                {"h": "b" * 64},
            )
    finally:
        conn.close()
        engine.dispose()
    result = _verify(prepared)
    assert not result.passed
    assert any(check.name == "b1.absent" for check in result.failures)


def test_three_verifier_runs_change_nothing(prepared: dict[str, Any]) -> None:
    """K25."""
    _bootstrap(prepared)
    _register(prepared)
    before = _counts(prepared["shadow_url"])
    for _ in range(3):
        assert _verify(prepared).passed
    assert _counts(prepared["shadow_url"]) == before


def test_v1_data_remains_byte_identical(prepared: dict[str, Any]) -> None:
    """K26: B0 and R2 read V1 and never write it."""
    engine, conn = connect(prepared["v1_url"])
    try:
        before = conn.execute(
            text(
                "SELECT md5(string_agg(r::text, E'\\n' ORDER BY r::text)) "
                "FROM catalog.artifacts AS r"
            )
        ).scalar_one()
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    _bootstrap(prepared)
    _register(prepared)
    engine, conn = connect(prepared["v1_url"])
    try:
        after = conn.execute(
            text(
                "SELECT md5(string_agg(r::text, E'\\n' ORDER BY r::text)) "
                "FROM catalog.artifacts AS r"
            )
        ).scalar_one()
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    assert after == before


def test_0009_remains_reversible_after_b0_and_r2(prepared: dict[str, Any]) -> None:
    """K27: downgrade drops the shadow schemas; re-upgrade recreates them empty."""
    _bootstrap(prepared)
    _register(prepared)
    url = prepared["shadow_url"]
    alembic_downgrade(url, "0008_l2f_execution_results")
    engine, conn = connect(url)
    try:
        remaining = int(
            conn.execute(
                text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'dbv2\\_%'")
            ).scalar_one()
        )
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    assert remaining == 0
    alembic_upgrade(url, "0009_dbv2_shadow_schema")
    counts = _counts(url)
    assert counts["dbv2_catalog.artifacts"] == 0
    assert counts["dbv2_catalog.backup_sets"] == 0


def test_the_operational_store_is_never_reached(prepared: dict[str, Any]) -> None:
    """K28: every URL this suite uses belongs to the throwaway cluster, not 127.0.0.1:5433."""
    for url in (prepared["v1_url"], prepared["shadow_url"]):
        assert "5433" not in url
        assert "127.0.0.1" not in url
    _bootstrap(prepared)
    _register(prepared)
    assert _verify(prepared).passed


def test_r1_requires_the_source_revision(prepared: dict[str, Any], pg_dump_executable: str) -> None:
    """D: build-r1 is bound to 0005 and refuses anything else."""
    engine, conn = connect(prepared["shadow_url"])
    try:
        environ = dict(os.environ)
        environ["MINOS_DBV2_PG_DUMP"] = pg_dump_executable
        with pytest.raises(R1Error, match="R1 must be built from 0005"):
            build_r1(
                conn,
                dsn=prepared["shadow_url"],
                root=prepared["recovery_root"],
                roots=prepared["corpus"].artifact_roots(),
                recovery_set_id=str(uuid.uuid4()),
                quiesce_started_at="2026-08-22T00:00:00+00:00",
                quiesce_ended_at="2026-08-22T00:05:00+00:00",
                created_at="2026-08-22T00:10:00+00:00",
                environ=environ,
            )
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()


def test_b0_requires_the_shadow_revision(prepared: dict[str, Any]) -> None:
    """D: B0 is bound to 0009."""
    engine, conn = connect(prepared["v1_url"])
    try:
        with pytest.raises(B0Error, match="B0 requires 0009"):
            bootstrap_artifacts(
                conn, bundle=prepared["bundle"], roots=prepared["corpus"].artifact_roots()
            )
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
