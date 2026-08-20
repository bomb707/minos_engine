"""Partition-scoped matrix-artifact retrieval boundary + real credential model.

Database grants alone are insufficient if both roles can open the same file. This module
pairs the 0005 partition views with a filesystem credential model that a real deployment
enforces at the OS level, and structures retrieval so no single caller-side object ever
holds both partition roots.

Components
----------
* :class:`MatrixArtifactBroker` — OWNER-SIDE (admin) component. It knows both partition
  roots and, given an owner connection, mints a :class:`PartitionArtifactReader` bound to
  the authenticated caller's partition and ONLY that partition's root. The broker never
  hands a train reader the validation root (or vice versa).
* :class:`PartitionArtifactReader` — a ROLE-SPECIFIC runtime component holding exactly
  ONE partition credential (its partition + its single root). It resolves artifacts for
  its own partition through the grant-enforced view on the caller's own connection,
  confines paths to its single root, and verifies bytes against the view-bound
  ``artifact_sha256``. There is deliberately no test partition anywhere.

Credential model (real, OS-enforced)
------------------------------------
A defensible local-filesystem deployment gives each partition root a DISTINCT OS group,
owned by the admin/storage writer, mode setgid + group-read + no other/world
(``0o2750``); matrix files are group-owned by the partition group and not group-writable
(``0o0640``). A trainer OS identity is a member of only the train group and a validation
OS identity only the validation group, so neither can open the other partition's files.

:func:`verify_operational_credentials` checks these invariants against the real inode
ownership/mode and returns ``PASS`` only when DISTINCT partition groups are actually
configured and enforce owner-write / partition-read. It returns ``HOLD`` (never a false
PASS) when the roots merely share the same UID under owner-only permissions — same-UID
owner-only directories are NOT partition isolation. :func:`verify_partition_capability`
is the structural/policy check that runs anywhere (distinct non-overlapping roots, path
confinement, no test path). Deployment secrets, real URIs, and payloads never live in Git.
"""

from __future__ import annotations

import grp
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from minos_engine.common.errors import MatrixAccessError

__all__ = [
    "PARTITION_ROLES",
    "PARTITION_VIEWS",
    "CredentialStatus",
    "PartitionArtifactReader",
    "MatrixArtifactBroker",
    "configure_partition_root",
    "verify_partition_capability",
    "verify_operational_credentials",
]

#: Authenticated DB identity → partition. There is deliberately NO test entry.
PARTITION_ROLES: dict[str, str] = {
    "minos_trainer": "train",
    "minos_evaluator": "validation",
}

#: Partition → the grant-enforced view the caller's own connection must query.
PARTITION_VIEWS: dict[str, str] = {
    "train": "profiling.training_matrix",
    "validation": "evaluation.validation_matrix",
}

#: The setgid + owner-rwx + group-r-x directory mode a partition root must carry.
_DIR_MODE = 0o2750
#: The owner-rw + group-r file mode a matrix artifact must carry.
_FILE_MODE = 0o0640


def _partition_for_connection(conn: Connection) -> str:
    """Partition derived from the AUTHENTICATED identity (current_user)."""
    current_user = str(conn.execute(text("SELECT current_user")).scalar_one())
    partition = PARTITION_ROLES.get(current_user)
    if partition is None:
        raise MatrixAccessError(f"database identity {current_user!r} has no matrix partition")
    return partition


