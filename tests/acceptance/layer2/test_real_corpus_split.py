"""Acceptance: the deterministic split over the ACTUAL 75-sample practice corpus.

Runs only where the corpus is available (``MINOS_DATASET_ROOT`` or the sibling
``minos_subnet/datasets`` layout). Large BAM files are external and never committed;
this test streams their SHA-256 to build the canonical manifest and asserts the exact
50/10/15 split, byte-identical regeneration, and — when the committed manifest exists —
that the generated manifest reproduces it exactly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.layer2.split.generator import generate
from minos_engine.layer2.split.policy import SUPPORTED_CHROMOSOMES
from minos_engine.layer2.split.verifier import verify_manifest
from tests.conftest import REPO_ROOT


def _dataset_root() -> Path | None:
    env = os.environ.get("MINOS_DATASET_ROOT")
    candidates = [Path(env)] if env else []
    candidates.append(REPO_ROOT.parent / "minos_subnet" / "datasets")
    for c in candidates:
        if (c / "practice").is_dir() and (c / "reference").is_dir():
            return c
    return None


_ROOT = _dataset_root()
_skip = pytest.mark.skipif(_ROOT is None, reason="75-sample practice corpus not available")


@_skip
def test_real_corpus_exact_split_and_determinism():
    manifest, inventory = generate(_ROOT)
    assert len(manifest.samples) == 75
    assert manifest.counts == {"train": 50, "validation": 10, "test": 15}
    for c in SUPPORTED_CHROMOSOMES:
        assert manifest.per_chromosome[c] == {"train": 10, "validation": 2, "test": 3}

    v = verify_manifest(manifest.to_canonical())
    assert v.ok, v.reasons

    # byte-identical regeneration (independent of enumeration order / wall-clock).
    manifest2, inventory2 = generate(_ROOT)
    assert canonical_json_str(manifest.to_canonical()) == canonical_json_str(
        manifest2.to_canonical()
    )
    assert inventory.inventory_hash == inventory2.inventory_hash

    # no truth/mutation or absolute paths leaked into the canonical manifest.
    blob = canonical_json_str(manifest.to_canonical()).lower()
    for token in ("truth", "mutation", ".vcf", "/home/", "input.bam"):
        assert token not in blob


@_skip
def test_generated_matches_committed_manifest_if_present():
    committed = REPO_ROOT / "manifests" / "layer2_dataset_split_v1.json"
    if not committed.exists():
        pytest.skip("committed manifest is produced in Commit V")
    manifest, _ = generate(_ROOT)
    generated = canonical_json_str(manifest.to_canonical()) + "\n"
    assert generated == committed.read_text(encoding="utf-8")
