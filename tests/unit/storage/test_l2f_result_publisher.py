"""F5-B content-addressed result-artifact publisher — behavioral tests (real filesystem)."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from minos_engine.storage.l2f_execution_contract import (
    L2F_RESULT_MANIFEST_MEDIA_TYPE,
    L2F_VCF_MEDIA_TYPE,
)
from minos_engine.storage.l2f_result_publisher import (
    RESULT_ARTIFACT_FILE_MODE,
    RESULT_MANIFEST_EXTENSION,
    VCF_EXTENSION,
    ResultArtifactIntegrityError,
    ResultArtifactPublisher,
    ResultArtifactRootError,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "resroot"
    root.mkdir()
    os.chmod(root, 0o2750)
    return root


def _payload(kind: str) -> tuple[bytes, str]:
    data = b"##fileformat=VCFv4.2\n#CHROM\n" if kind == "vcf" else b'{"schema_version":"x"}'
    return data, hashlib.sha256(data).hexdigest()


def test_root_contract_rejects_missing_symlink_and_wrong_mode(tmp_path: Path) -> None:
    with pytest.raises(ResultArtifactRootError):
        ResultArtifactPublisher(tmp_path / "absent")
    target = tmp_path / "real"
    target.mkdir()
    os.chmod(target, 0o2750)
    link = tmp_path / "linked"
    link.symlink_to(target)
    with pytest.raises(ResultArtifactRootError):
        ResultArtifactPublisher(link)
    wrong = tmp_path / "wrongmode"
    wrong.mkdir()
    os.chmod(wrong, 0o0755)
    with pytest.raises(ResultArtifactRootError):
        ResultArtifactPublisher(wrong)


@pytest.mark.parametrize(
    ("kind", "extension", "media"),
    [
        ("vcf", VCF_EXTENSION, L2F_VCF_MEDIA_TYPE),
        ("result_manifest", RESULT_MANIFEST_EXTENSION, L2F_RESULT_MANIFEST_MEDIA_TYPE),
    ],
)
def test_publish_is_content_addressed_immutable_and_credentialed(
    tmp_path: Path, kind: str, extension: str, media: str
) -> None:
    root = _root(tmp_path)
    pub = ResultArtifactPublisher(root)
    data, sha = _payload(kind)

    art = pub.publish(data, kind=kind, sha256=sha)
    assert art.created is True
    assert art.path == root / f"{sha}{extension}"
    assert art.path.read_bytes() == data
    assert art.media_type == media and art.size_bytes == len(data)
    st = art.path.stat()
    assert stat.S_IMODE(st.st_mode) == RESULT_ARTIFACT_FILE_MODE == 0o640
    assert st.st_uid == os.getuid() and st.st_gid == root.stat().st_gid
    assert art.uri == f"file://{art.path.resolve()}"

    # get-or-verify: republishing identical bytes reuses the inode
    again = pub.publish(data, kind=kind, sha256=sha)
    assert again.created is False and again.path == art.path


def test_vcf_and_manifest_extensions_are_distinct(tmp_path: Path) -> None:
    """The two kinds can never collide on one content-addressed path, even for identical bytes."""
    assert VCF_EXTENSION != RESULT_MANIFEST_EXTENSION
    root = _root(tmp_path)
    pub = ResultArtifactPublisher(root)
    data = b"##fileformat=VCFv4.2\n#CHROM\n"
    sha = hashlib.sha256(data).hexdigest()
    a = pub.publish(data, kind="vcf", sha256=sha)
    b = pub.publish(data, kind="result_manifest", sha256=sha)
    assert a.path != b.path
    assert a.media_type != b.media_type and a.provenance != b.provenance


def test_payload_not_matching_the_claimed_hash_is_rejected(tmp_path: Path) -> None:
    pub = ResultArtifactPublisher(_root(tmp_path))
    _data, sha = _payload("vcf")
    with pytest.raises(ResultArtifactIntegrityError):
        pub.publish(b"tampered", kind="vcf", sha256=sha)


def test_existing_file_with_wrong_bytes_is_rejected_and_left_unchanged(tmp_path: Path) -> None:
    root = _root(tmp_path)
    pub = ResultArtifactPublisher(root)
    data, sha = _payload("vcf")
    bad = root / f"{sha}{VCF_EXTENSION}"
    bad.write_bytes(b"not the real payload")
    os.chmod(bad, 0o640)
    with pytest.raises(ResultArtifactIntegrityError):
        pub.publish(data, kind="vcf", sha256=sha)
    assert bad.read_bytes() == b"not the real payload"


def test_existing_file_with_wrong_mode_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    pub = ResultArtifactPublisher(root)
    data, sha = _payload("vcf")
    path = root / f"{sha}{VCF_EXTENSION}"
    path.write_bytes(data)
    os.chmod(path, 0o600)
    with pytest.raises(ResultArtifactIntegrityError):
        pub.publish(data, kind="vcf", sha256=sha)


def test_unpublish_removes_only_the_created_inode(tmp_path: Path) -> None:
    root = _root(tmp_path)
    pub = ResultArtifactPublisher(root)
    data, sha = _payload("vcf")
    created = pub.publish(data, kind="vcf", sha256=sha)
    reused = pub.publish(data, kind="vcf", sha256=sha)
    pub.unpublish_if_created(reused)  # created=False -> never removed
    assert created.path.exists()
    pub.unpublish_if_created(created)
    assert not created.path.exists()


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    pub = ResultArtifactPublisher(_root(tmp_path))
    data, sha = _payload("vcf")
    with pytest.raises(ResultArtifactIntegrityError):
        pub.publish(data, kind="truth_vcf", sha256=sha)
