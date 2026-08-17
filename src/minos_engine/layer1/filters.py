"""One shared read-filter policy used by every applicable profiler (Layer 1 §8).

The policy classifies each observed alignment into exactly one bucket: included
(analysis-eligible) or excluded by a single, highest-priority reason. This makes
the accounting mutually exclusive so ``observed == included + Σ excluded``. No
feature module invents its own filtering — they all consume this classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from minos_engine.common.hashing import canonical_hash

__all__ = ["ReadLike", "ReadFilterPolicy", "ExclusionReason"]

# Fixed exclusion priority. The first matching reason wins so buckets never overlap.
EXCLUSION_ORDER = (
    "unmapped",
    "secondary",
    "supplementary",
    "duplicate",
    "qcfail",
    "below_mapq",
)


class ExclusionReason(str):
    pass


class ReadLike(Protocol):
    is_unmapped: bool
    is_secondary: bool
    is_supplementary: bool
    is_duplicate: bool
    is_qcfail: bool
    mapping_quality: int


@dataclass(frozen=True)
class ReadFilterPolicy:
    """Deterministic read-eligibility policy with a canonical identity hash."""

    min_mapping_quality: int = 0
    drop_unmapped: bool = True
    drop_secondary: bool = True
    drop_supplementary: bool = True
    drop_duplicate: bool = True
    drop_qcfail: bool = True

    def classify(self, read: ReadLike) -> str | None:
        """Return the single exclusion reason, or ``None`` if analysis-eligible."""
        if self.drop_unmapped and read.is_unmapped:
            return "unmapped"
        if self.drop_secondary and read.is_secondary:
            return "secondary"
        if self.drop_supplementary and read.is_supplementary:
            return "supplementary"
        if self.drop_duplicate and read.is_duplicate:
            return "duplicate"
        if self.drop_qcfail and read.is_qcfail:
            return "qcfail"
        if read.mapping_quality < self.min_mapping_quality:
            return "below_mapq"
        return None

    def eligible(self, read: ReadLike) -> bool:
        return self.classify(read) is None

    def policy_hash(self) -> str:
        return canonical_hash(
            {
                "min_mapping_quality": self.min_mapping_quality,
                "drop_unmapped": self.drop_unmapped,
                "drop_secondary": self.drop_secondary,
                "drop_supplementary": self.drop_supplementary,
                "drop_duplicate": self.drop_duplicate,
                "drop_qcfail": self.drop_qcfail,
                "exclusion_order": list(EXCLUSION_ORDER),
                "policy_version": "layer1-read-filter-v1",
            }
        )

    @classmethod
    def from_config(cls, filters: dict[str, Any]) -> ReadFilterPolicy:
        return cls(
            min_mapping_quality=int(filters.get("min_mapping_quality", 0)),
            drop_unmapped=bool(filters.get("drop_unmapped", True)),
            drop_secondary=bool(filters.get("drop_secondary", True)),
            drop_supplementary=bool(filters.get("drop_supplementary", True)),
            drop_duplicate=bool(filters.get("drop_duplicate", True)),
            drop_qcfail=bool(filters.get("drop_qcfail", True)),
        )
