"""E3 access boundary: role-scoped views, broker/reader retrieval, real credentials."""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from minos_engine.common.errors import MatrixAccessError
from minos_engine.storage.feature_matrix import build_feature_matrix_with_trust
from minos_engine.storage.matrix_access import (
    MatrixArtifactBroker,
    PartitionArtifactPublisher,
    PartitionArtifactReader,
    verify_operational_credentials,
    verify_partition_capability,
)
from tests.conftest import REPO_ROOT
from tests.integration.layer2_features.conftest import provision_test_roots

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


# --------------------------------------------------------------------------- #
# grant matrix (DB layer)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# broker / role-specific reader: no caller-side object holds both roots
# --------------------------------------------------------------------------- #
def test_broker_mints_single_partition_reader(l2e_engine, built, matrix_broker) -> None:
    train = built[("a", "train")]
    validation = built[("a", "validation")]
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_trainer"))
        reader = matrix_broker.reader_for(conn)
        assert isinstance(reader, PartitionArtifactReader)
        assert reader.partition == "train"
        # the reader holds ONLY the train root — not a dict of both.
        assert not hasattr(reader, "_roots")
        payload = reader.fetch_matrix_payload(conn, train.matrix_hash)
        assert len(payload) > 0
        # a train reader cannot resolve the validation matrix even by hash.
        with pytest.raises(MatrixAccessError, match="not visible"):
            reader.fetch_matrix_payload(conn, validation.matrix_hash)
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_evaluator"))
        reader = matrix_broker.reader_for(conn)
        assert reader.partition == "validation"
        assert len(reader.fetch_matrix_payload(conn, validation.matrix_hash)) > 0
        with pytest.raises(MatrixAccessError, match="not visible"):
            reader.fetch_matrix_payload(conn, train.matrix_hash)


def test_reader_rejects_mismatched_connection_identity(l2e_engine, built, matrix_broker) -> None:
    train = built[("a", "train")]
    train_reader = PartitionArtifactReader("train", matrix_broker.root_for("train"))
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_evaluator"))
        # an evaluator connection may not drive a train reader.
        with pytest.raises(MatrixAccessError, match="does not match"):
            train_reader.fetch_matrix_payload(conn, train.matrix_hash)


def test_unmapped_identity_has_no_retrieval_path(l2e_engine, built, matrix_broker) -> None:
    # default login role is not a partition role.
    with (
        l2e_engine.connect() as conn,
        pytest.raises(MatrixAccessError, match="no matrix partition"),
    ):
        matrix_broker.reader_for(conn)


def test_broker_rejects_same_or_overlapping_roots(artifact_root) -> None:
    same = artifact_root / "l2e" / "train"
    with pytest.raises(MatrixAccessError, match="same"):
        MatrixArtifactBroker(train_root=same, validation_root=same)
    with pytest.raises(MatrixAccessError, match="overlap"):
        MatrixArtifactBroker(
            train_root=artifact_root / "l2e", validation_root=artifact_root / "l2e" / "validation"
        )


def test_reader_path_confinement_and_tamper(l2e_engine, built, matrix_broker, tmp_path) -> None:
    train = built[("a", "train")]
    reader = PartitionArtifactReader("train", matrix_broker.root_for("train"))
    root = matrix_broker.root_for("train")
    # traversal / relative / absolute-outside are rejected by confinement.
    with pytest.raises(MatrixAccessError):
        reader._confine(str(root / ".." / "validation" / "x.parquet"))
    with pytest.raises(MatrixAccessError, match="absolute"):
        reader._confine("relative/path.parquet")
    with pytest.raises(MatrixAccessError):
        reader._confine("/etc/passwd")

    train_path = Path(train.artifact_path)
    original = train_path.read_bytes()

    def _restore() -> None:
        # recreate the shared artifact with its canonical bytes AND mode 0o640, so this
        # test never leaves a wrongly-permissioned inode for a later test.
        if train_path.is_symlink() or train_path.exists():
            train_path.unlink()
        train_path.write_bytes(original)
        os.chmod(train_path, 0o640)

    # symlink escape.
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(original)
    train_path.unlink()
    train_path.symlink_to(outside)
    try:
        with l2e_engine.connect() as conn:
            conn.execute(text("SET ROLE minos_trainer"))
            with pytest.raises(MatrixAccessError, match="escapes"):
                reader.fetch_matrix_payload(conn, train.matrix_hash)
    finally:
        _restore()
    # byte tamper is rejected by the sha check.
    train_path.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
    try:
        with l2e_engine.connect() as conn:
            conn.execute(text("SET ROLE minos_trainer"))
            with pytest.raises(MatrixAccessError, match="hash"):
                reader.fetch_matrix_payload(conn, train.matrix_hash)
    finally:
        _restore()


