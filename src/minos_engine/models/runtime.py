"""``l2g-training-runtime-v1`` — the EXACT interpreter and libraries the campaign may fit under.

``scikit-learn>=1.5,<2`` is a development dependency range. It is not a scientific runtime: a
model fitted under 1.5.0 and one fitted under 1.9.0 are not the same experiment, and a range
silently permits both. The campaign therefore binds exact versions and the trainer verifies them
before the first fit rather than recording them afterwards.
"""

from __future__ import annotations

import platform
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "TRAINING_RUNTIME_DOMAIN",
    "TRAINING_RUNTIME_SCHEMA",
    "TrainingRuntimeError",
    "compute_training_runtime_hash",
    "observe_training_runtime",
    "training_runtime_content",
    "verify_training_runtime",
]

TRAINING_RUNTIME_SCHEMA: Final = "l2g-training-runtime-v1"
TRAINING_RUNTIME_DOMAIN: Final = "minos:l2g-training-runtime:v1\n"

#: exact, not a range. These are the versions the sample-weight behaviour was verified under.
PINNED_RUNTIME: Final[dict[str, str]] = {
    "joblib": "1.6.0",
    "numpy": "2.5.2",
    "python": "3.12.3",
    "scikit-learn": "1.9.0",
    "scipy": "1.18.1",
}

#: the interpreter minor series is scientific; the patch level is recorded but not fatal.
_PYTHON_SERIES: Final = "3.12"


class TrainingRuntimeError(MinosEngineError):
    """The interpreter or libraries are not the frozen training runtime."""


def training_runtime_content() -> dict[str, Any]:
    return {
        "packages": dict(sorted(PINNED_RUNTIME.items())),
        "python_series": _PYTHON_SERIES,
        "schema_version": TRAINING_RUNTIME_SCHEMA,
        "threading": "SINGLE_THREADED_DETERMINISTIC",
    }


def compute_training_runtime_hash() -> str:
    return sha256_hex(
        TRAINING_RUNTIME_DOMAIN.encode("utf-8") + canonical_json_bytes(training_runtime_content())
    )


def observe_training_runtime() -> dict[str, str]:
    """What is ACTUALLY importable here. Never taken from a caller."""
    import joblib
    import numpy
    import scipy
    import sklearn

    return {
        "joblib": str(joblib.__version__),
        "numpy": str(numpy.__version__),
        "python": platform.python_version(),
        "scikit-learn": str(sklearn.__version__),
        "scipy": str(scipy.__version__),
    }


def verify_training_runtime() -> dict[str, Any]:
    """Refuse to fit under anything but the frozen runtime.

    Raises rather than warning: a campaign that quietly ran under a different solver version
    produces numbers nobody can reproduce, and the whole point of the freeze is reproducibility.
    """
    observed = observe_training_runtime()
    mismatched = {
        name: {"expected": expected, "observed": observed.get(name)}
        for name, expected in PINNED_RUNTIME.items()
        if observed.get(name) != expected
    }
    if mismatched:
        raise TrainingRuntimeError(
            f"the training runtime is not the frozen {TRAINING_RUNTIME_SCHEMA}: {mismatched}"
        )
    return {
        "observed": observed,
        "runtime_hash": compute_training_runtime_hash(),
        "schema_version": TRAINING_RUNTIME_SCHEMA,
    }
