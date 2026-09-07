"""A process-level guard that makes a sealed-partition read impossible, not merely unintended.

The previous L2-H qualification claimed `sealed_partitions_never_enumerated` while the loader read,
parsed and traversed the whole 75-member profile snapshot — deriving its "skipped" counts *by*
enumerating the very records it claimed not to touch. The claim was self-refuting, and a count of
skipped members proves nothing about non-access.

So isolation is no longer asserted. It is enforced here and *observed*: every open of a document
that carries sealed member identities raises, and the number of attempts is counted. A
qualification then derives its isolation checks from the counter rather than writing
`test_accessed: false` as a constant, which is the difference between evidence and a claim.

The guard is deliberately blunt. Blunt is the point: a subtle guard covering only the paths
someone remembered is the same mistake in a different shape.

It patches **every** file-open entry point a read can take rather than assuming one covers the
others: ``builtins.open``, ``io.open`` and ``os.open``. Rebinding ``io.open`` alone does not
intercept an existing ``builtins.open`` binding -- they are separate module attributes, and code
that captured ``builtins.open`` earlier would sail past. ``os.open`` is patched too, because a
low-level read is still a read; directory descriptors used for ``fsync`` are unaffected, since a
directory is never one of the guarded documents.

Attempts are also **categorised** by which seal they would have breached, so a qualification can
derive ``test_accessed`` and ``validation_read`` from counters instead of writing them as
constants.
"""

from __future__ import annotations

import builtins
import io
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "GUARDED_APIS",
    "SEALED_IDENTITY_AUTHORITIES",
    "TEST_SEALED_AUTHORITIES",
    "VALIDATION_SEALED_AUTHORITIES",
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

#: Every guarded document carries BOTH sealed partitions' identities: the snapshot membership,
#: its inventory and selections all span train/validation/test, and the split manifests are what
#: assign the partitions in the first place. Categorising by seal is therefore not a partition of
#: the file list -- each file counts against both -- which is the honest reading.
TEST_SEALED_AUTHORITIES: Final[tuple[str, ...]] = SEALED_IDENTITY_AUTHORITIES
VALIDATION_SEALED_AUTHORITIES: Final[tuple[str, ...]] = SEALED_IDENTITY_AUTHORITIES


class SealedAccessError(MinosEngineError):
    """A sealed-partition authority was opened. TEST is sealed until L2-I, enumeration included."""


class SealedAccessObservation:
    """What the guard actually saw. The qualification derives its isolation checks from this."""

    __slots__ = ("attempted_paths", "open_attempts", "test_attempts", "validation_attempts")

    def __init__(self) -> None:
        self.open_attempts = 0
        self.test_attempts = 0
        self.validation_attempts = 0
        #: base names only. The full path is operational, and a sealed identity must not be
        #: recorded even in a failure report.
        self.attempted_paths: list[str] = []

    def record(self, name: str) -> None:
        self.open_attempts += 1
        self.attempted_paths.append(name)
        if name in TEST_SEALED_AUTHORITIES:
            self.test_attempts += 1
        if name in VALIDATION_SEALED_AUTHORITIES:
            self.validation_attempts += 1

    def content(self) -> dict[str, Any]:
        return {
            "forbidden_sealed_path_open_attempts": self.open_attempts,
            "attempted_sealed_authorities": sorted(set(self.attempted_paths)),
            "test_identity_authority_open_attempts": self.test_attempts,
            "validation_identity_authority_open_attempts": self.validation_attempts,
            "guarded_file_apis": list(GUARDED_APIS),
        }


def _is_sealed(path: Any) -> str | None:
    try:
        name = Path(path).name
    except TypeError:
        return None
    return name if name in SEALED_IDENTITY_AUTHORITIES else None


#: Every file-open entry point a read can take. Named so the report can state what was covered.
GUARDED_APIS: Final[tuple[str, ...]] = ("builtins.open", "io.open", "os.open")


@contextmanager
def sealed_access_guard() -> Any:
    """Refuse and count every open of a sealed-identity authority inside this block."""
    observation = SealedAccessObservation()
    original_io = io.open
    original_builtins = builtins.open
    original_os = os.open

    def _refuse(name: str) -> None:
        observation.record(name)
        raise SealedAccessError(
            f"{name} carries sealed member identities; TEST is sealed until L2-I, "
            "including identity enumeration, and VALIDATION is not authorised here"
        )

    def guarded_io(file: Any, *args: Any, **kwargs: Any) -> Any:
        sealed = _is_sealed(file)
        if sealed is not None:
            _refuse(sealed)
        return original_io(file, *args, **kwargs)

    def guarded_builtins(file: Any, *args: Any, **kwargs: Any) -> Any:
        sealed = _is_sealed(file)
        if sealed is not None:
            _refuse(sealed)
        return original_builtins(file, *args, **kwargs)

    def guarded_os(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> Any:
        sealed = _is_sealed(path)
        if sealed is not None:
            _refuse(sealed)
        return original_os(path, flags, mode, **kwargs)

    io.open = guarded_io
    builtins.open = guarded_builtins
    os.open = guarded_os
    try:
        yield observation
    finally:
        io.open = original_io
        builtins.open = original_builtins
        os.open = original_os
