"""Frozen-artifact guard for the L2-F live-GATK parameter-space corrective.

The corrective regenerates only L2-F *derived* identities. Every historical upstream artifact —
the L2-C/L2-D/L2-E gates, manifests, and migrations 0001-0007 — must stay byte-identical, and the
operational database must stay untouched. These byte hashes were captured at the pre-corrective
commit 908fc4e0 and must never change here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: byte SHA-256 of every frozen gate / manifest / migration at 908fc4e0 (pre-corrective).
FROZEN_ARTIFACT_SHA256: dict[str, str] = {
    "gates/db-ready.json": "49741ccaefa84115705b8107da761e1c561fc32959111c7fa9316d1d7c6a585a",
    "gates/feature-matrix-frozen-1.json": "293ae36a2f677b084b85c5097e90447b701893c5376fcd613c2783f7adb39597",
    "gates/feature-view-ready.json": "d2ea43515fe347eda8c6e35ec7e37c982890a40209a6fc1e92c15e379bb52573",
    "gates/ingest-ready.json": "f0299d3004420506f1e34bf0bf14eb26730964816c94356f36da0f6f300acf5c",
    "gates/l1-ready.json": "d7099a12a2a93e447fd97cab3a4eec1a1a27a90b9262351ec7f576259946d951",
    "gates/profile-snapshot-frozen-1.json": "0596719f05aaf10041eef65fa1bb2a8c1c9c6d4b681b5f0ea7ed8720b75ad768",
    "gates/protocol-ready.json": "edb57817829a4d4f7f359f12dd626d8333d07f505c8ef5349f22d79ff93d8cf9",
    "gates/split-frozen-v2.json": "a4872e36a478e877f4bf6787198e80863d0fa5257e7e07507649656c9c2c24c3",
    "gates/split-frozen.json": "9af40f1bdc07ba7a10a2b3e46fb68ddc91b7eff1a88eac19083a9af9b252f5cf",
    "gates/twin-ready.json": "2edffacdddeaf21125715be18b14e77947e063b8a29dfb2bd43b171a864cddde",
    "manifests/layer2_dataset_split_v1.json": "fd4525f717c02f530ca88d906ab10eeb35bcc91c142e6b33655e4ee5f72c9e02",
    "manifests/layer2_dataset_split_v2_epoch1.json": "ffdd31955a24147430156aff003248f8acb51c68514ca95c6fdbe75525328773",
    "manifests/layer2_local_input_inventory_v1.json": "807b23d81a734f7b52067fb96f2eaec82c4ad6b963a24cca317daa4bc9837c04",
    "manifests/profile_snapshot_epoch1_artifact_inventory.json": "ebc9a37a51b1d683ebae6f91623bb849a7794bb3a997f3ec26c61021eac05240",
    "manifests/profile_snapshot_epoch1_members.json": "826d18948f88fe246e90ec530a5083f48ddf6002285d66093759b9c8ccbaf563",
    "manifests/profile_snapshot_epoch1_selections.json": "28eaeffd725a7db68d1dd3284343df98f3c3a53d921d776a36f5cc7978ef8299",
    "migrations/versions/0001_l2b_initial.py": "7cb904702a3d7e6861c3f828590fff59cff04dc42b90a76b2a617c2c77f03f12",
    "migrations/versions/0002_l2c_dataset_split.py": "f3dd195311959ce8caf079b7d9ceb00731bd72a4e7c22485338778885a0a96c6",
    "migrations/versions/0003_l2c_split_v2_epochs.py": "16446b8cfd82900180a6f7a04a62f35fc463597857265caa25376f38e7c66f9c",
    "migrations/versions/0004_l2d_profile_ingestion.py": "f540ba7f5c88ada0e1da9948f5bc7ae97d37dc3cbb1394b192c8c498e739ae0e",
    "migrations/versions/0005_l2e_feature_view.py": "21254bbeb6f131d043532127b1baaee51d2a50c8c2049967cd7007ba5aeefb23",
    "migrations/versions/0006_l2f_experiment_plan.py": "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6",
    "migrations/versions/0007_l2f_job_claiming.py": "bc247e0a68f82ad6e52868e115db3f1e237b637def98567c596e3cc0a4e42625",
}


@pytest.mark.parametrize("relative_path", sorted(FROZEN_ARTIFACT_SHA256))
def test_upstream_artifact_is_byte_identical(relative_path: str) -> None:
    path = _REPO_ROOT / relative_path
    assert path.is_file(), relative_path
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == FROZEN_ARTIFACT_SHA256[relative_path], relative_path


def test_migration_0006_and_0007_hashes_are_unchanged() -> None:
    """The two accepted L2-F migration identities, stated explicitly."""
    assert (
        FROZEN_ARTIFACT_SHA256["migrations/versions/0006_l2f_experiment_plan.py"]
        == "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
    )
    assert (
        FROZEN_ARTIFACT_SHA256["migrations/versions/0007_l2f_job_claiming.py"]
        == "bc247e0a68f82ad6e52868e115db3f1e237b637def98567c596e3cc0a4e42625"
    )


def test_migration_contract_still_recomputes() -> None:
    from minos_engine.storage.l2f_migration_contract import (
        L2F_CONTRACT_HASH,
        L2F_MIGRATION_SHA256,
        compute_migration_sha256,
    )

    assert compute_migration_sha256() == L2F_MIGRATION_SHA256
    assert L2F_CONTRACT_HASH == "c7a2e978857830ccff67821ded1196472d5f38baacb19a64352ec686ce74916b"


def test_historical_upstream_parameter_space_identity_is_untouched() -> None:
    """The frozen upstream split parameter-space identity 605679... is unchanged; the L2-F
    live-GATK identity is a SEPARATE, additional contract."""
    from minos_engine.callers.gatk.parameter_registry import REGISTRY
    from minos_engine.experiments.gatk_live_space import live_gatk_parameter_space

    historical = REGISTRY.documented_parameter_space(retrieved_at="2026-08-09T00:00:00+00:00")
    assert (
        historical.parameter_space_hash
        == "605679294caea090c8a78a5c93f3b816cb2aff05251b33446a7e312e83c205fc"
    )
    assert live_gatk_parameter_space().parameter_space_hash != historical.parameter_space_hash
