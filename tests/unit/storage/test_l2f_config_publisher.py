"""F3-C1 content-addressed CONFIG-payload publisher behavioral tests (real filesystem)."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from minos_engine.storage.l2f_config_publisher import (
    CONFIG_ARTIFACT_FILE_MODE,
    CONFIG_ARTIFACT_MEDIA_TYPE,
    ConfigArtifactIntegrityError,
    ConfigArtifactRootError,
    ConfigPayloadPublisher,
)


def _provisioned_root(tmp_path: Path) -> Path:
    root = tmp_path / "cfgroot"
    root.mkdir()
    os.chmod(root, 0o2750)
    return root


def _payload() -> tuple[bytes, str]:
    payload = b'{"alpha":1,"beta":true}'
    return payload, hashlib.sha256(payload).hexdigest()


def test_root_contract_rejects_missing_symlink_and_wrong_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigArtifactRootError):
        ConfigPayloadPublisher(tmp_path / "does-not-exist")

    link_target = tmp_path / "real"
    link_target.mkdir()
    os.chmod(link_target, 0o2750)
    symlinked = tmp_path / "linked"
    symlinked.symlink_to(link_target)
    with pytest.raises(ConfigArtifactRootError):
        ConfigPayloadPublisher(symlinked)

    wrong_mode = tmp_path / "wrongmode"
    wrong_mode.mkdir()
    os.chmod(wrong_mode, 0o0755)
    with pytest.raises(ConfigArtifactRootError):
        ConfigPayloadPublisher(wrong_mode)


def test_publish_is_content_addressed_immutable_and_credentialed(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    pub = ConfigPayloadPublisher(root)
    payload, config_hash = _payload()

    art = pub.publish(payload, config_hash=config_hash)
    assert art.created is True
    assert art.path == root / f"{config_hash}.json"
    assert art.path.read_bytes() == payload
    assert art.media_type == CONFIG_ARTIFACT_MEDIA_TYPE
    assert art.size_bytes == len(payload)
    st = art.path.stat()
    assert stat.S_IMODE(st.st_mode) == CONFIG_ARTIFACT_FILE_MODE == 0o640
    assert st.st_uid == os.getuid()
    assert st.st_gid == root.stat().st_gid
    assert art.uri == f"file://{art.path.resolve()}"

    # get-or-verify: republishing identical bytes reuses the inode (created=False).
    again = pub.publish(payload, config_hash=config_hash)
    assert again.created is False
    assert again.path == art.path


def test_publish_rejects_payload_not_matching_claimed_hash(tmp_path: Path) -> None:
    pub = ConfigPayloadPublisher(_provisioned_root(tmp_path))
    _, config_hash = _payload()
    with pytest.raises(ConfigArtifactIntegrityError):
        pub.publish(b"tampered-bytes", config_hash=config_hash)


def test_get_or_verify_rejects_existing_file_with_wrong_bytes(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    pub = ConfigPayloadPublisher(root)
    payload, config_hash = _payload()
    # pre-place a wrong-content file at the content-addressed path.
    bad = root / f"{config_hash}.json"
    bad.write_bytes(b"not the real payload")
    os.chmod(bad, 0o640)
    with pytest.raises(ConfigArtifactIntegrityError):
        pub.publish(payload, config_hash=config_hash)
    # the pre-existing file is left unchanged.
    assert bad.read_bytes() == b"not the real payload"


def test_unpublish_if_created_removes_only_created_inode(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    pub = ConfigPayloadPublisher(root)
    payload, config_hash = _payload()

    created = pub.publish(payload, config_hash=config_hash)
    assert created.path.exists()
    # a reused (created=False) artifact is never unpublished.
    reused = pub.publish(payload, config_hash=config_hash)
    pub.unpublish_if_created(reused)
    assert created.path.exists()
    # the created inode is removed.
    pub.unpublish_if_created(created)
    assert not created.path.exists()
