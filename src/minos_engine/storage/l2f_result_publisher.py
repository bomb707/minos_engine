"""L2-F F5 content-addressed result-artifact publisher (VCF + execution-result manifest).

Publishes the two immutable F5 artifacts into the PROVISIONED root named by
``MINOS_L2F_RESULT_ARTIFACT_ROOT`` using the SAME audited atomic protocol as the F3-C1 CONFIG
publisher: temp inode -> write -> fsync -> fchmod/fchown -> record temp inode identity -> hard-link
no-clobber -> verify final bytes/sha/size/credentials -> directory fsync. An existing identical
artifact is verified and reused; a final path is NEVER overwritten; only an inode PROVEN to have
been created by the failing call is removed on rollback.

Frozen root/file permission contract
------------------------------------
* root: an existing, non-symlink directory owned by the writer uid, mode EXACTLY ``0o2750``
  (never created or repaired here); its gid is the publish gid.
* file: content-addressed ``<root>/<sha256><extension>``, a regular non-symlink file owned by the
  writer uid, carrying the root gid, mode EXACTLY ``0o640``, whose bytes hash to ``<sha256>``.

The two artifact kinds use DISTINCT extensions (``.vcf`` and ``.result.json``) so a VCF and a
result manifest can never collide on one content-addressed path.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f_execution_contract import (
    L2F_RESULT_MANIFEST_MEDIA_TYPE,
    L2F_VCF_MEDIA_TYPE,
)

__all__ = [
    "ENV_RESULT_ARTIFACT_ROOT",
    "RESULT_ARTIFACT_FILE_MODE",
    "RESULT_ARTIFACT_ROOT_MODE",
    "VCF_EXTENSION",
    "RESULT_MANIFEST_EXTENSION",
    "VCF_ARTIFACT_KIND",
    "RESULT_MANIFEST_ARTIFACT_KIND",
    "ResultArtifactRootError",
    "ResultArtifactIntegrityError",
    "PublishedResultArtifact",
    "ResultArtifactPublisher",
    "result_artifact_root_from_env",
]

ENV_RESULT_ARTIFACT_ROOT = "MINOS_L2F_RESULT_ARTIFACT_ROOT"
RESULT_ARTIFACT_FILE_MODE = 0o640
RESULT_ARTIFACT_ROOT_MODE = 0o2750

#: DISTINCT extensions so the two artifact kinds can never share a content-addressed path.
VCF_EXTENSION = ".vcf"
RESULT_MANIFEST_EXTENSION = ".result.json"

VCF_ARTIFACT_KIND = "l2f:gatk-vcf"
RESULT_MANIFEST_ARTIFACT_KIND = "l2f:execution-result-json"

_KINDS: dict[str, tuple[str, str]] = {
    "vcf": (VCF_EXTENSION, L2F_VCF_MEDIA_TYPE),
    "result_manifest": (RESULT_MANIFEST_EXTENSION, L2F_RESULT_MANIFEST_MEDIA_TYPE),
}


class ResultArtifactRootError(MinosEngineError):
    """The provisioned result-artifact root does not satisfy the frozen root contract."""


class ResultArtifactIntegrityError(MinosEngineError):
    """A published/existing result artifact failed byte or credential verification."""


@dataclass(frozen=True)
class PublishedResultArtifact:
    """Result of publishing one content-addressed F5 artifact."""

    kind: str
    sha256: str
    path: Path
    uri: str
    size_bytes: int
    media_type: str
    provenance: str
    #: True iff THIS call created the final inode (False = an identical file was verified+reused).
    created: bool
    dev: int
    ino: int


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_existing_bytes(path: Path, expected_sha256: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ResultArtifactIntegrityError(f"result artifact is not a regular file: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ResultArtifactIntegrityError(
            f"pre-existing result artifact at {path} does not match its content hash (unchanged)"
        )


def _verify_inode_credential(path: Path, gid: int) -> None:
    if path.is_symlink():
        raise ResultArtifactIntegrityError(f"result artifact {path} is a symlink")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise ResultArtifactIntegrityError(f"result artifact {path} is not a regular file")
    if st.st_uid != os.getuid():
        raise ResultArtifactIntegrityError(f"result artifact {path} not owned by the writer uid")
    if st.st_gid != gid:
        raise ResultArtifactIntegrityError(f"result artifact {path} has the wrong gid")
    if stat.S_IMODE(st.st_mode) != RESULT_ARTIFACT_FILE_MODE:
        raise ResultArtifactIntegrityError(f"result artifact {path} has the wrong mode")


def _unlink_if_same_inode(path: Path, dev: int, ino: int) -> None:
    """Unlink ONLY if lstat proves the path still names exactly ``(dev, ino)``."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISLNK(st.st_mode) and st.st_dev == dev and st.st_ino == ino:
        with contextlib.suppress(OSError):
            os.unlink(path)


