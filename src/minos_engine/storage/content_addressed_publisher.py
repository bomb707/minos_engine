"""The audited content-addressed publication protocol, factored for reuse.

This is the SAME protocol the F3-C1 config publisher and the L2-F1 F5 result publisher were
audited under, lifted out so a later stage cannot quietly grow a second, weaker one:

    temp inode -> write -> fsync -> fchmod/fchown -> fsync -> record temp (dev, ino)
    -> hard-link no-clobber into the final name -> prove the final path names THAT inode
    -> directory fsync -> re-verify final credentials

Properties that make it safe rather than merely convenient:

* **No-clobber.** ``os.link`` fails closed when the final name exists, so a published object is
  never overwritten; an existing object is byte-verified and reused instead.
* **Concurrency.** Two processes publishing identical bytes converge on one inode; the loser
  verifies and reuses. A final path that names a different inode than the one just linked is a
  refusal, not a silent success.
* **Rollback honesty.** Cleanup unlinks a path ONLY when ``lstat`` still proves it names the
  exact ``(dev, ino)`` this call created, so a concurrent publisher's object is never removed.
* **Symlinks are never followed** for the root, the final object, or an existing object.

The caller supplies a :class:`ContentAddressedSpec` naming its own error types and permission
contract, so each stage keeps its own typed failures and audited modes.
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

__all__ = [
    "ContentAddressedSpec",
    "ContentAddressedStore",
    "PublishedObject",
]


@dataclass(frozen=True)
class ContentAddressedSpec:
    """The per-stage publication contract: what it is called, and how strict it is."""

    label: str
    root_mode: int
    file_mode: int
    root_error: type[MinosEngineError]
    integrity_error: type[MinosEngineError]


@dataclass(frozen=True)
class PublishedObject:
    """One published content-addressed object."""

    sha256: str
    path: Path
    uri: str
    size_bytes: int
    #: True iff THIS call created the final inode (False = an identical object was verified+reused).
    created: bool
    dev: int
    ino: int


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_if_same_inode(path: Path, dev: int, ino: int) -> None:
    """Unlink ONLY if ``lstat`` still proves the path names exactly ``(dev, ino)``."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISLNK(st.st_mode) and st.st_dev == dev and st.st_ino == ino:
        with contextlib.suppress(OSError):
            os.unlink(path)


class ContentAddressedStore:
    """A validated publication root implementing the audited atomic protocol."""

    def __init__(
        self, root: Path, spec: ContentAddressedSpec, *, expected_gid: int | None = None
    ) -> None:
        self._spec = spec
        self._root = self._validate_root(root, spec, expected_gid=expected_gid)
        self._gid = self._root.stat().st_gid

    @staticmethod
    def _validate_root(root: Path, spec: ContentAddressedSpec, *, expected_gid: int | None) -> Path:
        if root.is_symlink():
            raise spec.root_error(f"{spec.label} root {root} is a symlink")
        if not root.is_dir():
            raise spec.root_error(f"{spec.label} root {root} is not an existing directory")
        st = root.stat()
        if st.st_uid != os.getuid():
            raise spec.root_error(f"{spec.label} root {root} not owned by the writer uid")
        if stat.S_IMODE(st.st_mode) != spec.root_mode:
            raise spec.root_error(f"{spec.label} root {root} must have mode {oct(spec.root_mode)}")
        if expected_gid is not None and st.st_gid != expected_gid:
            raise spec.root_error(f"{spec.label} root {root} has an unexpected gid")
        return root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def gid(self) -> int:
        return self._gid

    def path_for(self, sha256: str, extension: str) -> Path:
        return self._root / f"{sha256}{extension}"

    def uri_for(self, sha256: str, extension: str) -> str:
        return f"file://{self.path_for(sha256, extension).resolve()}"

    def verify_existing(self, path: Path, sha256: str) -> None:
        """Byte and credential verification of an object this call did not create."""
        spec = self._spec
        if path.is_symlink() or not path.is_file():
            raise spec.integrity_error(f"{spec.label} is not a regular file: {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            raise spec.integrity_error(
                f"pre-existing {spec.label} at {path} does not match its content hash (unchanged)"
            )
        self.verify_credentials(path)

    def verify_credentials(self, path: Path) -> None:
        spec = self._spec
        if path.is_symlink():
            raise spec.integrity_error(f"{spec.label} {path} is a symlink")
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            raise spec.integrity_error(f"{spec.label} {path} is not a regular file")
        if st.st_uid != os.getuid():
            raise spec.integrity_error(f"{spec.label} {path} not owned by the writer uid")
        if st.st_gid != self._gid:
            raise spec.integrity_error(f"{spec.label} {path} has the wrong gid")
        if stat.S_IMODE(st.st_mode) != spec.file_mode:
            raise spec.integrity_error(f"{spec.label} {path} has the wrong mode")

    def unpublish_if_created(self, obj: PublishedObject) -> None:
        """Rollback cleanup: remove an inode THIS call created; never a reused/concurrent one."""
        if obj.created:
            _unlink_if_same_inode(obj.path, obj.dev, obj.ino)
            with contextlib.suppress(OSError):
                _fsync_directory(self._root)

    def publish(self, payload: bytes, *, sha256: str, extension: str) -> PublishedObject:
        """Publish ``payload`` (whose digest MUST equal ``sha256``) with no-clobber semantics."""
        spec = self._spec
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise spec.integrity_error(f"{spec.label} bytes do not hash to the claimed sha256")
        final_path = self.path_for(sha256, extension)
        uri = self.uri_for(sha256, extension)

        def _result(created: bool, dev: int, ino: int) -> PublishedObject:
            return PublishedObject(
                sha256=sha256,
                path=final_path,
                uri=uri,
                size_bytes=len(payload),
                created=created,
                dev=dev,
                ino=ino,
            )

        if final_path.exists() or final_path.is_symlink():
            self.verify_existing(final_path, sha256)
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
            os.fchmod(tmp_fd, spec.file_mode)
            os.fchown(tmp_fd, -1, self._gid)
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            fd_open = False
            if hashlib.sha256(tmp_path.read_bytes()).hexdigest() != sha256:  # pragma: no cover
                raise spec.integrity_error("written bytes do not hash to the claimed sha256")
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
                self.verify_existing(final_path, sha256)
                st = final_path.stat()
                return _result(False, st.st_dev, st.st_ino)
            linked = True
            fst = os.lstat(final_path)
            if (fst.st_dev, fst.st_ino) != (created_dev, created_ino):
                raise spec.integrity_error(
                    "final path does not name the freshly linked inode (replaced concurrently)"
                )
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            _fsync_directory(self._root)
            self.verify_credentials(final_path)
        except BaseException:
            if linked:
                _unlink_if_same_inode(final_path, created_dev, created_ino)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            with contextlib.suppress(OSError):
                _fsync_directory(self._root)
            raise
        return _result(True, created_dev, created_ino)
