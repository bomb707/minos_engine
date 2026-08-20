"""E3 access boundary: role-scoped views, broker/reader retrieval, real credentials."""

from __future__ import annotations

import grp
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
    PartitionArtifactReader,
    provision_partition_root,
    verify_operational_credentials,
    verify_partition_capability,
)
from tests.conftest import REPO_ROOT
from tests.integration.layer2_features.conftest import usable_secondary_groups

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


def test_operational_credentials_hold_on_same_group(artifact_root) -> None:
    # same-UID owner-only (same group) roots are explicitly NOT isolation → HOLD.
    status = verify_operational_credentials(
        train_root=artifact_root / "l2e" / "train",
        validation_root=artifact_root / "l2e" / "validation",
        trainer_identity=pwd.getpwuid(os.getuid()).pw_name,
        evaluator_identity=pwd.getpwuid(os.getuid()).pw_name,
    )
    assert status.status == "HOLD"
    assert status.checks["partition_groups_distinct"] is False
    assert any("same OS group" in r for r in status.reasons)


# --------------------------------------------------------------------------- #
# item 1 — every artifact inode carries the partition gid + mode 0640
# --------------------------------------------------------------------------- #
def test_built_artifact_inode_has_gid_and_mode_0640(l2e_engine, extra_snaps, tmp_path) -> None:
    # build into an ISOLATED root (other tests mutate the shared artifact_root files).
    snap = extra_snaps[13]
    root = tmp_path / "inode"
    result = build_feature_matrix_with_trust(
        l2e_engine, snap.manifest_bytes, snap.trust, "train", artifact_root=root
    )
    partition_root = root / "l2e" / "train"
    st = Path(result.artifact_path).stat()
    assert st.st_uid == os.getuid()
    assert st.st_gid == partition_root.stat().st_gid  # partition gid provisioned on root
    assert stat.S_IMODE(st.st_mode) == 0o640  # owner-rw, group-r, no other/world
    # the persistence boundary verifies this at publish, so the final name never keeps a
    # wrongly-permissioned inode.


def test_publish_applies_distinct_partition_group_gid(l2e_engine, extra_snaps, tmp_path) -> None:
    """Build a fresh matrix into a root provisioned with a distinct OS group, and prove
    the published inode carries that group's gid + 0640 (exercises fchown/fchmod/verify)."""
    groups = usable_secondary_groups()
    if not groups:
        pytest.skip("no non-primary OS group available to provision a partition root")
    group = groups[0]
    gid = grp.getgrnam(group).gr_gid
    snap = extra_snaps[9]  # a fresh identity, never built into artifact_root
    provisioned = tmp_path / "prov"
    provision_partition_root(provisioned / "l2e" / "train", group=group)
    result = build_feature_matrix_with_trust(
        l2e_engine, snap.manifest_bytes, snap.trust, "train", artifact_root=provisioned
    )
    st = Path(result.artifact_path).stat()
    assert st.st_gid == gid
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert st.st_uid == os.getuid()


def test_publish_verifies_inode_credential_after_publication(
    l2e_engine, extra_snaps, tmp_path, monkeypatch
) -> None:
    """If the published inode ends up with the wrong mode, publication fails closed."""
    from minos_engine.layer2.features.errors import MatrixArtifactIntegrityError

    snap = extra_snaps[10]
    real_fchmod = os.fchmod
    # corrupt the applied mode so the post-publication verification must reject it.
    monkeypatch.setattr(os, "fchmod", lambda fd, _mode: real_fchmod(fd, 0o644))
    with pytest.raises(MatrixArtifactIntegrityError, match="mode"):
        build_feature_matrix_with_trust(
            l2e_engine, snap.manifest_bytes, snap.trust, "train", artifact_root=tmp_path / "bad"
        )


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


def test_partition_root_ownership_and_mode(artifact_root) -> None:
    for partition in ("train", "validation"):
        root = artifact_root / "l2e" / partition
        assert root.stat().st_uid == os.getuid()
        assert (root.stat().st_mode & 0o077) == 0


def test_no_test_partition_anywhere(artifact_root, matrix_broker) -> None:
    from minos_engine.storage.matrix_access import PARTITION_ROLES, PARTITION_VIEWS

    assert set(PARTITION_ROLES.values()) == {"train", "validation"}
    assert set(PARTITION_VIEWS) == {"train", "validation"}
    assert not (artifact_root / "l2e" / "test").exists()
