"""L2-F F3-C1 content-addressed CONFIG-payload artifact publisher.

Publishes each accepted canonical CONFIG payload (the exact
``canonical_json_bytes(effective_config)`` bytes, ``sha256 == config_hash``) as an immutable,
content-addressed file and hands the caller enough information to register it in
``catalog.artifacts``. It is a **provisioned** publisher: the root must already exist with the
frozen ownership/mode contract — it is never created or silently repaired here.

This is a NEW publisher, deliberately not the partition-scoped train/validation feature-matrix
publisher (``layer2.features.matrix_parquet``): CONFIG payloads are content-addressed and NOT
partition-scoped, so reusing the partition-gid credential split would confuse that
partition-specific contract. The atomic-publish state machine mirrors the proven matrix one.

Frozen root/file permission contract
------------------------------------
* root: an existing, non-symlink directory owned by the writer uid (``os.getuid()``), mode
  EXACTLY ``0o2750`` (``CONFIG_ARTIFACT_ROOT_MODE``); its gid is the publish gid.
* file: content-addressed ``<root>/<config_hash>.json``, a regular non-symlink file owned by
  the writer uid, carrying the root gid, mode EXACTLY ``0o640`` (``CONFIG_ARTIFACT_FILE_MODE``),
  whose bytes hash to ``config_hash``.
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
from minos_engine.storage.l2f_migration_contract import L2F_CONFIG_PAYLOAD_MEDIA_TYPE

__all__ = [
    "CONFIG_ARTIFACT_FILE_MODE",
    "CONFIG_ARTIFACT_ROOT_MODE",
    "CONFIG_ARTIFACT_EXTENSION",
    "CONFIG_ARTIFACT_MEDIA_TYPE",
    "CONFIG_ARTIFACT_KIND",
    "ConfigArtifactRootError",
    "ConfigArtifactIntegrityError",
    "PublishedConfigArtifact",
    "ConfigPayloadPublisher",
]

CONFIG_ARTIFACT_FILE_MODE = 0o640
CONFIG_ARTIFACT_ROOT_MODE = 0o2750
CONFIG_ARTIFACT_EXTENSION = ".json"
CONFIG_ARTIFACT_MEDIA_TYPE = L2F_CONFIG_PAYLOAD_MEDIA_TYPE
CONFIG_ARTIFACT_KIND = "l2f:config-payload-json"


class ConfigArtifactRootError(MinosEngineError):
    """The provisioned CONFIG-artifact root does not satisfy the frozen root contract."""


class ConfigArtifactIntegrityError(MinosEngineError):
    """A published/existing CONFIG artifact failed byte or credential verification."""


@dataclass(frozen=True)
class PublishedConfigArtifact:
    """Result of publishing one content-addressed CONFIG payload."""

    config_hash: str
    path: Path
    uri: str
    size_bytes: int
    media_type: str
    #: True iff THIS call created the final inode (False = an identical file already existed
    #: and was verified + reused). Only a created inode may be unpublished on rollback.
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
        raise ConfigArtifactIntegrityError(f"config artifact is not a regular file: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ConfigArtifactIntegrityError(
            f"pre-existing config artifact at {path} does not match its content hash (unchanged)"
        )


def _verify_inode_credential(path: Path, gid: int) -> None:
    if path.is_symlink():
        raise ConfigArtifactIntegrityError(f"config artifact {path} is a symlink")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise ConfigArtifactIntegrityError(f"config artifact {path} is not a regular file")
    if st.st_uid != os.getuid():
        raise ConfigArtifactIntegrityError(f"config artifact {path} not owned by the writer uid")
    if st.st_gid != gid:
        raise ConfigArtifactIntegrityError(f"config artifact {path} has the wrong gid")
    if stat.S_IMODE(st.st_mode) != CONFIG_ARTIFACT_FILE_MODE:
        raise ConfigArtifactIntegrityError(f"config artifact {path} has the wrong mode")


def _unlink_if_same_inode(path: Path, dev: int, ino: int) -> None:
    """Unlink ``path`` ONLY if lstat proves it still names exactly ``(dev, ino)`` and is not a
    symlink — so a concurrent winner/replacement is never removed; unprovable identity is a
    fail-safe no-op."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISLNK(st.st_mode) and st.st_dev == dev and st.st_ino == ino:
        with contextlib.suppress(OSError):
            os.unlink(path)