class PartitionArtifactReader:
    """A role-specific reader holding ONE partition credential (partition + single root).

    Constructed by the owner-side broker for the caller's authenticated partition; it
    never learns the other partition's root.
    """

    def __init__(self, partition: str, root: Path) -> None:
        if partition not in PARTITION_VIEWS:
            raise MatrixAccessError(f"unknown partition {partition!r}")
        self._partition = partition
        self._root = root.resolve()

    @property
    def partition(self) -> str:
        return self._partition

    def _confine(self, uri: str) -> Path:
        candidate = Path(uri)
        if not candidate.is_absolute():
            raise MatrixAccessError("artifact uri must be an absolute path")
        try:
            resolved = candidate.resolve(strict=True)  # follows symlinks
        except OSError as exc:
            raise MatrixAccessError(f"artifact path cannot be resolved: {exc}") from exc
        if not resolved.is_relative_to(self._root):
            raise MatrixAccessError(
                "artifact path escapes the caller's partition root (traversal, symlink, "
                "or wrong-partition location)"
            )
        if not resolved.is_file():
            raise MatrixAccessError("artifact path is not a regular file")
        return resolved

    def fetch_matrix_payload(self, conn: Connection, matrix_hash: str) -> bytes:
        """Read this partition's matrix artifact, verified end-to-end.

        The connection's authenticated identity must match this reader's partition; the
        row is resolved through the grant-enforced partition view; the path is confined
        to this reader's single root; the bytes must hash to the view-bound
        ``artifact_sha256``. Works for zero-row matrices (the view exposes a
        matrix-level row with NULL member columns).
        """
        if _partition_for_connection(conn) != self._partition:
            raise MatrixAccessError("connection identity does not match this partition reader")
        view = PARTITION_VIEWS[self._partition]
        row = (
            conn.execute(
                text(
                    f"SELECT DISTINCT artifact_uri, artifact_sha256, partition "  # noqa: S608
                    f"FROM {view} WHERE matrix_hash = :h"
                ),
                {"h": matrix_hash},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise MatrixAccessError(
                f"matrix {matrix_hash[:12]}… is not visible to this partition identity"
            )
        if row["partition"] != self._partition:  # pragma: no cover - view is partition-fixed
            raise MatrixAccessError("view returned a foreign-partition row")
        path = self._confine(str(row["artifact_uri"]))
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row["artifact_sha256"]:
            raise MatrixAccessError("artifact bytes do not hash to the view-bound artifact_sha256")
        return payload


class MatrixArtifactBroker:
    """Owner-side broker: holds both roots, hands out single-partition readers.

    No trainer/evaluator-side object ever receives both roots — the broker is an
    admin-side component that mints a reader bound to the authenticated caller's own
    partition and root only.
    """

    def __init__(self, *, train_root: Path, validation_root: Path) -> None:
        train = train_root.resolve()
        validation = validation_root.resolve()
        if train == validation:
            raise MatrixAccessError("train and validation roots must not be the same")
        if train.is_relative_to(validation) or validation.is_relative_to(train):
            raise MatrixAccessError("train and validation roots must not overlap")
        self._roots: dict[str, Path] = {"train": train, "validation": validation}

    def reader_for(self, conn: Connection) -> PartitionArtifactReader:
        """Mint a reader for the connection's authenticated partition, with ONLY that
        partition's root."""
        partition = _partition_for_connection(conn)
        return PartitionArtifactReader(partition, self._roots[partition])

    def root_for(self, partition: str) -> Path:
        if partition not in self._roots:
            raise MatrixAccessError(f"unknown partition {partition!r}")
        return self._roots[partition]


# --------------------------------------------------------------------------- #
# credential provisioning + verification
# --------------------------------------------------------------------------- #
def configure_partition_root(root: Path, *, group: str) -> None:
    """Apply the real partition credential to a root: chgrp to ``group``, setgid +
    owner-rwx + group-r-x, no other/world. Deployment/admin-side; requires membership in
    ``group`` (or privilege). Existing matrix files are set to owner-rw + group-r."""
    root.mkdir(parents=True, exist_ok=True)
    gid = grp.getgrnam(group).gr_gid
    os.chown(root, os.getuid(), gid)
    os.chmod(root, _DIR_MODE)
    for child in root.iterdir():
        if child.is_file():
            os.chown(child, os.getuid(), gid)
            os.chmod(child, _FILE_MODE)


@dataclass(frozen=True)
class CredentialStatus:
    """Outcome of an operational credential check. ``PASS`` only on real isolation."""

    status: Literal["PASS", "HOLD"]
    checks: dict[str, bool]
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def verify_partition_capability(*, train_root: Path, validation_root: Path) -> dict[str, bool]:
    """Structural/policy checks that run ANYWHERE (no OS identities required)."""
    train = train_root.resolve()
    validation = validation_root.resolve()
    checks: dict[str, bool] = {}
    checks["roots_distinct"] = train != validation
    checks["roots_disjoint"] = not train.is_relative_to(validation) and (
        not validation.is_relative_to(train)
    )
    # no test partition exists in the role/view maps or as a sibling retrieval root.
    checks["no_test_partition_configured"] = (
        "test" not in PARTITION_ROLES.values()
        and "test" not in PARTITION_VIEWS
        and not (train.parent / "test").exists()
        and not (validation.parent / "test").exists()
    )
    checks["partition_map_is_train_validation_only"] = set(PARTITION_ROLES.values()) == {
        "train",
        "validation",
    }
    checks["view_map_is_train_validation_only"] = set(PARTITION_VIEWS) == {"train", "validation"}
    return checks


def _root_credential_checks(partition: str, root: Path, gid: int) -> tuple[dict[str, bool], str]:
    checks: dict[str, bool] = {}
    st = root.stat()
    mode = stat.S_IMODE(st.st_mode)
    checks[f"{partition}_root_owned_by_writer"] = st.st_uid == os.getuid()
    checks[f"{partition}_root_setgid"] = bool(mode & stat.S_ISGID)
    checks[f"{partition}_root_group_read"] = bool(mode & stat.S_IRGRP)
    checks[f"{partition}_root_no_other"] = (mode & 0o007) == 0
    checks[f"{partition}_root_group_matches"] = st.st_gid == gid
    group_name = grp.getgrgid(st.st_gid).gr_name
    file_ok = True
    for child in root.iterdir():
        if child.is_file():
            cst = child.stat()
            cmode = stat.S_IMODE(cst.st_mode)
            if cst.st_gid != gid or (cmode & stat.S_IWGRP) or (cmode & 0o007):
                file_ok = False
                break
    checks[f"{partition}_files_group_owned_not_writable"] = file_ok
    return checks, group_name


def verify_operational_credentials(
    *,
    train_root: Path,
    validation_root: Path,
    train_group: str | None = None,
    validation_group: str | None = None,
) -> CredentialStatus:
    """Real OS-credential verification. Returns ``PASS`` only when the two roots carry
    DISTINCT partition groups that actually enforce owner-write / partition-read; returns
    ``HOLD`` otherwise (including same-UID owner-only roots, which are NOT isolation)."""
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    train = train_root.resolve()
    validation = validation_root.resolve()

    train_gid = train.stat().st_gid
    validation_gid = validation.stat().st_gid
    distinct = train_gid != validation_gid
    checks["partition_groups_distinct"] = distinct
    if not distinct:
        reasons.append(
            "train and validation roots share the same OS group — same-UID owner-only "
            "directories are not partition isolation"
        )

    if train_group is not None:
        checks["train_group_expected"] = train_gid == grp.getgrnam(train_group).gr_gid
    if validation_group is not None:
        checks["validation_group_expected"] = (
            validation_gid == grp.getgrnam(validation_group).gr_gid
        )

    tchecks, tgroup = _root_credential_checks("train", train, train_gid)
    vchecks, vgroup = _root_credential_checks("validation", validation, validation_gid)
    checks.update(tchecks)
    checks.update(vchecks)
    checks["group_names_distinct"] = tgroup != vgroup

    status: Literal["PASS", "HOLD"] = "PASS" if all(checks.values()) else "HOLD"
    if status == "HOLD" and not reasons:
        reasons.append(
            "partition credential invariants not satisfied: "
            + ", ".join(sorted(k for k, v in checks.items() if not v))
        )
    return CredentialStatus(status=status, checks=checks, reasons=tuple(reasons))
