"""Content-addressed publication of the L2-F2 evaluation metrics document.

This deliberately owns no protocol of its own: it is a thin, typed adapter over the audited
protocol in ``minos_engine.storage.content_addressed_publisher`` — the same implementation the
F3-C1 config publisher and the L2-F1 F5 result publisher were audited under. A second, weaker
publication path is exactly the kind of divergence that turns an audited property into a claim.

Frozen contract
---------------
* root: an existing, non-symlink directory owned by the writer uid, mode EXACTLY ``0o2750``
  (never created or repaired here); its gid becomes the published file's gid.
* file: ``<root>/<sha256>.json``, a regular non-symlink file owned by the writer uid, carrying
  the root gid, mode EXACTLY ``0o640``, whose bytes hash to ``<sha256>``.
* publication is atomic, no-clobber and safe under concurrent identical publishers; a
  conflicting or symlinked object at the final path is refused, never overwritten.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from minos_engine.common.errors import MinosEngineError
from minos_engine.evaluation.contracts import EVALUATION_METRICS_MEDIA_TYPE
from minos_engine.storage.content_addressed_publisher import (
    ContentAddressedSpec,
    ContentAddressedStore,
    PublishedObject,
)

__all__ = [
    "ENV_EVALUATION_ARTIFACT_ROOT",
    "EVALUATION_ARTIFACT_FILE_MODE",
    "EVALUATION_ARTIFACT_ROOT_MODE",
    "EVALUATION_METRICS_EXTENSION",
    "EVALUATION_METRICS_PROVENANCE",
    "EvaluationArtifactPublisher",
    "EvaluationPublishError",
    "PublishedMetricsArtifact",
    "evaluation_artifact_root_from_env",
]

ENV_EVALUATION_ARTIFACT_ROOT = "MINOS_L2F2_EVALUATION_ARTIFACT_ROOT"

EVALUATION_ARTIFACT_ROOT_MODE = 0o2750
EVALUATION_ARTIFACT_FILE_MODE = 0o640

EVALUATION_METRICS_EXTENSION = ".json"

#: must equal the provenance migration 0010's registrar writes; the two are asserted equal.
EVALUATION_METRICS_PROVENANCE = "l2f2:evaluation-metrics"


class EvaluationPublishError(MinosEngineError):
    """The evaluation artifact root or a published metrics file is unsafe."""


_SPEC = ContentAddressedSpec(
    label="evaluation metrics artifact",
    root_mode=EVALUATION_ARTIFACT_ROOT_MODE,
    file_mode=EVALUATION_ARTIFACT_FILE_MODE,
    root_error=EvaluationPublishError,
    integrity_error=EvaluationPublishError,
)


@dataclass(frozen=True)
class PublishedMetricsArtifact:
    """One published metrics document, with everything registration needs."""

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


class EvaluationArtifactPublisher:
    """Publishes the canonical metrics document under its own content address."""

    def __init__(self, root: Path, *, expected_gid: int | None = None) -> None:
        root = Path(root)
        if not root.is_absolute():
            raise EvaluationPublishError(
                f"evaluation metrics artifact root {root} must be absolute"
            )
        self._store = ContentAddressedStore(root, _SPEC, expected_gid=expected_gid)

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def gid(self) -> int:
        return self._store.gid

    def content_uri(self, sha256: str) -> str:
        return self._store.uri_for(sha256, EVALUATION_METRICS_EXTENSION)

    def unpublish_if_created(self, artifact: PublishedMetricsArtifact) -> None:
        """Rollback cleanup for an inode THIS call created; never a reused/concurrent one.

        A content-addressed metrics document is deliberately NOT removed just because a later
        database step failed: another evaluation may legitimately share those exact bytes.
        """
        if artifact.created:
            self._store.unpublish_if_created(
                PublishedObject(
                    sha256=artifact.sha256,
                    path=artifact.path,
                    uri=artifact.uri,
                    size_bytes=artifact.size_bytes,
                    created=True,
                    dev=artifact.dev,
                    ino=artifact.ino,
                )
            )

    def publish(self, payload: bytes) -> PublishedMetricsArtifact:
        """Publish ``payload`` to ``<sha256>.json``; get-or-verify an identical existing file."""
        digest = hashlib.sha256(payload).hexdigest()
        published = self._store.publish(
            payload, sha256=digest, extension=EVALUATION_METRICS_EXTENSION
        )
        return PublishedMetricsArtifact(
            sha256=published.sha256,
            path=published.path,
            uri=published.uri,
            size_bytes=published.size_bytes,
            media_type=EVALUATION_METRICS_MEDIA_TYPE,
            provenance=EVALUATION_METRICS_PROVENANCE,
            created=published.created,
            dev=published.dev,
            ino=published.ino,
        )


def evaluation_artifact_root_from_env() -> Path:
    """Resolve the evaluation artifact root from the environment, validating it eagerly."""
    raw = os.environ.get(ENV_EVALUATION_ARTIFACT_ROOT)
    if raw is None or not raw.strip():
        raise EvaluationPublishError(
            f"{ENV_EVALUATION_ARTIFACT_ROOT} is not set; the provisioned evaluation artifact "
            f"root (mode {oct(EVALUATION_ARTIFACT_ROOT_MODE)}) must be configured explicitly"
        )
    root = Path(raw.strip())
    EvaluationArtifactPublisher(root)  # validates the frozen root contract, fails closed
    return root
