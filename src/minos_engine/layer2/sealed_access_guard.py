"""A process-level guard that makes a sealed-partition read impossible, not merely unintended.

The previous L2-H qualification claimed `sealed_partitions_never_enumerated` while the loader read,
parsed and traversed the whole 75-member profile snapshot — deriving its "skipped" counts *by*
enumerating the very records it claimed not to touch. The claim was self-refuting, and a count of
skipped members proves nothing about non-access.

So isolation is no longer asserted. It is enforced here and *observed*: every open of a document
that carries sealed member identities raises, and the number of attempts is counted. A
qualification then derives its isolation checks from the counter rather than writing
`test_accessed: false` as a constant, which is the difference between evidence and a claim.

The guard is deliberately blunt. It patches :func:`io.open` (which ``open``, ``Path.read_bytes``
and ``json.load`` all reach) for the duration of a ``with`` block and refuses any path whose
resolved name is a known sealed-identity-bearing authority. Blunt is the point: a subtle guard
that only covers the paths someone remembered is the same mistake in a different shape.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "SEALED_IDENTITY_AUTHORITIES",
    "SealedAccessError",
    "SealedAccessObservation",
    "sealed_access_guard",
]

#: Documents that contain TEST and/or VALIDATION member identities. Reading any of them -- even to
#: filter it down to TRAIN -- enumerates sealed identities, so the whole file is off limits.
SEALED_IDENTITY_AUTHORITIES: Final[tuple[str, ...]] = (
    "profile_snapshot_epoch1_members.json",
    "profile_snapshot_epoch1_artifact_inventory.json",
    "profile_snapshot_epoch1_selections.json",
    "layer2_dataset_split_v1.json",
    "layer2_dataset_split_v2_epoch1.json",
    "layer2_local_input_inventory_v1.json",
)


class SealedAccessError(MinosEngineError):
    """A sealed-partition authority was opened. TEST is sealed until L2-I, enumeration included."""


class SealedAccessObservation:
    """What the guard actually saw. The qualification derives its isolation checks from this."""

    __slots__ = ("attempted_paths", "open_attempts")

    def __init__(self) -> None:
        self.open_attempts = 0
        #: base names only. The full path is operational, and a sealed identity must not be
        #: recorded even in a failure report.
        self.attempted_paths: list[str] = []

    def content(self) -> dict[str, Any]:
        return {
            "forbidden_sealed_path_open_attempts": self.open_attempts,
            "attempted_sealed_authorities": sorted(set(self.attempted_paths)),
        }


def _is_sealed(path: Any) -> str | None:
    try:
        name = Path(path).name
    except TypeError:
        return None
    return name if name in SEALED_IDENTITY_AUTHORITIES else None


@contextmanager
def sealed_access_guard() -> Any:
    """Refuse and count every open of a sealed-identity authority inside this block."""
    observation = SealedAccessObservation()
    original = io.open

    def guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
        sealed = _is_sealed(file)
        if sealed is not None:
            observation.open_attempts += 1
            observation.attempted_paths.append(sealed)
            raise SealedAccessError(
                f"{sealed} carries sealed member identities; TEST is sealed until L2-I, "
                "including identity enumeration, and VALIDATION is not authorised here"
            )
        return original(file, *args, **kwargs)

    io.open = guarded
    try:
        yield observation
    finally:
        io.open = original
