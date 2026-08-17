"""Build a ReleaseManifest from the engine state and a runtime snapshot.

The runtime identities (upstream commit, scorer hash, parameter-space hash) are
sourced from a :class:`RoundProtocolSnapshot`; you cannot build a release
manifest without them, which is the correct fail-closed behavior. ``git_sha``
must be resolvable — an empty git identity fails closed.
"""

from __future__ import annotations

from minos_engine import __version__
from minos_engine.callers.gatk.parameter_registry import REGISTRY, GatkParameterRegistry
from minos_engine.common.errors import ManifestError
from minos_engine.common.hashing import canonical_hash
from minos_engine.common.versions import engine_git_sha
from minos_engine.protocol.contracts import RoundProtocolSnapshot
from minos_engine.schema_registry import load_schema
from minos_engine.settings import Settings

from .release import ReleaseManifest

__all__ = ["protocol_contract_hash", "engine_config_hash", "build_release_manifest"]

_PROTOCOL_SCHEMAS = (
    "round-protocol-snapshot-v1",
    "round-context-v1",
    "artifact-identity-v1",
    "parameter-space-snapshot-v1",
)


def protocol_contract_hash() -> str:
    """Stable hash over the protocol/intake contract schemas."""
    payload = {name: load_schema(name) for name in _PROTOCOL_SCHEMAS}
    return canonical_hash(payload)


def engine_config_hash(settings: Settings) -> str:
    return canonical_hash(settings.model_dump(mode="json"))


def build_release_manifest(
    snapshot: RoundProtocolSnapshot,
    *,
    created_at: str,
    settings: Settings | None = None,
    registry: GatkParameterRegistry = REGISTRY,
    git_sha: str | None = None,
) -> ReleaseManifest:
    resolved_git = git_sha if git_sha is not None else engine_git_sha()
    if not resolved_git or not resolved_git.strip():
        raise ManifestError("git_sha is unavailable; a release manifest requires it (fail closed)")

    cfg = settings or Settings.load()
    return ReleaseManifest(
        engine_version=__version__,
        git_sha=resolved_git.strip(),
        engine_config_hash=engine_config_hash(cfg),
        protocol_contract_hash=protocol_contract_hash(),
        gatk_registry_hash=registry.registry_hash(),
        minos_upstream_commit=snapshot.minos_upstream_commit,
        scorer_hash=snapshot.scorer_hash,
        parameter_space_hash=snapshot.parameter_space_hash,
        created_at=created_at,
    )
