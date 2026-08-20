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

:class:`PartitionArtifactPublisher` is the MANDATORY owner-side write boundary: it holds
BOTH frozen partition credentials (root + gid) and validates them at construction — each
root exists and is not a symlink, has EXACT mode ``0o2750`` and writer ownership, the two
partition gids are DISTINCT, and the roots are disjoint. Missing production roots are
never auto-created. The production builder consumes a publisher, never a bare
``artifact_root``.

:func:`verify_operational_credentials` takes EXPLICIT ``trainer_identity`` and
``evaluator_identity`` OS usernames and returns ``PASS`` only when it can PROVE
separation: distinct partition groups; trainer ∈ train group ∉ validation group (and the
mirror for evaluator); every artifact is a regular non-symlink file with EXACT mode
``0o640``, the partition gid and the writer uid; both roots contain at least one verified
artifact; and a real cross-identity access test — the PRIVILEGED PARENT resolves the
exact own/foreign artifact paths, then a forked child drops supplementary groups, gid and
uid and directly ``os.open``/reads that exact path (a ``PermissionError`` from directory
traversal or opening is ``DENIED``); own must be ``OK`` and foreign ``DENIED`` for both.
When impersonation is unavailable — not privileged, identities absent, or a root has no
artifact (as in CI) — it returns ``HOLD``, never a false ``PASS``. Two groups of the same
current user is NOT separation. :func:`verify_partition_capability` is the
structural/policy check that runs anywhere. :func:`provision_partition_root` applies a
partition's frozen group + setgid before building. Deployment secrets, real URIs, and
payloads never live in Git.
"""

from __future__ import annotations

import grp
import hashlib
import os
import pwd
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
    "PartitionArtifactPublisher",
    "provision_partition_root",
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


def _validate_partition_root(partition: str, root: Path) -> int:
    """Validate a provisioned partition root and return its gid. The root must be an
    existing, non-symlink directory owned by the writer uid with EXACT mode 0o2750."""
    if root.is_symlink():
        raise MatrixAccessError(f"{partition} root {root} is a symlink")
    if not root.is_dir():
        raise MatrixAccessError(f"{partition} root {root} does not exist or is not a directory")
    st = root.stat()
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid != os.getuid():
        raise MatrixAccessError(f"{partition} root {root} is not owned by the writer uid")
    if mode != _DIR_MODE:
        raise MatrixAccessError(
            f"{partition} root {root} has mode {oct(mode)}, expected {oct(_DIR_MODE)}"
        )
    return st.st_gid


class PartitionArtifactPublisher:
    """Owner-side publisher: the MANDATORY production write boundary. Holds both frozen
    partition credentials (root + gid) and validates them at construction, so a bare
    caller-supplied ``artifact_root`` can never reach the write path.

    Construction requires: each root exists and is not a symlink; exact mode ``0o2750``;
    writer ownership; the two partition gids are DISTINCT; and the roots are disjoint.
    Missing production roots are NOT created automatically — provisioning
    (:func:`provision_partition_root`) is a deliberate deployment step.
    """

    def __init__(self, *, train_root: Path, validation_root: Path) -> None:
        # symlink + existence + mode + ownership are validated on the GIVEN paths first
        # (a symlinked root must be rejected before any resolution hides it).
        train_gid = _validate_partition_root("train", train_root)
        validation_gid = _validate_partition_root("validation", validation_root)
        train = train_root.resolve()
        validation = validation_root.resolve()
        if train == validation:
            raise MatrixAccessError("train and validation roots must not be the same")
        if train.is_relative_to(validation) or validation.is_relative_to(train):
            raise MatrixAccessError("train and validation roots must not overlap")
        if train_gid == validation_gid:
            raise MatrixAccessError(
                "train and validation roots must carry DISTINCT partition gids "
                "(same-UID same-group roots are not partition isolation)"
            )
        self._roots: dict[str, Path] = {"train": train, "validation": validation}
        self._gids: dict[str, int] = {"train": train_gid, "validation": validation_gid}

    def partition_root(self, partition: str) -> Path:
        if partition not in self._roots:
            raise MatrixAccessError(f"unknown partition {partition!r}")
        return self._roots[partition]

    def partition_gid(self, partition: str) -> int:
        if partition not in self._gids:
            raise MatrixAccessError(f"unknown partition {partition!r}")
        return self._gids[partition]


# --------------------------------------------------------------------------- #
# credential provisioning + verification
# --------------------------------------------------------------------------- #
def provision_partition_root(root: Path, *, group: str) -> None:
    """Provision a partition root with its FROZEN group BEFORE any artifact is built:
    chgrp to ``group``, setgid + owner-rwx + group-r-x, no other/world (``0o2750``).
    Deployment/admin-side; requires membership in ``group`` (or privilege). Existing
    matrix files are set to owner-rw + group-r (``0o0640``). New artifacts receive the
    partition credential per-inode at publication (see ``matrix_parquet``), so correct
    permissions never depend on this being re-run after artifacts exist."""
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
    """Outcome of an operational credential check. ``PASS`` only on PROVEN isolation."""

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


def _artifact_mode_checks(partition: str, root: Path, gid: int) -> dict[str, bool]:
    """Root has EXACT mode 0o2750; every matrix artifact inode is a regular non-symlink
    file with EXACT mode 0o640, the partition gid, and the writer uid."""
    checks: dict[str, bool] = {}
    st = root.stat()
    checks[f"{partition}_root_owned_by_writer"] = st.st_uid == os.getuid()
    checks[f"{partition}_root_mode_exactly_02750"] = stat.S_IMODE(st.st_mode) == _DIR_MODE
    checks[f"{partition}_root_group_matches_gid"] = st.st_gid == gid
    files = [c for c in root.iterdir() if c.name.endswith(".parquet")]
    all_ok = True
    for child in files:
        if child.is_symlink():
            all_ok = False
            break
        cst = child.stat()
        if (
            not stat.S_ISREG(cst.st_mode)
            or stat.S_IMODE(cst.st_mode) != _FILE_MODE
            or cst.st_gid != gid
            or cst.st_uid != os.getuid()
        ):
            all_ok = False
            break
    checks[f"{partition}_artifacts_exact_mode_0640"] = all_ok
    return checks


def _first_artifact(root: Path) -> Path | None:
    for child in sorted(root.glob("*.parquet")):
        if child.is_file() and not child.is_symlink():
            return child
    return None


def _identity_group_memberships(username: str) -> set[int] | None:
    """The set of gids ``username`` belongs to, or None if the identity does not exist."""
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        return None
    return set(os.getgrouplist(username, entry.pw_gid))


def verify_operational_credentials(
    *,
    train_root: Path,
    validation_root: Path,
    trainer_identity: str,
    evaluator_identity: str,
) -> CredentialStatus:
    """Prove REAL partition credential separation between two explicit OS identities.

    ``PASS`` requires ALL of: distinct partition groups; ``trainer_identity`` is a member
    of the train group and NOT the validation group; ``evaluator_identity`` is a member of
    the validation group and NOT the train group; every artifact carries the partition gid,
    ``S_IRGRP``, no group-write and no other bits; and a PROVEN cross-identity access test
    (each identity can open its own artifact and receives ``PermissionError`` for the
    other partition). If real identity impersonation is unavailable (e.g. not privileged,
    or the identities do not exist), the result is ``HOLD`` — never a false ``PASS``.
    Same-UID owner-only directories are explicitly not isolation.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    train = train_root.resolve()
    validation = validation_root.resolve()

    train_gid = train.stat().st_gid
    validation_gid = validation.stat().st_gid
    checks["partition_groups_distinct"] = train_gid != validation_gid
    if train_gid == validation_gid:
        reasons.append(
            "train and validation roots share the same OS group — same-UID owner-only "
            "directories are not partition isolation"
        )

    checks.update(_artifact_mode_checks("train", train, train_gid))
    checks.update(_artifact_mode_checks("validation", validation, validation_gid))

    # both roots must contain at least one verified artifact to run the access test on.
    train_artifact = _first_artifact(train)
    validation_artifact = _first_artifact(validation)
    checks["train_root_has_artifact"] = train_artifact is not None
    checks["validation_root_has_artifact"] = validation_artifact is not None

    checks["identities_distinct"] = trainer_identity != evaluator_identity
    trainer_groups = _identity_group_memberships(trainer_identity)
    evaluator_groups = _identity_group_memberships(evaluator_identity)
    checks["trainer_identity_exists"] = trainer_groups is not None
    checks["evaluator_identity_exists"] = evaluator_groups is not None
    if trainer_groups is not None:
        checks["trainer_in_train_group"] = train_gid in trainer_groups
        checks["trainer_not_in_validation_group"] = validation_gid not in trainer_groups
    if evaluator_groups is not None:
        checks["evaluator_in_validation_group"] = validation_gid in evaluator_groups
        checks["evaluator_not_in_train_group"] = train_gid not in evaluator_groups

    # PROVEN cross-identity access requires privilege to impersonate; without it, HOLD.
    can_impersonate = (
        os.geteuid() == 0
        and trainer_groups is not None
        and evaluator_groups is not None
        and train_artifact is not None
        and validation_artifact is not None
    )
    if not can_impersonate:
        checks["cross_identity_access_proven"] = False
        reasons.append(
            "real identity impersonation unavailable (not privileged, identities absent, "
            "or a partition root has no artifact) — HOLD, never PASS"
        )
    else:  # pragma: no cover - requires root + provisioned trainer/evaluator users
        assert train_artifact is not None and validation_artifact is not None
        proven = _prove_cross_identity_denial(
            trainer_identity, evaluator_identity, train_artifact, validation_artifact
        )
        checks["cross_identity_access_proven"] = proven
        if not proven:
            reasons.append("cross-identity access test did not prove partition denial")

    status: Literal["PASS", "HOLD"] = "PASS" if all(checks.values()) else "HOLD"
    if status == "HOLD" and not reasons:
        reasons.append(
            "partition credential invariants not satisfied: "
            + ", ".join(sorted(k for k, v in checks.items() if not v))
        )
    return CredentialStatus(status=status, checks=checks, reasons=tuple(reasons))


