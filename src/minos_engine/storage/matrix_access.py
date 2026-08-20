"""Partition-scoped matrix-artifact retrieval boundary (E3 access ruling).

Database grants alone are insufficient if both roles can open the same file: this
boundary pairs the 0005 partition views with partition-scoped filesystem roots so the
caller's AUTHENTICATED database identity — ``current_user`` on the caller's own
connection, never a caller-provided role string — determines both which matrix rows
are visible (grant-enforced view) and which artifact root may be read.

Rules (all fail closed with :class:`MatrixAccessError`):
  * trainer resolves/reads ONLY train artifacts; evaluator ONLY validation artifacts;
  * no test root, test view, or test retrieval path exists;
  * the train and validation roots must be distinct and non-overlapping;
  * artifact paths are resolved with symlinks followed and must remain inside the
    caller's partition root — traversal, symlink escapes, wrong-partition paths, and
    cross-partition substitution are rejected;
  * the retrieved bytes must hash to the view-bound ``artifact_sha256``;
  * the local-filesystem deployment invariant (owner-only permissions per partition
    root, current-uid ownership) is operationally verifiable via
    :meth:`PartitionArtifactStore.verify_partition_isolation`.

Deployment secrets, real URIs, and payloads never live in Git — roots are supplied by
the deployment, and this module only ever returns bytes for the caller's own partition.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from minos_engine.common.errors import MatrixAccessError

__all__ = ["PARTITION_ROLES", "PARTITION_VIEWS", "PartitionArtifactStore"]

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


class PartitionArtifactStore:
    """Resolve and read matrix artifacts strictly inside the caller's partition."""

    def __init__(self, *, train_root: Path, validation_root: Path) -> None:
        train_resolved = train_root.resolve()
        validation_resolved = validation_root.resolve()
        if train_resolved == validation_resolved:
            raise MatrixAccessError("train and validation roots must not be the same")
        if train_resolved.is_relative_to(validation_resolved) or validation_resolved.is_relative_to(
            train_resolved
        ):
            raise MatrixAccessError("train and validation roots must not overlap")
        self._roots: dict[str, Path] = {
            "train": train_resolved,
            "validation": validation_resolved,
        }

    @staticmethod
    def partition_for_connection(conn: Connection) -> str:
        """Partition derived from the AUTHENTICATED identity (current_user)."""
        current_user = str(conn.execute(text("SELECT current_user")).scalar_one())
        partition = PARTITION_ROLES.get(current_user)
        if partition is None:
            raise MatrixAccessError(f"database identity {current_user!r} has no matrix partition")
        return partition

    def _confine(self, partition: str, uri: str) -> Path:
        root = self._roots[partition]
        candidate = Path(uri)
        if not candidate.is_absolute():
            raise MatrixAccessError("artifact uri must be an absolute path")
        try:
            resolved = candidate.resolve(strict=True)  # follows symlinks
        except OSError as exc:
            raise MatrixAccessError(f"artifact path cannot be resolved: {exc}") from exc
        if not resolved.is_relative_to(root):
            raise MatrixAccessError(
                "artifact path escapes the caller's partition root (traversal, symlink, "
                "or wrong-partition location)"
            )
        other = [r for p, r in self._roots.items() if p != partition]
        if any(resolved.is_relative_to(r) for r in other):  # pragma: no cover - disjoint
            raise MatrixAccessError("artifact path resolves into a foreign partition root")
        return resolved

    def fetch_matrix_payload(self, conn: Connection, matrix_hash: str) -> bytes:
        """Read the caller's own partition matrix artifact, verified end-to-end.

        The row is resolved through the caller's grant-enforced partition view ON THE
        CALLER'S OWN CONNECTION; the path is confined to the caller's partition root;
        the bytes must hash to the view-bound ``artifact_sha256``.
        """
        partition = self.partition_for_connection(conn)
        view = PARTITION_VIEWS[partition]
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
        if row["partition"] != partition:  # pragma: no cover - view is partition-fixed
            raise MatrixAccessError("view returned a foreign-partition row")
        path = self._confine(partition, str(row["artifact_uri"]))
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row["artifact_sha256"]:
            raise MatrixAccessError("artifact bytes do not hash to the view-bound artifact_sha256")
        return payload

    def verify_partition_isolation(self) -> dict[str, bool]:
        """Operational filesystem-deployment invariants (named checks, no repair)."""
        checks: dict[str, bool] = {}
        train_root = self._roots["train"]
        validation_root = self._roots["validation"]
        checks["roots_distinct_and_disjoint"] = (
            train_root != validation_root
            and not train_root.is_relative_to(validation_root)
            and not validation_root.is_relative_to(train_root)
        )
        for partition, root in self._roots.items():
            exists = root.is_dir()
            checks[f"{partition}_root_exists"] = exists
            if not exists:
                checks[f"{partition}_root_owner_only"] = False
                checks[f"{partition}_root_owned_by_runtime_uid"] = False
                continue
            mode = stat.S_IMODE(root.stat().st_mode)
            checks[f"{partition}_root_owner_only"] = (mode & 0o077) == 0
            checks[f"{partition}_root_owned_by_runtime_uid"] = root.stat().st_uid == os.getuid()
        return checks