# --------------------------------------------------------------------------- #
# capability (runs anywhere) vs operational credential (HOLD until real OS creds)
# --------------------------------------------------------------------------- #
def test_partition_capability_runs_anywhere(artifact_root) -> None:
    checks = verify_partition_capability(
        train_root=artifact_root / "l2e" / "train",
        validation_root=artifact_root / "l2e" / "validation",
    )
    assert checks and all(checks.values()), checks


def test_operational_credentials_hold_without_real_identities(artifact_root) -> None:
    # Explicit identities are required, but real impersonation is unavailable in CI
    # (not privileged / identities absent) → HOLD, never PASS. Two groups of the same
    # user would NOT be separation either; this verifier only PASSes on proven denial.
    status = verify_operational_credentials(
        train_root=artifact_root / "l2e" / "train",
        validation_root=artifact_root / "l2e" / "validation",
        trainer_identity="minos_trainer_os",
        evaluator_identity="minos_evaluator_os",
    )
    assert status.status == "HOLD"
    assert not status.ok
    assert status.checks["cross_identity_access_proven"] is False
    assert any("impersonation unavailable" in r for r in status.reasons)


def test_operational_credentials_hold_on_same_group(tmp_path) -> None:
    # two roots owned by the SAME uid + SAME group are explicitly NOT isolation → HOLD.
    train = tmp_path / "l2e" / "train"
    validation = tmp_path / "l2e" / "validation"
    for root in (train, validation):
        root.mkdir(parents=True)
        os.chown(root, os.getuid(), os.getgid())
        root.chmod(0o2750)
    me = pwd.getpwuid(os.getuid()).pw_name
    status = verify_operational_credentials(
        train_root=train,
        validation_root=validation,
        trainer_identity=me,
        evaluator_identity=me,
    )
    assert status.status == "HOLD"
    assert status.checks["partition_groups_distinct"] is False
    assert any("same OS group" in r for r in status.reasons)


# --------------------------------------------------------------------------- #
# item 1 — every artifact inode carries the partition gid + mode 0640
# --------------------------------------------------------------------------- #
def test_built_artifact_inode_has_gid_and_mode_0640(l2e_engine, extra_snaps, tmp_path) -> None:
    # build into an ISOLATED, provisioned root (distinct partition gids per partition).
    snap = extra_snaps[13]
    root = provision_test_roots(tmp_path / "inode")
    result = build_feature_matrix_with_trust(
        l2e_engine, snap.manifest_bytes, snap.trust, "train", artifact_root=root
    )
    train_root = root / "l2e" / "train"
    validation_root = root / "l2e" / "validation"
    assert train_root.stat().st_gid != validation_root.stat().st_gid  # distinct gids
    st = Path(result.artifact_path).stat()
    assert not Path(result.artifact_path).is_symlink()
    assert st.st_uid == os.getuid()
    assert st.st_gid == train_root.stat().st_gid  # the train partition gid, applied per-inode
    assert stat.S_IMODE(st.st_mode) == 0o640  # owner-rw, group-r, no other/world
    # the persistence boundary verifies this at publish, so the final name never keeps a
    # wrongly-permissioned inode.


