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

The atomic protocol itself now lives in
``minos_engine.storage.content_addressed_publisher`` so later stages reuse the audited
implementation instead of growing a second, weaker one. The behaviour here is unchanged:
same modes, same typed errors, same no-clobber and rollback semantics.

The two artifact kinds use DISTINCT extensions (``.vcf`` and ``.result.json``) so a VCF and a
result manifest can never collide on one content-addressed path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.content_addressed_publisher import (
    ContentAddressedSpec,
    ContentAddressedStore,
    PublishedObject,
)
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


_SPEC = ContentAddressedSpec(
    label="result artifact",
    root_mode=RESULT_ARTIFACT_ROOT_MODE,
    file_mode=RESULT_ARTIFACT_FILE_MODE,
    root_error=ResultArtifactRootError,
    integrity_error=ResultArtifactIntegrityError,
)


class ResultArtifactPublisher:
    """A provisioned content-addressed publisher for the two F5 artifact kinds."""

    def __init__(self, root: Path, *, expected_gid: int | None = None) -> None:
        self._store = ContentAddressedStore(root, _SPEC, expected_gid=expected_gid)

    @property
    def root(self) -> Path:
        return self._store.root

    @property
    def gid(self) -> int:
        return self._store.gid

    def content_uri(self, kind: str, sha256: str) -> str:
        extension, _media = self._kind(kind)
        return self._store.uri_for(sha256, extension)

    @staticmethod
    def _kind(kind: str) -> tuple[str, str]:
        try:
            return _KINDS[kind]
        except KeyError as exc:
            raise ResultArtifactIntegrityError(f"unknown result artifact kind {kind!r}") from exc

    def unpublish_if_created(self, artifact: PublishedResultArtifact) -> None:
        """Remove a file THIS call created (rollback cleanup); never a reused/concurrent one."""
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

    def publish(self, payload: bytes, *, kind: str, sha256: str) -> PublishedResultArtifact:
        """Publish ``payload`` (whose sha256 MUST equal ``sha256``) to its immutable
        content-addressed path with no-clobber semantics; get-or-verify an existing file."""
        extension, media_type = self._kind(kind)
        provenance = VCF_ARTIFACT_KIND if kind == "vcf" else RESULT_MANIFEST_ARTIFACT_KIND
        published = self._store.publish(payload, sha256=sha256, extension=extension)
        return PublishedResultArtifact(
            kind=kind,
            sha256=published.sha256,
            path=published.path,
            uri=published.uri,
            size_bytes=published.size_bytes,
            media_type=media_type,
            provenance=provenance,
            created=published.created,
            dev=published.dev,
            ino=published.ino,
        )


def result_artifact_root_from_env() -> Path:
    raw = os.environ.get(ENV_RESULT_ARTIFACT_ROOT)
    if raw is None or not raw.strip():
        raise ResultArtifactRootError(
            f"{ENV_RESULT_ARTIFACT_ROOT} is not set; the provisioned result-artifact root "
            f"(mode {oct(RESULT_ARTIFACT_ROOT_MODE)}) must be configured explicitly"
        )
    return Path(raw.strip())
