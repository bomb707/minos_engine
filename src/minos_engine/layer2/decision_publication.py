"""Immutable, content-addressed publication of safe-controller decision manifests.

**Why this exists rather than a database write.** The v2 build specification's live decision
algorithm requires `persist decision + all candidate reasons in one transaction` and returns
`the exact canonical CONFIG bytes referenced by persisted hash` — persistence precedes the
return. That requirement binds an *activated* controller. This one is not activated:
`Layer2Service.select_config` still raises, so there is no live round, no caller receiving a
config, and nothing to lose.

What blocks the database route today is structural, not preference. `runtime.decisions` is a
strict subset of the table the specification describes — no `mode`, no `fallback_reason`, no
`controller_version`, no `parameter_space_hash`, no `decision_manifest`, no `decided_at`, and
`profile_id` nullable — and its `config_id`/`profile_id` foreign keys point at tables that hold no
rows. Adding the missing columns needs a migration, and the repository has exactly one linear
alembic chain with no branch labels: the operational store sits at `0005_l2e_feature_view` while
the chain runs to `0026`, every revision from `0006` on being an L2-F2 scientific-campaign
migration. A new controller revision could only be reached by advancing the operational database
through twenty-one unrelated scientific migrations. That is explicitly not permitted, and
improvising a branch-labelled chain to dodge it would be a far larger architectural change than
this task authorises.

So the disposition recorded here is **deferred, not waived**:
`docs/layer2/L2H_SAFE_CONTROLLER.md` §10 check 21 accepts "an explicit decision that decisions are
not persisted" as a closure, and this is that decision, scoped to the pre-activation stage.
Database persistence remains a prerequisite for activating the public service, and the migration
topology is the impediment that must be resolved first. "The table is inconvenient" is not the
reason and would not be a good one.

Meanwhile decisions are published the way this engine publishes all its other evidence: canonical
bytes under a content-addressed name, written atomically, fsynced, read back and verified before
the call returns. Idempotency is a property of the naming rather than a protocol — the same
semantic decision has the same identity and therefore the same path and the same bytes, so a retry
converges instead of conflicting, and two different decisions can never contend for one name.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError

__all__ = [
    "DECISION_PUBLICATION_DISPOSITION",
    "DECISION_RECORD_SCHEMA",
    "DecisionPublicationError",
    "PublishedDecision",
    "decide_and_publish",
    "publish_safe_decision",
    "read_published_decision",
]

DECISION_RECORD_SCHEMA: Final = "l2h-safe-decision-record-v1"

#: The exact disposition, so no reader has to infer it from behaviour.
DECISION_PUBLICATION_DISPOSITION: Final = "FILE_PUBLISHED_DB_PERSISTENCE_DEFERRED_TO_ACTIVATION"

_DIRECTORY_MODE: Final = 0o750
_FILE_MODE: Final = 0o640


class DecisionPublicationError(MinosEngineError):
    """A decision could not be published immutably. The decision must not be reported as made."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecisionPublicationError(message)


