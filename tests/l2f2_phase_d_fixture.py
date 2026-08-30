"""THE repository-local Phase-D campaign fixture, and the one place its location is decided.

The Phase-D preparation proof and the finalist-freeze unit tests both need this campaign's
accepted evidence. That evidence lives in an operational workspace which no CI runner has, so a
bounded, byte-preserving copy is committed and this module is its single address.

The root is resolved from this file's own location, never from the working directory and never
from an absolute machine path, so the suites run from any clone and on any runner.

The bundle is TEST evidence. It is never the operational campaign authority: production
preparation reads operationally supplied storage, and no module under ``src/`` imports this.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "FIXTURE_CONFIG_ROOT",
    "FIXTURE_FREEZE_PATH",
    "FIXTURE_MANIFEST_PATH",
    "FIXTURE_ROOT",
    "forgery_config_hashes",
    "load_fixture_manifest",
]

FIXTURE_ROOT = (Path(__file__).resolve().parent / "fixtures" / "l2f2_phase_d_campaign").resolve()
FIXTURE_CONFIG_ROOT = FIXTURE_ROOT / "config_artifacts"
FIXTURE_FREEZE_PATH = FIXTURE_ROOT / "phase_c_validation_finalists_20260830.json"
FIXTURE_MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"


def load_fixture_manifest() -> dict[str, Any]:
    """The bundle's own provenance record. TEST evidence; never a scientific authority."""
    return dict(json.loads(FIXTURE_MANIFEST_PATH.read_bytes()))


def forgery_config_hashes() -> tuple[str, ...]:
    """The four adversarial CONFIG identities, in committed manifest order.

    Deterministic by construction: the manifest names them and orders them by Phase-B design
    index. Nothing here globs a directory, so the forged campaign a test builds is the same
    forged campaign on every machine and in every run.

    None is a finalist and none has production scientific authority. Each is a member of the
    closed Phase-B 48-candidate design and is regenerable from committed public inputs, which
    ``tests/integration/layer2_db/test_l2f2_phase_d_fixture.py`` proves rather than assumes.
    """
    members = load_fixture_manifest()["forgery_identities"]["members"]
    ordered = tuple(str(m["config_hash"]) for m in members)
    if len(ordered) != 4 or len(set(ordered)) != 4:
        raise AssertionError("the fixture manifest must name exactly four distinct forgeries")
    return ordered