class ResultArtifactPublisher:
    """A provisioned content-addressed publisher for the two F5 artifact kinds."""

    def __init__(self, root: Path, *, expected_gid: int | None = None) -> None:
        self._root = self._validate_root(root, expected_gid=expected_gid)
        self._gid = self._root.stat().st_gid

    @staticmethod
    def _validate_root(root: Path, *, expected_gid: int | None) -> Path:
        if root.is_symlink():
            raise ResultArtifactRootError(f"result artifact root {root} is a symlink")
        if not root.is_dir():
            raise ResultArtifactRootError(
                f"result artifact root {root} is not an existing directory"
            )
        st = root.stat()
        if st.st_uid != os.getuid():
            raise ResultArtifactRootError(
                f"result artifact root {root} not owned by the writer uid"
            )
        if stat.S_IMODE(st.st_mode) != RESULT_ARTIFACT_ROOT_MODE:
            raise ResultArtifactRootError(
                f"result artifact root {root} must have mode {oct(RESULT_ARTIFACT_ROOT_MODE)}"
            )
        if expected_gid is not None and st.st_gid != expected_gid:
            raise ResultArtifactRootError(f"result artifact root {root} has an unexpected gid")
        return root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def gid(self) -> int:
        return self._gid

    def content_uri(self, kind: str, sha256: str) -> str:
        extension, _media = self._kind(kind)
        return f"file://{(self._root / f'{sha256}{extension}').resolve()}"

    @staticmethod
    def _kind(kind: str) -> tuple[str, str]:
        try:
            return _KINDS[kind]
        except KeyError as exc:
            raise ResultArtifactIntegrityError(f"unknown result artifact kind {kind!r}") from exc

    def unpublish_if_created(self, artifact: PublishedResultArtifact) -> None:
        """Remove a file THIS call created (rollback cleanup); never a reused/concurrent one."""
        if artifact.created:
            _unlink_if_same_inode(artifact.path, artifact.dev, artifact.ino)
            with contextlib.suppress(OSError):
                _fsync_directory(self._root)

    def publish(self, payload: bytes, *, kind: str, sha256: str) -> PublishedResultArtifact:
        """Publish ``payload`` (whose sha256 MUST equal ``sha256``) to its immutable
        content-addressed path with no-clobber semantics; get-or-verify an existing file."""
        extension, media_type = self._kind(kind)
        provenance = VCF_ARTIFACT_KIND if kind == "vcf" else RESULT_MANIFEST_ARTIFACT_KIND
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise ResultArtifactIntegrityError(
                f"{kind} payload bytes do not hash to the claimed sha256"
            )
        final_path = self._root / f"{sha256}{extension}"
        uri = self.content_uri(kind, sha256)

        def _result(created: bool, dev: int, ino: int) -> PublishedResultArtifact:
            return PublishedResultArtifact(
                kind=kind,
                sha256=sha256,
                path=final_path,
                uri=uri,
                size_bytes=len(payload),
                media_type=media_type,
                provenance=provenance,
                created=created,
                dev=dev,
                ino=ino,
            )

        if final_path.exists():
            _verify_existing_bytes(final_path, sha256)
            _verify_inode_credential(final_path, self._gid)
            st = final_path.stat()
            return _result(False, st.st_dev, st.st_ino)

        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{sha256}-", dir=self._root)
        tmp_path = Path(tmp_name)
        fd_open = True
        try:
            mv = memoryview(payload)
            while mv:
                mv = mv[os.write(tmp_fd, mv) :]
            os.fsync(tmp_fd)
            os.fchmod(tmp_fd, RESULT_ARTIFACT_FILE_MODE)
            os.fchown(tmp_fd, -1, self._gid)
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            fd_open = False
            if hashlib.sha256(tmp_path.read_bytes()).hexdigest() != sha256:  # pragma: no cover
                raise ResultArtifactIntegrityError(
                    "written bytes do not hash to the claimed sha256"
                )
        except BaseException:
            if fd_open:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise

        tst = os.lstat(tmp_path)
        created_dev, created_ino = tst.st_dev, tst.st_ino
        linked = False
        try:
            try:
                os.link(tmp_path, final_path)  # fails closed if the target already exists
            except FileExistsError:
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()
                _verify_existing_bytes(final_path, sha256)
                _verify_inode_credential(final_path, self._gid)
                st = final_path.stat()
                return _result(False, st.st_dev, st.st_ino)
            linked = True
            fst = os.lstat(final_path)
            if (fst.st_dev, fst.st_ino) != (created_dev, created_ino):
                raise ResultArtifactIntegrityError(
                    "final path does not name the freshly linked inode (replaced concurrently)"
                )
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            _fsync_directory(self._root)
            _verify_inode_credential(final_path, self._gid)
        except BaseException:
            if linked:
                _unlink_if_same_inode(final_path, created_dev, created_ino)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            with contextlib.suppress(OSError):
                _fsync_directory(self._root)
            raise
        return _result(True, created_dev, created_ino)


def result_artifact_root_from_env() -> Path:
    raw = os.environ.get(ENV_RESULT_ARTIFACT_ROOT)
    if raw is None or not raw.strip():
        raise ResultArtifactRootError(
            f"{ENV_RESULT_ARTIFACT_ROOT} is not set; the provisioned result-artifact root "
            f"(mode {oct(RESULT_ARTIFACT_ROOT_MODE)}) must be configured explicitly"
        )
    return Path(raw.strip())
