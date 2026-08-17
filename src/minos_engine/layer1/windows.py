"""Deterministic window partition (Layer 1 spec §3, §15).

The exact interval is partitioned into fixed ``primary_bp`` windows (the last may
be shorter). Windows are a pure function of the normalized region and window size,
so they are identical across runs and process restarts, never overlap, never
overflow the interval, and preserve a stable order by ``window_id``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Region

__all__ = ["WindowSpec", "generate_windows"]


@dataclass(frozen=True)
class WindowSpec:
    window_id: int
    contig: str
    start0: int
    end0: int

    @property
    def length_bp(self) -> int:
        return self.end0 - self.start0


def generate_windows(region: Region, primary_bp: int) -> tuple[WindowSpec, ...]:
    if primary_bp <= 0:
        raise ValueError("primary_bp must be positive")
    windows: list[WindowSpec] = []
    wid = 0
    pos = region.start0
    while pos < region.end0_exclusive:
        end = min(pos + primary_bp, region.end0_exclusive)
        windows.append(WindowSpec(window_id=wid, contig=region.contig, start0=pos, end0=end))
        pos = end
        wid += 1
    return tuple(windows)