def test_publish_verifies_inode_credential_after_publication(
    l2e_engine, extra_snaps, tmp_path, monkeypatch
) -> None:
    """If the published inode ends up with the wrong mode, publication fails closed and
    the just-created inode is unlinked (no bad final artifact remains)."""
    from minos_engine.layer2.features.errors import MatrixArtifactIntegrityError

    snap = extra_snaps[10]
    root = provision_test_roots(tmp_path / "bad")
    real_fchmod = os.fchmod
    # corrupt the applied mode so the post-publication verification must reject it.
    monkeypatch.setattr(os, "fchmod", lambda fd, _mode: real_fchmod(fd, 0o644))
    with pytest.raises(MatrixArtifactIntegrityError, match="mode"):
        build_feature_matrix_with_trust(
            l2e_engine, snap.manifest_bytes, snap.trust, "train", artifact_root=root
        )
    monkeypatch.undo()
    # no final or temporary artifact remains after the failed publish.
    assert list((root / "l2e" / "train").glob("*.parquet")) == []
    assert list((root / "l2e" / "train").glob(".tmp-*")) == []


# --------------------------------------------------------------------------- #
# real trainer runtime/package assembly (git archive) — no validation material
# --------------------------------------------------------------------------- #
def test_trainer_runtime_bundle_contains_no_validation_material(
    l2e_engine, built, matrix_broker, tmp_path
) -> None:
    validation = built[("a", "validation")]
    validation_root = str(matrix_broker.root_for("validation"))
    validation_group = "minos_validation_grp"  # a deployment-side credential name

    bundle = tmp_path / "trainer_runtime"
    bundle.mkdir()
    # (1) actual source assembly via `git archive` of the storage package (source only,
    #     no artifacts) — a real runtime/checkout, not a call to the Python method.
    archive = tmp_path / "src.tar"
    with archive.open("wb") as fh:
        subprocess.run(
            ["git", "archive", "HEAD", "src/minos_engine/storage"],
            cwd=REPO_ROOT,
            check=True,
            stdout=fh,
        )
    subprocess.run(["tar", "-xf", str(archive), "-C", str(bundle)], check=True)
    # (2) a trainer deployment config carrying ONLY the train partition credential.
    train_root = str(matrix_broker.root_for("train"))
    (bundle / "trainer_deploy.yaml").write_text(
        "role: minos_trainer\n"
        "partition: train\n"
        f"artifact_root: {train_root}\n"
        "artifact_group: minos_train_grp\n"
        "db_role: minos_trainer\n",
        encoding="utf-8",
    )
    # (3) evidence hashes are allowed to be present.
    (bundle / "evidence_hashes.txt").write_text(
        f"validation_matrix_hash={validation.matrix_hash}\n", encoding="utf-8"
    )

    all_bytes = b"".join(p.read_bytes() for p in bundle.rglob("*") if p.is_file())
    # no validation payload bytes.
    assert Path(validation.artifact_path).read_bytes() not in all_bytes
    assert not any(p.suffix == ".parquet" for p in bundle.rglob("*"))
    # no retrievable validation URI/root, no validation group/credential.
    text_blob = all_bytes.decode("utf-8", errors="ignore")
    assert validation_root not in text_blob
    assert validation.artifact_path not in text_blob
    assert validation_group not in text_blob
    assert "minos_evaluator" not in (bundle / "trainer_deploy.yaml").read_text()
    # the evidence hash (hash-only) IS allowed.
    assert validation.matrix_hash in text_blob


def test_partition_root_ownership_and_exact_mode_02750(artifact_root) -> None:
    train = artifact_root / "l2e" / "train"
    validation = artifact_root / "l2e" / "validation"
    for root in (train, validation):
        assert root.stat().st_uid == os.getuid()
        assert stat.S_IMODE(root.stat().st_mode) == 0o2750  # setgid + owner-rwx + group-r-x
    assert train.stat().st_gid != validation.stat().st_gid  # distinct partition gids