def _open_exact_path_as(username: str, path: Path) -> str:  # pragma: no cover - root-only
    """In a forked child that has DROPPED to ``username`` (supplementary groups, gid, uid),
    directly ``os.open``/read the EXACT ``path`` resolved by the privileged parent. A
    ``PermissionError`` from directory traversal or file opening is ``DENIED``."""
    entry = pwd.getpwnam(username)
    os.setgroups(os.getgrouplist(username, entry.pw_gid))
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    try:
        fd = os.open(str(path), os.O_RDONLY)  # traversal OR open may raise PermissionError
        try:
            os.read(fd, 1)
        finally:
            os.close(fd)
        return "OK"
    except PermissionError:
        return "DENIED"


def _prove_cross_identity_denial(
    trainer: str, evaluator: str, train_artifact: Path, validation_artifact: Path
) -> bool:  # pragma: no cover - requires root + provisioned users
    """The privileged parent resolved the EXACT own/foreign artifact paths; fork per
    (identity, exact path) and require own=OK, foreign=DENIED for both identities."""

    def run(username: str, path: Path) -> str:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            result = "ERR"
            try:
                result = _open_exact_path_as(username, path)
            finally:
                os.write(w, result.encode())
                os._exit(0)
        os.close(w)
        out = os.read(r, 16).decode()
        os.close(r)
        os.waitpid(pid, 0)
        return out

    return (
        run(trainer, train_artifact) == "OK"
        and run(trainer, validation_artifact) == "DENIED"
        and run(evaluator, validation_artifact) == "OK"
        and run(evaluator, train_artifact) == "DENIED"
    )