class ConfigPayloadPublisher:
    """A provisioned content-addressed CONFIG-payload publisher bound to one validated root."""

    def __init__(self, root: Path, *, expected_gid: int | None = None) -> None:
        self._root = self._validate_root(root, expected_gid=expected_gid)
        self._gid = root.stat().st_gid

    @staticmethod
    def _validate_root(root: Path, *, expected_gid: int | None) -> Path:
        if root.is_symlink():
            raise ConfigArtifactRootError(f"config artifact root {root} is a symlink")
        if not root.is_dir():
            raise ConfigArtifactRootError(
                f"config artifact root {root} is not an existing directory"
            )
        st = root.stat()
        if st.st_uid != os.getuid():
            raise ConfigArtifactRootError(
                f"config artifact root {root} not owned by the writer uid"
            )
        if stat.S_IMODE(st.st_mode) != CONFIG_ARTIFACT_ROOT_MODE:
            raise ConfigArtifactRootError(
                f"config artifact root {root} must have mode {oct(CONFIG_ARTIFACT_ROOT_MODE)}"
            )
        if expected_gid is not None and st.st_gid != expected_gid:
            raise ConfigArtifactRootError(f"config artifact root {root} has an unexpected gid")
        return root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def gid(self) -> int:
        return self._gid

    def content_uri(self, config_hash: str) -> str:
        return f"file://{(self._root / f'{config_hash}{CONFIG_ARTIFACT_EXTENSION}').resolve()}"

    def unpublish_if_created(self, artifact: PublishedConfigArtifact) -> None:
        """Remove a file this call created (rollback cleanup); never a reused/concurrent one."""
        if artifact.created:
            _unlink_if_same_inode(artifact.path, artifact.dev, artifact.ino)
            with contextlib.suppress(OSError):
                _fsync_directory(self._root)

    def publish(self, payload: bytes, *, config_hash: str) -> PublishedConfigArtifact:
        """Publish ``payload`` (whose sha256 MUST equal ``config_hash``) to its immutable
        content-addressed path with no-clobber semantics; get-or-verify an existing file."""
        if hashlib.sha256(payload).hexdigest() != config_hash:
            raise ConfigArtifactIntegrityError(
                "payload bytes do not hash to the claimed config_hash"
            )
        final_path = self._root / f"{config_hash}{CONFIG_ARTIFACT_EXTENSION}"
        uri = self.content_uri(config_hash)

        if final_path.exists():
            _verify_existing_bytes(final_path, config_hash)
            _verify_inode_credential(final_path, self._gid)
            st = final_path.stat()
            return PublishedConfigArtifact(
                config_hash,
                final_path,
                uri,
                len(payload),
                CONFIG_ARTIFACT_MEDIA_TYPE,
                created=False,
                dev=st.st_dev,
                ino=st.st_ino,
            )

        # Phase 1: fully write/credential/close the temp inode BEFORE os.link.
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{config_hash}-", dir=self._root)
        tmp_path = Path(tmp_name)
        fd_open = True
        try:
            mv = memoryview(payload)
            while mv:
                mv = mv[os.write(tmp_fd, mv) :]
            os.fsync(tmp_fd)
            os.fchmod(tmp_fd, CONFIG_ARTIFACT_FILE_MODE)
            os.fchown(tmp_fd, -1, self._gid)
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            fd_open = False
            if hashlib.sha256(tmp_path.read_bytes()).hexdigest() != config_hash:  # pragma: no cover
                raise ConfigArtifactIntegrityError(
                    "written config bytes do not hash to config_hash"
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

        # Phase 2/3: one protected state machine — link, verify inode identity, cleanup temp,
        # fsync dir, verify credential; on failure remove ONLY our freshly linked inode.
        linked = False
        try:
            try:
                os.link(tmp_path, final_path)  # fails closed if the target already exists
            except FileExistsError:
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()
                _verify_existing_bytes(final_path, config_hash)
                _verify_inode_credential(final_path, self._gid)
                st = final_path.stat()
                return PublishedConfigArtifact(
                    config_hash,
                    final_path,
                    uri,
                    len(payload),
                    CONFIG_ARTIFACT_MEDIA_TYPE,
                    created=False,
                    dev=st.st_dev,
                    ino=st.st_ino,
                )
            linked = True
            fst = os.lstat(final_path)
            if (fst.st_dev, fst.st_ino) != (created_dev, created_ino):
                raise ConfigArtifactIntegrityError(
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
        return PublishedConfigArtifact(
            config_hash,
            final_path,
            uri,
            len(payload),
            CONFIG_ARTIFACT_MEDIA_TYPE,
            created=True,
            dev=created_dev,
            ino=created_ino,
        )