# --------------------------------------------------------------------------- #
# item 2 — mandatory provisioning at the production write boundary
# --------------------------------------------------------------------------- #
def test_publisher_requires_existing_provisioned_roots(tmp_path) -> None:
    train = tmp_path / "l2e" / "train"
    validation = tmp_path / "l2e" / "validation"
    # missing roots are NOT created automatically.
    with pytest.raises(MatrixAccessError, match="does not exist"):
        PartitionArtifactPublisher(train_root=train, validation_root=validation)
    # wrong mode is rejected.
    train.mkdir(parents=True)
    validation.mkdir(parents=True)
    train.chmod(0o2750)
    validation.chmod(0o700)
    with pytest.raises(MatrixAccessError, match="mode"):
        PartitionArtifactPublisher(train_root=train, validation_root=validation)
    # symlinked root is rejected.
    linkbase = tmp_path / "linked"
    (linkbase / "real").mkdir(parents=True)
    (linkbase / "real").chmod(0o2750)
    (linkbase / "train").symlink_to(linkbase / "real")
    (linkbase / "validation").mkdir()
    (linkbase / "validation").chmod(0o2750)
    with pytest.raises(MatrixAccessError, match="symlink|does not exist"):
        PartitionArtifactPublisher(
            train_root=linkbase / "train", validation_root=linkbase / "validation"
        )


def test_publisher_requires_distinct_partition_gids(tmp_path) -> None:
    # both roots provisioned with the SAME gid (current gid) — rejected as non-isolation.
    for part in ("train", "validation"):
        root = tmp_path / "l2e" / part
        root.mkdir(parents=True)
        os.chown(root, os.getuid(), os.getgid())
        root.chmod(0o2750)
    with pytest.raises(MatrixAccessError, match="DISTINCT partition gids"):
        PartitionArtifactPublisher(
            train_root=tmp_path / "l2e" / "train",
            validation_root=tmp_path / "l2e" / "validation",
        )


def test_production_builder_requires_publisher_not_bare_root(matrix_publisher) -> None:
    import inspect

    from minos_engine.storage.feature_matrix import build_accepted_epoch1_feature_matrix

    sig = inspect.signature(build_accepted_epoch1_feature_matrix)
    assert "publisher" in sig.parameters
    assert "artifact_root" not in sig.parameters
    assert isinstance(matrix_publisher, PartitionArtifactPublisher)


# --------------------------------------------------------------------------- #
# item 1 — privileged cross-identity PASS path (deployment/qualification only)
# --------------------------------------------------------------------------- #
def test_privileged_cross_identity_pass(built, artifact_root) -> None:
    """The real PASS path requires root + two provisioned service identities. In an
    ordinary (unprivileged) environment this SKIPS; a privileged deployment/qualification
    run exercises the cross-identity denial proof and expects PASS."""
    if os.geteuid() != 0:
        pytest.skip("cross-identity PASS requires privilege to impersonate service users")
    trainer = os.environ.get("MINOS_TRAINER_OS_IDENTITY")
    evaluator = os.environ.get("MINOS_EVALUATOR_OS_IDENTITY")
    if not trainer or not evaluator:  # pragma: no cover - privileged deployment only
        pytest.skip("MINOS_TRAINER_OS_IDENTITY / MINOS_EVALUATOR_OS_IDENTITY not set")
    status = verify_operational_credentials(  # pragma: no cover - privileged deployment only
        train_root=artifact_root / "l2e" / "train",
        validation_root=artifact_root / "l2e" / "validation",
        trainer_identity=trainer,
        evaluator_identity=evaluator,
    )
    assert status.status == "PASS", status.reasons


def test_no_test_partition_anywhere(artifact_root, matrix_broker) -> None:
    from minos_engine.storage.matrix_access import PARTITION_ROLES, PARTITION_VIEWS

    assert set(PARTITION_ROLES.values()) == {"train", "validation"}
    assert set(PARTITION_VIEWS) == {"train", "validation"}
    assert not (artifact_root / "l2e" / "test").exists()
