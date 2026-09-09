"""The raw upstream authority checks must use exact type, like every other authority here.

A subclass of the real ``MinerPlatformClient`` can override ``get_round_status``; a subclass of the
real ``Miner`` can override ``_download_bam``. Either would otherwise earn a genuine sealed
production capability while returning whatever it liked.

This environment cannot import the subnet (``bittensor_wallet`` is absent), and that is not a
reason to leave the attack untested: the resolver itself is monkeypatched, so the *policy* is
proven independently of whether the subnet's dependencies are installed.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from minos_engine.protocol import round_status as module
from minos_engine.protocol.round_status import (
    PlatformRoundStatusError,
    ProductionRoundStatusTransport,
    is_verified_official_miner,
    production_round_status_transport,
    verify_official_miner,
    verify_subnet_platform_client,
)


class OfficialClient:
    """Stands in for the real ``utils.platform_client.MinerPlatformClient``."""

    demo = False

    def __init__(self, *, base_url: str = "https://platform.example", hotkey: str = "5F") -> None:
        self.keypair = types.SimpleNamespace(ss58_address=hotkey)
        self.config = types.SimpleNamespace(base_url=base_url)

    async def get_round_status(self) -> dict[str, Any]:
        return {}


class ForgedClient(OfficialClient):
    """A real subclass of the official class, returning whatever it likes."""

    async def get_round_status(self) -> dict[str, Any]:
        return {"has_active_round": True, "status": "open", "round_id": "deadbeef"}


class OfficialMiner:
    """Stands in for the real ``neurons.miner.Miner``."""

    def _download_bam(self, round_data: dict[str, Any], round_id: str) -> str:
        return "/tmp/official.bam"


class ForgedMiner(OfficialMiner):
    def _download_bam(self, round_data: dict[str, Any], round_id: str) -> str:
        return "/tmp/attacker.bam"


@pytest.fixture
def official_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "resolve_official_platform_client_type", lambda: OfficialClient)
    monkeypatch.setattr(module, "resolve_official_miner_type", lambda: OfficialMiner)


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #
def test_the_actual_official_client_is_accepted(official_types):
    verified = verify_subnet_platform_client(OfficialClient())
    assert verified.client.__class__ is OfficialClient
    transport = production_round_status_transport(OfficialClient())
    assert type(transport) is ProductionRoundStatusTransport


def test_a_subclass_of_the_official_client_is_refused(official_types):
    """The gap: `isinstance` would have accepted this and minted a real sealed capability."""
    forged = ForgedClient()
    assert isinstance(forged, OfficialClient), "isinstance alone would pass"
    with pytest.raises(PlatformRoundStatusError, match="whatever it is called or inherits from"):
        verify_subnet_platform_client(forged)
    with pytest.raises(PlatformRoundStatusError, match="whatever it is called or inherits from"):
        production_round_status_transport(forged)


def test_the_client_conditions_still_apply_after_exact_type(official_types):
    with pytest.raises(PlatformRoundStatusError, match="not HTTPS"):
        verify_subnet_platform_client(OfficialClient(base_url="http://platform.example"))
    with pytest.raises(PlatformRoundStatusError, match="no hotkey"):
        verify_subnet_platform_client(OfficialClient(hotkey=""))
    demo = OfficialClient()
    demo.demo = True
    with pytest.raises(PlatformRoundStatusError, match="demo mode"):
        verify_subnet_platform_client(demo)


# --------------------------------------------------------------------------- #
# the miner
# --------------------------------------------------------------------------- #
def test_the_actual_official_miner_is_accepted(official_types):
    verified = verify_official_miner(OfficialMiner())
    assert is_verified_official_miner(verified)
    assert verified.miner.__class__ is OfficialMiner


def test_a_subclass_of_the_official_miner_is_refused(official_types):
    """Refused even though its `_download_bam` looks perfectly valid."""
    forged = ForgedMiner()
    assert isinstance(forged, OfficialMiner), "isinstance alone would pass"
    assert callable(forged._download_bam)
    with pytest.raises(PlatformRoundStatusError, match="whatever it inherits from"):
        verify_official_miner(forged)


def test_a_miner_without_the_download_operation_is_refused(monkeypatch):
    class Hollow:
        pass

    monkeypatch.setattr(module, "resolve_official_miner_type", lambda: Hollow)
    with pytest.raises(PlatformRoundStatusError, match="does not expose its download operation"):
        verify_official_miner(Hollow())


@pytest.mark.parametrize("candidate", [object(), None, "OfficialMiner", {}])
def test_unrelated_objects_are_refused(official_types, candidate):
    with pytest.raises(PlatformRoundStatusError):
        verify_official_miner(candidate)
    with pytest.raises(PlatformRoundStatusError):
        verify_subnet_platform_client(candidate)


# --------------------------------------------------------------------------- #
# the rule is stated once, in source
# --------------------------------------------------------------------------- #
def test_no_raw_upstream_check_uses_isinstance():
    import inspect

    source = inspect.getsource(module)
    for banned in (
        "isinstance(client, official)",
        "isinstance(miner, official)",
    ):
        assert banned not in source, banned
    assert "type(client) is official" in source
    assert "type(miner) is official" in source


def test_the_real_resolvers_still_fail_closed_without_the_subnet():
    """Unpatched, and with the package absent, production refuses rather than falling back."""
    try:
        module.resolve_official_platform_client_type()
    except PlatformRoundStatusError as error:
        assert "cannot be imported" in str(error)
        assert "does not fall back" in str(error)
    else:  # pragma: no cover - only where the subnet is installed
        pytest.skip("the subnet package is importable here")
