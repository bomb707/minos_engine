"""E3 access boundary: role-scoped views, artifact denial, retrieval confinement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from minos_engine.common.errors import MatrixAccessError
from minos_engine.storage.matrix_access import PartitionArtifactStore

_APP_ROLES = ("minos_trainer", "minos_evaluator", "minos_live", "minos_runner")

_TRAIN_VIEW = "SELECT count(*) FROM profiling.training_matrix"
_VALIDATION_VIEW = "SELECT count(*) FROM evaluation.validation_matrix"
_BASE_TABLES = (
    "SELECT count(*) FROM profiling.feature_sets",
    "SELECT count(*) FROM profiling.feature_matrices",
    "SELECT count(*) FROM profiling.feature_matrix_members",
)
_RAW_ARTIFACTS = "SELECT count(*) FROM catalog.artifacts"


def _role_query(engine: Engine, role: str, sql: str) -> bool:
    with engine.connect() as conn:
        try:
            conn.execute(text(f"SET ROLE {role}"))
            conn.execute(text(sql)).scalar()
            return True
        except Exception:  # noqa: BLE001 - denial is the signal
            return False


def _store(artifact_root: Path) -> PartitionArtifactStore:
    return PartitionArtifactStore(
        train_root=artifact_root / "l2e" / "train",
        validation_root=artifact_root / "l2e" / "validation",
    )


def test_train_view_visible_only_to_trainer(l2e_engine, built) -> None:
    assert _role_query(l2e_engine, "minos_trainer", _TRAIN_VIEW) is True
    assert _role_query(l2e_engine, "minos_evaluator", _TRAIN_VIEW) is False
    assert _role_query(l2e_engine, "minos_live", _TRAIN_VIEW) is False
    assert _role_query(l2e_engine, "minos_runner", _TRAIN_VIEW) is False


def test_validation_view_visible_only_to_evaluator(l2e_engine, built) -> None:
    assert _role_query(l2e_engine, "minos_evaluator", _VALIDATION_VIEW) is True
    assert _role_query(l2e_engine, "minos_trainer", _VALIDATION_VIEW) is False
    assert _role_query(l2e_engine, "minos_live", _VALIDATION_VIEW) is False
    assert _role_query(l2e_engine, "minos_runner", _VALIDATION_VIEW) is False


def test_both_partition_roles_denied_base_tables_and_raw_artifacts(l2e_engine, built) -> None:
    for role in ("minos_trainer", "minos_evaluator"):
        for sql in _BASE_TABLES:
            assert _role_query(l2e_engine, role, sql) is False, (role, sql)
        assert _role_query(l2e_engine, role, _RAW_ARTIFACTS) is False, role


def test_live_and_runner_legacy_artifact_access_unchanged(l2e_engine, built) -> None:
    assert _role_query(l2e_engine, "minos_live", _RAW_ARTIFACTS) is True
    assert _role_query(l2e_engine, "minos_runner", _RAW_ARTIFACTS) is True


def test_view_rows_carry_only_own_partition(l2e_engine, built) -> None:
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_trainer"))
        train_rows = (
            conn.execute(text("SELECT DISTINCT partition FROM profiling.training_matrix"))
            .scalars()
            .all()
        )
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_evaluator"))
        validation_rows = (
            conn.execute(text("SELECT DISTINCT partition FROM evaluation.validation_matrix"))
            .scalars()
            .all()
        )
    assert train_rows == ["train"]
    assert validation_rows == ["validation"]


# --------------------------------------------------------------------------- #
# partition-aware retrieval boundary
# --------------------------------------------------------------------------- #
def test_retrieval_identity_is_derived_from_current_user(l2e_engine, built, artifact_root) -> None:
    store = _store(artifact_root)
    train = built[("a", "train")]
    validation = built[("a", "validation")]
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_trainer"))
        payload = store.fetch_matrix_payload(conn, train.matrix_hash)
        assert len(payload) > 0
        # the trainer CANNOT resolve the validation matrix — not even its existence.
        with pytest.raises(MatrixAccessError, match="not visible"):
            store.fetch_matrix_payload(conn, validation.matrix_hash)
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_evaluator"))
        payload = store.fetch_matrix_payload(conn, validation.matrix_hash)
        assert len(payload) > 0
        with pytest.raises(MatrixAccessError, match="not visible"):
            store.fetch_matrix_payload(conn, train.matrix_hash)
    # an identity outside the partition map has no retrieval path at all.
    with (
        l2e_engine.connect() as conn,
        pytest.raises(MatrixAccessError, match="no matrix partition"),
    ):
        store.fetch_matrix_payload(conn, train.matrix_hash)


def test_roots_must_be_distinct_and_non_overlapping(artifact_root) -> None:
    same = artifact_root / "l2e" / "train"
    with pytest.raises(MatrixAccessError, match="same"):
        PartitionArtifactStore(train_root=same, validation_root=same)
    with pytest.raises(MatrixAccessError, match="overlap"):
        PartitionArtifactStore(
            train_root=artifact_root / "l2e", validation_root=artifact_root / "l2e" / "validation"
        )


def test_path_confinement_rejects_traversal_and_symlink_escape(
    l2e_engine, built, artifact_root, tmp_path
) -> None:
    store = _store(artifact_root)
    train = built[("a", "train")]
    train_path = Path(train.artifact_path)
    # traversal / relative / outside paths are rejected by confinement.
    with pytest.raises(MatrixAccessError):
        store._confine("train", str(artifact_root / "l2e" / "train" / ".." / "validation" / "x"))
    with pytest.raises(MatrixAccessError, match="absolute"):
        store._confine("train", "relative/path.parquet")
    with pytest.raises(MatrixAccessError):
        store._confine("train", "/etc/passwd")
    # symlink escape: replace the train artifact with a symlink out of the root.
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(train_path.read_bytes())
    original = train_path.read_bytes()
    train_path.unlink()
    train_path.symlink_to(outside)
    try:
        with l2e_engine.connect() as conn:
            conn.execute(text("SET ROLE minos_trainer"))
            with pytest.raises(MatrixAccessError, match="escapes"):
                store.fetch_matrix_payload(conn, train.matrix_hash)
    finally:
        train_path.unlink()
        train_path.write_bytes(original)


def test_cross_partition_substitution_and_byte_tamper_rejected(
    l2e_engine, built, artifact_root
) -> None:
    store = _store(artifact_root)
    train = built[("a", "train")]
    validation = built[("a", "validation")]
    train_path = Path(train.artifact_path)
    original = train_path.read_bytes()
    # substitute the validation artifact's bytes at the train location: hash mismatch.
    train_path.write_bytes(Path(validation.artifact_path).read_bytes())
    try:
        with l2e_engine.connect() as conn:
            conn.execute(text("SET ROLE minos_trainer"))
            with pytest.raises(MatrixAccessError, match="hash"):
                store.fetch_matrix_payload(conn, train.matrix_hash)
        # a single flipped byte is equally rejected.
        train_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
        with l2e_engine.connect() as conn:
            conn.execute(text("SET ROLE minos_trainer"))
            with pytest.raises(MatrixAccessError, match="hash"):
                store.fetch_matrix_payload(conn, train.matrix_hash)
    finally:
        train_path.write_bytes(original)


def test_partition_root_isolation_is_operationally_verified(artifact_root) -> None:
    store = _store(artifact_root)
    checks = store.verify_partition_isolation()
    assert checks and all(checks.values()), checks
    train_root = artifact_root / "l2e" / "train"
    train_root.chmod(0o755)
    try:
        loosened = store.verify_partition_isolation()
        assert loosened["train_root_owner_only"] is False
    finally:
        train_root.chmod(0o700)
    assert all(store.verify_partition_isolation().values())


def test_simulated_trainer_runtime_contains_no_validation_material(
    l2e_engine, built, artifact_root, tmp_path
) -> None:
    """Assemble everything the trainer identity can actually reach — view rows plus
    boundary-fetched artifacts — into a simulated checkout, then prove no validation
    payload, no retrievable validation URI, and no validation credential is present
    (evidence hashes are allowed)."""
    store = _store(artifact_root)
    checkout = tmp_path / "trainer_checkout"
    checkout.mkdir()
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_trainer"))
        rows = (
            conn.execute(
                text(
                    "SELECT DISTINCT partition, matrix_hash, artifact_sha256, artifact_uri "
                    "FROM profiling.training_matrix"
                )
            )
            .mappings()
            .all()
        )
        fetched: dict[str, bytes] = {}
        for row in rows:
            fetched[str(row["matrix_hash"])] = store.fetch_matrix_payload(
                conn, str(row["matrix_hash"])
            )
    for matrix_hash, payload in fetched.items():
        (checkout / f"{matrix_hash}.parquet").write_bytes(payload)
    validation_root = (artifact_root / "l2e" / "validation").resolve()
    validation_payloads = {p.read_bytes() for p in validation_root.iterdir() if p.is_file()}
    # 1) every reachable row/URI is train-partition and train-rooted.
    assert rows and all(row["partition"] == "train" for row in rows)
    assert all(
        Path(str(row["artifact_uri"]))
        .resolve()
        .is_relative_to((artifact_root / "l2e" / "train").resolve())
        for row in rows
    )
    # 2) no validation payload bytes exist anywhere in the simulated checkout.
    for file in checkout.iterdir():
        assert file.read_bytes() not in validation_payloads
    # 3) no retrievable validation URI: the trainer cannot see validation rows at all,
    #    and the boundary refuses the validation matrix even if its hash leaks
    #    (hash-only evidence is explicitly allowed).
    leaked_validation_hash = built[("a", "validation")].matrix_hash
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_trainer"))
        with pytest.raises(MatrixAccessError):
            store.fetch_matrix_payload(conn, leaked_validation_hash)
        with pytest.raises(Exception, match="permission denied"):
            conn.execute(text("SELECT artifact_uri FROM evaluation.validation_matrix"))


def test_no_test_retrieval_path_exists(artifact_root) -> None:
    from minos_engine.storage.matrix_access import PARTITION_ROLES, PARTITION_VIEWS

    assert set(PARTITION_ROLES.values()) == {"train", "validation"}
    assert set(PARTITION_VIEWS) == {"train", "validation"}
    store = _store(artifact_root)
    assert set(store._roots) == {"train", "validation"}
    assert not (artifact_root / "l2e" / "test").exists()


def test_partition_root_ownership_matches_runtime_uid(artifact_root) -> None:
    for partition in ("train", "validation"):
        root = artifact_root / "l2e" / partition
        assert root.stat().st_uid == os.getuid()
        assert (root.stat().st_mode & 0o077) == 0
