"""The audited fresh per-attempt workspace, shared by every stage that runs an external tool.

L2-F1 GATK execution and L2-F2 evaluation both need one private, provably-fresh directory per
attempt. This module is that single implementation, kept deliberately neutral: it knows nothing
about GATK, hap.py, jobs or evaluations, so the offline evaluator can reuse it without importing
the execution path it must never touch.

The safety properties are the ones L2-F1 was audited under, unchanged:

* ``mkdir`` without ``exist_ok`` — a stale directory, a planted file or a substituted symlink is
  never adopted;
* the created ``(st_dev, st_ino)`` is captured immediately, before any further check;
* a private ``O_EXCL`` sentinel with an unguessable name defeats inode-number reuse;
* a RETAINED ``O_DIRECTORY | O_NOFOLLOW`` descriptor makes every later operation
  descriptor-relative, closing the check/use race a pathname-based ``rmtree`` leaves open;
* removal touches ONLY the inode this process created.

The typed error is supplied by the caller, so each stage keeps its own failure vocabulary.
"""

from __future__ import annotations

import contextlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ATTEMPT_DIR_MODE",
    "AttemptWorkspace",
    "create_attempt_workspace",
    "reject_symlinked_components",
    "remove_attempt_workspace",
]

#: every per-attempt work directory is created private to the executing user.
ATTEMPT_DIR_MODE = 0o700


@dataclass
class AttemptWorkspace:
    """One per-attempt directory bound to the EXACT inode this process created.

    Identity is pinned three ways, because each alone is defeatable:

    * ``(st_dev, st_ino)`` captured immediately after ``mkdir``;
    * a private ``O_EXCL`` sentinel with an unguessable name, because a filesystem may REUSE an
      inode number after ``rmdir`` — a replacement directory cannot reproduce the sentinel;
    * a RETAINED directory descriptor (``O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC``) opened at
      creation time. Every later child operation is **descriptor-relative**, so the pathname is
      never re-resolved and a replacement installed at :attr:`path` can never be traversed,
      read through or deleted — closing the check/use race a pathname-based ``rmtree`` leaves
      open.

    A parent descriptor is retained as well, so the final ``rmdir`` of the attempt entry itself
    is performed relative to the parent inode this attempt actually created its entry in.
    """

    path: Path
    st_dev: int
    st_ino: int
    sentinel: str
    dir_fd: int | None = None
    parent_fd: int | None = None

    # -- identity ---------------------------------------------------------------------------- #
    def same_inode(self) -> bool:
        """True when the PATH still resolves to a directory with the created ``(dev, ino)``.

        This is the check that detects a replacement installed at :attr:`path`; it is deliberately
        pathname-based, because that is exactly the substitution it exists to notice.
        """
        try:
            info = os.lstat(self.path)
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode) and info.st_dev == self.st_dev and info.st_ino == self.st_ino
        )

    def descriptor_valid(self) -> bool:
        """True when the RETAINED descriptor still refers to the directory we created."""
        if self.dir_fd is None:
            return False
        try:
            info = os.fstat(self.dir_fd)
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode) and info.st_dev == self.st_dev and info.st_ino == self.st_ino
        )

    def still_ours(self) -> bool:
        """True only when the path AND the retained descriptor are the directory we created."""
        if not self.same_inode() or not self.descriptor_valid():
            return False
        try:  # the sentinel is looked up RELATIVE to the retained descriptor
            marker = os.stat(self.sentinel, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError:
            return False  # an inode-number reuse cannot reproduce the private sentinel
        return stat.S_ISREG(marker.st_mode)

    # -- descriptor lifecycle ---------------------------------------------------------------- #
    def close(self) -> None:
        """Close both retained descriptors EXACTLY once, on every path (idempotent)."""
        for name in ("dir_fd", "parent_fd"):
            fd = getattr(self, name)
            setattr(self, name, None)  # cleared FIRST, so a second call can never double-close
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def reject_symlinked_components(path: Path, *, error: type[Exception]) -> Path:
    """Reject a path any of whose components (including intermediates) is a symlink."""
    absolute = Path(os.path.abspath(path))
    walked = Path(absolute.anchor or os.sep)
    for part in absolute.relative_to(walked).parts:
        walked = walked / part
        if walked.is_symlink():
            raise error(f"path component {walked} is a symlink; symlinked work paths are refused")
    return absolute


def _remove_children_at(dir_fd: int) -> None:
    """Recursively empty a directory using ONLY descriptor-relative operations.

    Nothing here re-resolves a pathname and nothing follows a symlink, so a replacement installed
    at the attempt path cannot be traversed. Sub-directories are opened relative to their parent
    descriptor with ``O_NOFOLLOW``, so a symlink swapped in for a child is unlinked, never
    followed.
    """
    for name in os.listdir(dir_fd):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
                )
            except OSError:
                continue
            try:
                _remove_children_at(child)
            finally:
                with contextlib.suppress(OSError):
                    os.close(child)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=dir_fd)
        else:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)


