"""Enforce the SINGLE_THREADED_DETERMINISTIC claim the runtime hash already makes.

Setting ``OMP_NUM_THREADS`` in the environment is not enforcement: by the time this process has
imported numpy and scikit-learn, OpenMP and BLAS have already read their thread counts, and the
variable changes nothing. On this machine both report 16 threads at import.

``threadpoolctl`` re-limits the loaded libraries in place, which is why the fit runs inside a
context rather than after an ``os.environ`` assignment. The report is observed from the running
pools, not asserted.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "ThreadEnforcementError",
    "observe_thread_pools",
    "single_threaded",
    "verify_single_threaded",
]

_REQUIRED_APIS = ("blas", "openmp")


class ThreadEnforcementError(MinosEngineError):
    """The process is not running single-threaded where the runtime claims it is."""


def observe_thread_pools() -> list[dict[str, Any]]:
    """What the LOADED libraries actually report, right now."""
    from threadpoolctl import threadpool_info

    return [
        {
            "user_api": entry.get("user_api"),
            "internal_api": entry.get("internal_api"),
            "num_threads": entry.get("num_threads"),
            "prefix": entry.get("prefix"),
        }
        for entry in threadpool_info()
    ]


def verify_single_threaded() -> list[dict[str, Any]]:
    """Refuse if any loaded BLAS/OpenMP pool would run multi-threaded."""
    pools = observe_thread_pools()
    offending = [p for p in pools if p["user_api"] in _REQUIRED_APIS and p["num_threads"] != 1]
    if offending:
        raise ThreadEnforcementError(
            f"the frozen runtime is SINGLE_THREADED_DETERMINISTIC but these pools are not "
            f"limited: {offending}"
        )
    return pools


@contextmanager
def single_threaded() -> Iterator[list[dict[str, Any]]]:
    """Fit inside this. Yields the OBSERVED pool report taken while the limit is in force."""
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        yield verify_single_threaded()
