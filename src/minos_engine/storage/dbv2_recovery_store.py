"""The DB-V2 recovery root: an externally provisioned, content-addressed evidence store.

``MINOS_DB_RECOVERY_ROOT`` has no default and no repository-relative fallback — a default would
silently select the source checkout, which is the whole point of publishing recovery evidence
somewhere else. The root must already exist; this module never creates, chmods or repairs it.

Publication is atomic and no-clobber:

    mkstemp in the destination directory -> write -> fsync -> fchmod -> verify size, digest, mode
    and credentials -> link(2) into place -> fsync the directory -> reopen and verify

``link(2)`` fails if the target exists, so a second publication of the same digest is a verified
no-op rather than an overwrite. On failure the temporary inode is removed **only** when this call
proved it created it; a file already published is never removed, because ambiguous cleanup of
immutable evidence is worse than an orphan.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

__all__ = [
    "ENV_RECOVERY_ROOT",
    "FILE_MODE",
    "PublishedFile",
    "RecoveryRoot",
    "RecoveryRootError",
    "RecoveryStoreError",
]

ENV_RECOVERY_ROOT: Final = "MINOS_DB_RECOVERY_ROOT"

#: the frozen permission contract. Nothing here ever changes an existing inode's mode.
ROOT_MODE: Final = 0o2750
FILE_MODE: Final = 0o640

#: content-addressed layout, one subdirectory per evidence kind.
KIND_LAYOUT: Final[dict[str, tuple[str, str]]] = {
    "backup": ("backups", ".dump"),
    "snapshot": ("snapshots", ".snapshot.json"),
    "recovery": ("recovery", ".recovery.json"),
}


class RecoveryStoreError(RuntimeError):
    """A recovery-store operation failed closed."""


class RecoveryRootError(RecoveryStoreError):
    """The configured recovery root does not satisfy the frozen contract."""


@dataclass(frozen=True, slots=True)
class PublishedFile:
    """One immutable published evidence file."""

    kind: str
    sha256: str
    size_bytes: int
    relative_path: str
    already_present: bool

    def path_under(self, root: Path) -> Path:
        return root / self.relative_path


def _no_symlink_component(path: Path) -> None:
    """Reject a symlink anywhere in the resolved chain, not merely at the leaf."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise RecoveryRootError(f"{ENV_RECOVERY_ROOT} contains a symlink component: {current}")