def _remove_created_inode(workspace: AttemptWorkspace, *, require_sentinel: bool = True) -> None:
    """Remove ONLY the directory inode this attempt created; never a replacement.

    Children are removed through the RETAINED descriptor, so the untrusted pathname is never
    traversed. The attempt's own directory entry is then removed relative to the retained PARENT
    descriptor, and only after re-confirming that the entry still names our exact inode; if that
    identity cannot be established the entry is LEFT ALONE rather than risking the deletion of a
    replacement. ``rmdir`` additionally refuses a non-empty directory, so a replacement that has
    had anything written into it survives even the final step.

    ``require_sentinel=False`` is used exclusively on the creation-failure path, where the
    sentinel may not have been written yet.
    """
    if require_sentinel:
        if not workspace.still_ours():
            workspace.close()
            return
    elif not workspace.descriptor_valid() and not workspace.same_inode():
        workspace.close()
        return

    try:
        if workspace.dir_fd is not None and workspace.descriptor_valid():
            _remove_children_at(workspace.dir_fd)
        _rmdir_entry(workspace)
    finally:
        workspace.close()


def _rmdir_entry(workspace: AttemptWorkspace) -> None:
    """Remove the attempt's own directory entry relative to the retained parent descriptor."""
    name = workspace.path.name
    parent_fd = workspace.parent_fd
    try:
        if parent_fd is not None:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        else:  # pragma: no cover - the parent descriptor is always retained on the created path
            entry = os.lstat(workspace.path)
    except OSError:
        return
    if not stat.S_ISDIR(entry.st_mode):
        return
    if (entry.st_dev, entry.st_ino) != (workspace.st_dev, workspace.st_ino):
        return  # a replacement occupies the entry: leave it entirely alone
    with contextlib.suppress(OSError):
        if parent_fd is not None:
            os.rmdir(name, dir_fd=parent_fd)
        else:  # pragma: no cover - see above
            os.rmdir(workspace.path)


def create_attempt_workspace(
    work_root: Path, *, name: str, error: type[Exception]
) -> AttemptWorkspace:
    """Create a FRESH, EXCLUSIVE per-attempt directory (never ``exist_ok``, never reused).

    This is the generic, audited core. L2-F1 execution and L2-F2 evaluation both use it rather
    than growing separate workspace protocols; only the entry NAME and the typed error differ.

    ``mkdir`` without ``exist_ok`` fails closed if anything already occupies the path, so a stale
    directory, a pre-planted output file or a substituted symlink cannot be adopted. The created
    inode's ``(st_dev, st_ino)`` is captured immediately, a descriptor onto that exact inode is
    retained for the attempt's whole lifetime, and every later check runs against that identity;
    if validation fails afterwards, ONLY that inode is removed.
    """
    root = reject_symlinked_components(work_root, error=error)
    if not root.is_dir():
        raise error(f"work root {root} is not an existing directory")
    attempt = root / name
    try:
        attempt.mkdir(mode=ATTEMPT_DIR_MODE)
    except FileExistsError as exc:
        raise error(f"attempt work directory {attempt} already exists; it is never reused") from exc
    except OSError as exc:
        raise error(f"attempt work directory {attempt} is unusable: {exc}") from exc

    # capture the created inode identity BEFORE any further check, so a substitution racing the
    # validation can only ever fail the validation - never be adopted, and never be deleted.
    try:
        info = os.lstat(attempt)
    except OSError as exc:  # pragma: no cover - the directory was just created
        raise error(f"attempt work directory {attempt} vanished") from exc
    sentinel = f".minos-attempt-{uuid.uuid4().hex}"
    workspace = AttemptWorkspace(
        path=attempt, st_dev=info.st_dev, st_ino=info.st_ino, sentinel=sentinel
    )

    try:
        try:  # RETAIN descriptors: every later child operation is descriptor-relative
            workspace.parent_fd = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            workspace.dir_fd = os.open(
                attempt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError as exc:
            raise error(f"attempt work directory {attempt} could not be opened: {exc}") from exc
        if not workspace.descriptor_valid():
            raise error(
                f"attempt work directory {attempt} descriptor does not match the created inode"
            )
        try:  # a private O_EXCL marker pins the identity beyond inode-number reuse
            os.close(
                os.open(
                    sentinel,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=workspace.dir_fd,
                )
            )
        except OSError as exc:
            raise error(f"attempt work directory {attempt} could not be marked: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise error(f"attempt work directory {attempt} is not a directory")
        if info.st_uid != os.geteuid():
            raise error(f"attempt work directory {attempt} is not owned by this user")
        if stat.S_IMODE(info.st_mode) != ATTEMPT_DIR_MODE:
            raise error(
                f"attempt work directory {attempt} has mode {stat.S_IMODE(info.st_mode):#o}, "
                f"expected {ATTEMPT_DIR_MODE:#o}"
            )
        if attempt.parent != root:
            raise error(
                f"attempt work directory {attempt} is not directly under the work root {root}"
            )
        if not workspace.still_ours():  # pragma: no cover - substitution racing this call
            raise error(f"attempt work directory {attempt} was replaced during validation")
    except BaseException:
        # remove ONLY the inode this call created (the sentinel may not exist yet).
        _remove_created_inode(workspace, require_sentinel=False)
        raise
    return workspace


def remove_attempt_workspace(workspace: AttemptWorkspace | None) -> None:
    """Inode-safe removal of a workspace created by :func:`create_attempt_workspace`.

    Removal goes through the RETAINED descriptor, so only the inode this process created is
    ever removed — never a replacement installed at the same path.
    """
    if workspace is None:
        return
    _remove_created_inode(workspace)