class PublishedDecision:
    """A decision manifest that is durably on disk and has been read back."""

    __slots__ = ("bytes_written", "identity", "path", "reused", "sha256")

    def __init__(
        self, *, identity: str, path: Path, sha256: str, bytes_written: int, reused: bool
    ) -> None:
        self.identity = identity
        self.path = path
        self.sha256 = sha256
        self.bytes_written = bytes_written
        #: True when an identical record was already published: a converged retry, not a failure.
        self.reused = reused


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_safe_decision(
    *, manifest: dict[str, Any], identity: str, output_root: Any
) -> PublishedDecision:
    """Publish one decision manifest under its OWN identity. Atomic, no-clobber, read back.

    Two defects are closed here.

    First, the identity is now *derived* rather than accepted: a caller cannot publish arbitrary
    canonical bytes under an unrelated 64-hex name, because the manifest must hash to the name it
    is filed under. A record whose name does not describe its contents is worse than no record.

    Second, the old check-then-``os.replace`` sequence was overwrite-capable: two writers could
    both see the target absent and both rename onto it, and the loser's bytes would vanish
    silently. Creation is now no-clobber (``O_CREAT | O_EXCL`` via ``os.link``), which is atomic
    across processes on a POSIX filesystem, so exactly one writer creates the immutable record and
    every other writer converges to it -- or fails closed if its bytes differ. A process-local
    lock would not do: nothing says one process publishes.
    """
    from minos_engine.layer2.safe_controller import safe_decision_manifest_identity

    _require(
        isinstance(manifest, dict) and bool(manifest),
        "a decision manifest must be a non-empty document",
    )
    _require(
        len(identity) == 64 and all(c in "0123456789abcdef" for c in identity),
        f"{identity!r} is not a decision identity",
    )
    derived = safe_decision_manifest_identity(manifest)
    _require(
        derived == identity,
        f"the manifest hashes to {derived}, so it may not be published as {identity}",
    )
    payload = canonical_json_bytes(manifest)
    expected_sha = hashlib.sha256(payload).hexdigest()

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
    target = root / f"{identity}.json"

    def _converge() -> PublishedDecision:
        """An existing final record: identical bytes converge, different bytes fail closed."""
        _require(not target.is_symlink(), f"{target} is a symlink")
        existing = target.read_bytes()
        _require(
            existing == payload,
            f"{identity} is already published with different bytes; a decision identity may "
            "never name two decisions",
        )
        return PublishedDecision(
            identity=identity,
            path=target,
            sha256=expected_sha,
            bytes_written=len(existing),
            reused=True,
        )

    if target.exists():
        return _converge()

    handle, staged_name = tempfile.mkstemp(dir=root, prefix=f".{identity}.", suffix=".tmp")
    staged = Path(staged_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, _FILE_MODE)
        try:
            # NO-CLOBBER: link() fails if the target exists, atomically, across processes.
            os.link(staged, target)
        except FileExistsError:
            # another writer won the race; converge on the record that actually exists
            staged.unlink(missing_ok=True)
            return _converge()
        _fsync_directory(root)
    finally:
        staged.unlink(missing_ok=True)

    # read back from the FINAL path: the bytes on disk are the record, not the ones we meant
    written = target.read_bytes()
    _require(
        written == payload and hashlib.sha256(written).hexdigest() == expected_sha,
        f"{target} does not read back as the decision that was published",
    )
    return PublishedDecision(
        identity=identity,
        path=target,
        sha256=expected_sha,
        bytes_written=len(written),
        reused=False,
    )


def read_published_decision(*, identity: str, output_root: Any) -> dict[str, Any]:
    """Read a published decision back and require it to still be the one its name claims."""
    path = Path(output_root) / f"{identity}.json"
    _require(path.is_file(), f"no decision is published under {identity}")
    _require(not path.is_symlink(), f"{path} is a symlink")
    raw = path.read_bytes()
    manifest = json.loads(raw)
    _require(
        canonical_json_bytes(manifest) == raw,
        f"the published decision {identity} is not canonical bytes",
    )
    from minos_engine.layer2.safe_controller import safe_decision_manifest_identity

    _require(
        safe_decision_manifest_identity(manifest) == identity,
        f"the published decision at {path} does not hash to the identity that names it",
    )
    return dict(manifest)


def decide_and_publish(
    *, request: Any, authority: Any, ownership: Any, output_root: Any
) -> tuple[Any, PublishedDecision]:
    """Decide, publish, and only then return — the ordering the specification requires.

    The publication happens before this function returns, so no caller can observe a decision that
    is not already durable. That is the property `persist(...)` before `return` exists to give,
    obtained here against the filesystem rather than against a database table that cannot yet hold
    the fields a controller decision has.
    """
    from minos_engine.layer2.safe_controller import (
        safe_decision_manifest_content,
        select_safe_baseline,
    )

    result = select_safe_baseline(request=request, authority=authority, ownership=ownership)
    manifest = safe_decision_manifest_content(
        request=request, authority=authority, ownership=ownership
    )
    published = publish_safe_decision(
        manifest=manifest,
        identity=result.decision.decision_manifest_hash,
        output_root=output_root,
    )
    return result, published