class RecoveryRoot:
    """A validated recovery root. Construction is the only place the contract is checked."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise RecoveryRootError(f"{ENV_RECOVERY_ROOT} must be absolute, got {path}")
        if ".." in path.parts:
            raise RecoveryRootError(f"{ENV_RECOVERY_ROOT} must not contain '..': {path}")
        _no_symlink_component(path)
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as error:
            raise RecoveryRootError(
                f"{ENV_RECOVERY_ROOT} must already exist; this application never creates it: {path}"
            ) from error
        if not stat.S_ISDIR(info.st_mode):
            raise RecoveryRootError(f"{ENV_RECOVERY_ROOT} is not a directory: {path}")
        if stat.S_IMODE(info.st_mode) != ROOT_MODE:
            raise RecoveryRootError(
                f"{ENV_RECOVERY_ROOT} must be mode {ROOT_MODE:04o}, found "
                f"{stat.S_IMODE(info.st_mode):04o}; this application never repairs it"
            )
        self._path = path

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> RecoveryRoot:
        source = os.environ if environ is None else environ
        raw = source.get(ENV_RECOVERY_ROOT)
        if not raw:
            raise RecoveryRootError(
                f"{ENV_RECOVERY_ROOT} is not set; it has no default and no repository-relative "
                "fallback, because a default would silently select the source checkout"
            )
        return cls(Path(raw))

    @property
    def path(self) -> Path:
        return self._path

    def relative_path_for(self, kind: str, digest: str) -> str:
        try:
            directory, suffix = KIND_LAYOUT[kind]
        except KeyError as error:
            raise RecoveryStoreError(f"unknown evidence kind {kind!r}") from error
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RecoveryStoreError(f"digest must be 64 lowercase hex characters, got {digest!r}")
        return str(PurePosixPath(directory) / f"{digest}{suffix}")

    # -- reading -------------------------------------------------------------------------
    def read(self, kind: str, digest: str) -> bytes:
        """Read a published file through a descriptor that refuses to follow a symlink."""
        relative = self.relative_path_for(kind, digest)
        target = self._path / relative
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError as error:
            raise RecoveryStoreError(f"recovery evidence is missing: {relative}") from error
        except OSError as error:
            raise RecoveryStoreError(f"recovery evidence is not a regular file: {relative}") from (
                error
            )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RecoveryStoreError(f"recovery evidence is not a regular file: {relative}")
            payload = b""
            while chunk := os.read(fd, 1 << 20):
                payload += chunk
        finally:
            os.close(fd)
        observed = hashlib.sha256(payload).hexdigest()
        if observed != digest:
            raise RecoveryStoreError(
                f"published evidence {relative} hashes to {observed}, not {digest}"
            )
        return payload

    def stat_mode(self, kind: str, digest: str) -> int:
        target = self._path / self.relative_path_for(kind, digest)
        return stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode)

    def exists(self, kind: str, digest: str) -> bool:
        target = self._path / self.relative_path_for(kind, digest)
        try:
            return stat.S_ISREG(os.stat(target, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False

    # -- publication ---------------------------------------------------------------------
    def publish(self, kind: str, payload: bytes) -> PublishedFile:
        """Publish ``payload`` immutably under its own digest. A second call is a verified no-op."""
        digest = hashlib.sha256(payload).hexdigest()
        relative = self.relative_path_for(kind, digest)
        target = self._path / relative
        directory = target.parent
        if not directory.is_dir():
            raise RecoveryStoreError(
                f"{ENV_RECOVERY_ROOT} is missing the {directory.name!r} subdirectory; the "
                "recovery root is provisioned externally and never created here"
            )
        if self.exists(kind, digest):
            self._verify_published(target, payload, digest)
            return PublishedFile(kind, digest, len(payload), relative, already_present=True)

        handle, temporary_name = tempfile.mkstemp(dir=directory, prefix=".publish-")
        temporary = Path(temporary_name)
        created_inode = os.fstat(handle).st_ino
        published = False
        try:
            os.write(handle, payload)
            os.fsync(handle)
            os.fchmod(handle, FILE_MODE)
            info = os.fstat(handle)
            if info.st_size != len(payload):
                raise RecoveryStoreError(
                    f"wrote {info.st_size} bytes for {relative}, expected {len(payload)}"
                )
            if stat.S_IMODE(info.st_mode) != FILE_MODE:
                raise RecoveryStoreError(f"{relative} is mode {stat.S_IMODE(info.st_mode):04o}")
            if info.st_uid != os.geteuid():
                raise RecoveryStoreError(f"{relative} is owned by uid {info.st_uid}")
            os.lseek(handle, 0, os.SEEK_SET)
            written = b""
            while chunk := os.read(handle, 1 << 20):
                written += chunk
            if hashlib.sha256(written).hexdigest() != digest:
                raise RecoveryStoreError(f"{relative} does not read back to its own digest")
            os.close(handle)
            handle = -1
            try:
                os.link(temporary, target)
            except FileExistsError:
                # someone else published the same bytes first; that is the no-clobber contract
                self._verify_published(target, payload, digest)
                return PublishedFile(kind, digest, len(payload), relative, already_present=True)
            published = True
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._verify_published(target, payload, digest)
            return PublishedFile(kind, digest, len(payload), relative, already_present=False)
        finally:
            if handle >= 0:
                os.close(handle)
            # remove ONLY the inode this call proved it created, and never the published file
            try:
                leftover = os.stat(temporary, follow_symlinks=False)
            except FileNotFoundError:
                leftover = None
            if leftover is not None and leftover.st_ino == created_inode:
                os.unlink(temporary)
            del published

    def _verify_published(self, target: Path, payload: bytes, digest: str) -> None:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RecoveryStoreError(f"{target.name} is not a regular file")
            if info.st_size != len(payload):
                raise RecoveryStoreError(
                    f"{target.name} is {info.st_size} bytes, expected {len(payload)}"
                )
            if stat.S_IMODE(info.st_mode) != FILE_MODE:
                raise RecoveryStoreError(
                    f"{target.name} is mode {stat.S_IMODE(info.st_mode):04o}, "
                    f"expected {FILE_MODE:04o}"
                )
            content = b""
            while chunk := os.read(fd, 1 << 20):
                content += chunk
        finally:
            os.close(fd)
        if hashlib.sha256(content).hexdigest() != digest:
            raise RecoveryStoreError(f"{target.name} does not hash to {digest}")
        if content != payload:
            raise RecoveryStoreError(f"{target.name} holds different bytes than this call")
