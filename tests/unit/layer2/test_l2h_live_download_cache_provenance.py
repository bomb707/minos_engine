"""Cached bytes must be shown to have come from THIS round-status response.

The official downloader caches by existence when the platform publishes no ``bam_sha256``
(``download_file_verified``: "Cache hit (no hash check)"), and ``Miner._download_bam`` keys its
output directory on ``round_id`` alone. So two responses for the same round with different URLs can
legitimately return the *first* response's bytes, and a per-instance binding cannot catch it,
because the downloads object is created after the cache hit.

These tests drive the real integration with a stand-in downloader that reproduces exactly that
behaviour, and require the engine to refuse rather than mint authority over bytes whose provenance
it cannot establish.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.protocol.round_status import (
    PROVENANCE_SIDECAR_SCHEMA,
    PlatformRoundStatusError,
    _operational_source_digest,
    _require_cached_bytes_belong_to_this_round,
    _sidecar_path,
    _write_provenance_sidecar,
    accept_fixture_round_downloads,
    observe_fixture_round_status,
)
from minos_engine.protocol.round_status import (
    FixtureRoundStatusTransport as Fixture,
)
from tests.conftest import REPO_ROOT
from tests.layer2_live_replay import FIXTURE_BAI_URL, FIXTURE_BAM_URL, round_status_payload


def _subnet_root() -> Path:
    """Located relative to the repository, and overridable; never an operator-specific path."""
    import os

    configured = os.environ.get("MINOS_SUBNET_ROOT")
    return Path(configured) if configured else REPO_ROOT.parent / "minos_subnet"


REGION = "chr18:1-300000"
ROUND = "2026-09-09T09:00:00+00:00"
URL_A = FIXTURE_BAM_URL
URL_B = "https://fixture.invalid/other-source/input.bam?sig=B"


def _receipt(**override: Any):
    payload = round_status_payload(round_id=ROUND, region=REGION)
    payload = {**payload, **override}
    return observe_fixture_round_status(Fixture(payload))


# --------------------------------------------------------------------------- #
# the finding, stated as a test
# --------------------------------------------------------------------------- #
def test_the_official_downloader_caches_by_existence_without_a_digest():
    """Read from the installed subnet source, so the finding cannot silently go stale."""
    utility = _subnet_root() / "utils" / "file_utils.py"
    if not utility.is_file():
        pytest.skip("the subnet checkout is not available beside this repository")
    source = utility.read_text()
    assert "Cache hit (no hash check)" in source
    assert "if expected_sha256 is None:" in source
    miner = (_subnet_root() / "neurons" / "miner.py").read_text()
    assert 'round_data.get("bam_sha256")' in miner, "the digest is optional upstream"
    assert 'output_dir = BASE_DIR / "output" / safe_round_dir_name(round_id)' in miner
    # the index IS always refreshed, which is why only the BAM needs this protection
    assert "if bam_index.exists():\n            bam_index.unlink()" in miner


def test_two_responses_for_one_round_have_different_source_digests():
    first = _receipt()
    second = _receipt(bam_presigned_url=URL_B)
    assert first.identity == second.identity, "the scientific identity cannot separate them"
    assert _operational_source_digest(first) != _operational_source_digest(second)


# --------------------------------------------------------------------------- #
# the sidecar policy
# --------------------------------------------------------------------------- #
def test_cached_bytes_without_a_sidecar_are_refused(tmp_path):
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"cached from somewhere")
    with pytest.raises(PlatformRoundStatusError, match="no provenance record exists"):
        _require_cached_bytes_belong_to_this_round(
            bam, source_digest=_operational_source_digest(_receipt()), bam_sha256="0" * 64
        )


def test_cached_bytes_from_another_response_are_refused(tmp_path):
    """The A/B substitution: bytes fetched under response A, presented under response B."""
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"the bytes response A served")
    from minos_engine.common.hashing import sha256_hex

    digest = sha256_hex(bam.read_bytes())

    receipt_a = _receipt()
    receipt_b = _receipt(bam_presigned_url=URL_B)
    _write_provenance_sidecar(
        bam,
        source_digest=_operational_source_digest(receipt_a),
        round_id=ROUND,
        bam_sha256=digest,
        bai_sha256="1" * 64,
    )
    # under A the cached bytes carry provenance ...
    _require_cached_bytes_belong_to_this_round(
        bam, source_digest=_operational_source_digest(receipt_a), bam_sha256=digest
    )
    # ... and under B they do not
    with pytest.raises(PlatformRoundStatusError, match="DIFFERENT round-status response"):
        _require_cached_bytes_belong_to_this_round(
            bam, source_digest=_operational_source_digest(receipt_b), bam_sha256=digest
        )


def test_a_sidecar_that_no_longer_describes_the_bytes_is_refused(tmp_path):
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"bytes")
    receipt = _receipt()
    _write_provenance_sidecar(
        bam,
        source_digest=_operational_source_digest(receipt),
        round_id=ROUND,
        bam_sha256="a" * 64,
        bai_sha256="b" * 64,
    )
    with pytest.raises(PlatformRoundStatusError, match="no longer hashes"):
        _require_cached_bytes_belong_to_this_round(
            bam, source_digest=_operational_source_digest(receipt), bam_sha256="c" * 64
        )


@pytest.mark.parametrize("payload", [b"{not json", b'{"schema_version": "someone-elses"}'])
def test_an_unreadable_or_foreign_sidecar_is_refused(tmp_path, payload):
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"bytes")
    _sidecar_path(bam).write_bytes(payload)
    with pytest.raises(PlatformRoundStatusError):
        _require_cached_bytes_belong_to_this_round(
            bam, source_digest=_operational_source_digest(_receipt()), bam_sha256="0" * 64
        )


def test_the_sidecar_records_a_digest_and_never_a_url(tmp_path):
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"bytes")
    receipt = _receipt()
    _write_provenance_sidecar(
        bam,
        source_digest=_operational_source_digest(receipt),
        round_id=ROUND,
        bam_sha256="a" * 64,
        bai_sha256="b" * 64,
    )
    raw = _sidecar_path(bam).read_bytes()
    record = json.loads(raw)
    assert record["schema_version"] == PROVENANCE_SIDECAR_SCHEMA
    assert set(record) == {
        "schema_version",
        "round_id",
        "operational_source_digest",
        "bam_sha256",
        "bai_sha256",
    }
    blob = raw.decode().lower()
    for fragment in ("http", "://", "sig=", "?", "presigned"):
        assert fragment not in blob, fragment
    assert URL_A not in blob and URL_B not in blob


def test_the_sidecar_write_is_atomic_and_leaves_no_staging_file(tmp_path):
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"bytes")
    _write_provenance_sidecar(
        bam,
        source_digest="0" * 64,
        round_id=ROUND,
        bam_sha256="a" * 64,
        bai_sha256="b" * 64,
    )
    assert _sidecar_path(bam).is_file()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".minos-prov.")] == []


# --------------------------------------------------------------------------- #
# the whole integration, against a downloader that behaves like the official one
# --------------------------------------------------------------------------- #
class _CachingDownloader:
    """Reproduces `_download_bam`: output keyed on round_id, cache-by-existence, fresh index."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fetches = 0

    def expected_path(self, round_id: str) -> Path:
        """What `_official_output_bam_path` derives in production, for this stand-in."""
        return self.root / round_id.replace(":", "_") / "input.bam"

    def _download_bam(self, round_data: dict[str, Any], round_id: str) -> Path:
        """Mirrors `download_file_verified`: reuse on existence, or on a matching digest."""
        from minos_engine.common.hashing import sha256_hex

        bam = self.expected_path(round_id)
        bam.parent.mkdir(parents=True, exist_ok=True)
        expected = round_data.get("bam_sha256")
        reuse = bam.exists() and (expected is None or sha256_hex(bam.read_bytes()) == expected)
        if not reuse:
            self.fetches += 1
            bam.write_bytes(b"payload from " + str(round_data.get("bam_presigned_url")).encode())
        bai = Path(str(bam) + ".bai")
        bai.unlink(missing_ok=True)
        bai.write_bytes(b"index for " + bam.name.encode())
        return bam


def _run_integration(receipt: Any, downloader: _CachingDownloader) -> Any:
    """Drive the REAL policy with a stand-in downloader.

    Only three things are substituted, and none of them is the policy: the two exact-type gates
    (which need the subnet package installed) and the official output-path derivation (which reads
    ``BASE_DIR`` from that same package). Everything the tests are about -- the pre/post inode
    proof, the sidecar rules, the digest handling -- is production code.
    """
    from minos_engine.protocol import round_status as module

    wrapper = type("VerifiedOfficialMiner", (), {"miner": downloader})()
    saved = (
        module.is_verified_official_miner,
        module.is_verified_production_receipt,
        module._official_output_bam_path,
    )
    module.is_verified_official_miner = lambda candidate: candidate is wrapper  # type: ignore[assignment]
    module.is_verified_production_receipt = lambda candidate: candidate is receipt  # type: ignore[assignment]
    module._official_output_bam_path = downloader.expected_path  # type: ignore[assignment]
    try:
        return module.download_production_round_inputs(receipt=receipt, miner=wrapper)
    finally:
        (
            module.is_verified_official_miner,
            module.is_verified_production_receipt,
            module._official_output_bam_path,
        ) = saved  # type: ignore[assignment]


def test_a_first_fetch_records_provenance_and_a_reuse_under_the_same_response_carries(tmp_path):
    downloader = _CachingDownloader(tmp_path)
    receipt = _receipt()
    first = _run_integration(receipt, downloader)
    assert downloader.fetches == 1
    assert first.bam_source_slot == "official-miner-download"

    again = _run_integration(receipt, downloader)
    assert downloader.fetches == 1, "the official downloader cached, as it is entitled to"
    assert again.bam_sha256 == first.bam_sha256


def test_cached_bytes_cannot_acquire_another_responses_authority(tmp_path):
    """§I: bytes from response A must not obtain response B's production download authority."""
    downloader = _CachingDownloader(tmp_path)
    receipt_a = _receipt()
    _run_integration(receipt_a, downloader)
    assert downloader.fetches == 1

    receipt_b = _receipt(bam_presigned_url=URL_B)
    assert receipt_a.identity == receipt_b.identity
    with pytest.raises(PlatformRoundStatusError, match="DIFFERENT round-status response"):
        _run_integration(receipt_b, downloader)
    assert downloader.fetches == 1, "nothing was silently deleted or re-fetched"


def test_a_published_digest_makes_a_cache_hit_authoritative(tmp_path):
    """With bam_sha256 the official downloader verifies content, so provenance is the hash."""
    downloader = _CachingDownloader(tmp_path)
    seed = _receipt()
    first = _run_integration(seed, downloader)

    with_digest = _receipt(bam_presigned_url=URL_B, bam_sha256=first.bam_sha256)
    result = _run_integration(with_digest, downloader)
    assert result.bam_sha256 == first.bam_sha256


def test_a_mismatched_published_digest_is_refused(tmp_path):
    downloader = _CachingDownloader(tmp_path)
    receipt = _receipt(bam_sha256="a" * 64)
    with pytest.raises(PlatformRoundStatusError, match="SHA-256 the platform published"):
        _run_integration(receipt, downloader)


def test_a_download_failure_or_missing_index_is_refused(tmp_path):
    class Fails(_CachingDownloader):
        def _download_bam(self, round_data, round_id):
            return None

    class NoIndex(_CachingDownloader):
        def _download_bam(self, round_data, round_id):
            bam = self.expected_path(round_id)
            bam.parent.mkdir(parents=True, exist_ok=True)
            bam.write_bytes(b"only the alignment")
            return bam

    with pytest.raises(PlatformRoundStatusError, match="could not be downloaded"):
        _run_integration(_receipt(), Fails(tmp_path / "a"))
    with pytest.raises(PlatformRoundStatusError, match="no BAM index"):
        _run_integration(_receipt(), NoIndex(tmp_path / "b"))


def test_the_fixture_route_is_unaffected_and_still_deterministic(tmp_path):
    """Offline replay keeps working: the fixture route never touches this policy."""
    bam = tmp_path / "f.bam"
    bai = tmp_path / "f.bam.bai"
    bam.write_bytes(b"fixture bam")
    bai.write_bytes(b"fixture bai")
    receipt = _receipt()
    downloads = accept_fixture_round_downloads(
        receipt=receipt,
        bam_source_url=FIXTURE_BAM_URL,
        bam_path=bam.resolve(),
        bai_source_url=FIXTURE_BAI_URL,
        bai_path=bai.resolve(),
    )
    assert downloads.scope == "fixture"
    assert not _sidecar_path(bam).exists(), "the fixture route writes no sidecar"


# --------------------------------------------------------------------------- #
# timestamps must never decide anything on their own
# --------------------------------------------------------------------------- #
class _NeverWrites(_CachingDownloader):
    """The BAM already exists and the downloader leaves it exactly as it found it."""

    def _download_bam(self, round_data: dict[str, Any], round_id: str) -> Path:
        bam = self.expected_path(round_id)
        bai = Path(str(bam) + ".bai")
        bai.unlink(missing_ok=True)
        bai.write_bytes(b"fresh index")  # the miner always rebuilds this
        return bam


def _plant_cached_bam(downloader: _CachingDownloader, *, mtime_ns: int | None = None) -> Path:
    import os

    bam = downloader.expected_path(ROUND)
    bam.parent.mkdir(parents=True, exist_ok=True)
    bam.write_bytes(b"bytes response A served")
    if mtime_ns is not None:
        os.utime(bam, ns=(mtime_ns, mtime_ns))
    return bam


def test_an_equal_mtime_is_not_evidence_of_a_write(tmp_path):
    """The fail-open equality boundary, closed. The file is never touched by this call."""
    downloader = _NeverWrites(tmp_path)
    bam = _plant_cached_bam(downloader)
    before = bam.stat().st_mtime_ns

    receipt = _receipt(bam_presigned_url=URL_B)
    with pytest.raises(PlatformRoundStatusError, match="no provenance record exists"):
        _run_integration(receipt, downloader)
    assert bam.stat().st_mtime_ns == before, "nothing was rewritten"
    assert downloader.fetches == 0


def test_a_future_mtime_is_not_evidence_of_a_write(tmp_path):
    """Filesystem metadata must not become caller-controlled authority."""
    import time

    downloader = _NeverWrites(tmp_path)
    far_future = time.time_ns() + 3_600_000_000_000  # an hour ahead
    bam = _plant_cached_bam(downloader, mtime_ns=far_future)
    assert bam.stat().st_mtime_ns > time.time_ns()

    with pytest.raises(PlatformRoundStatusError, match="no provenance record exists"):
        _run_integration(_receipt(), downloader)


def test_a_future_mtime_cannot_launder_another_responses_bytes(tmp_path):
    """The A/B substitution, with the clock stacked in the attacker's favour."""
    import time

    downloader = _CachingDownloader(tmp_path)
    _run_integration(_receipt(), downloader)  # response A fetches and records provenance
    assert downloader.fetches == 1

    bam = downloader.expected_path(ROUND)
    ahead = time.time_ns() + 3_600_000_000_000
    import os

    os.utime(bam, ns=(ahead, ahead))

    receipt_b = _receipt(bam_presigned_url=URL_B)
    with pytest.raises(PlatformRoundStatusError, match="DIFFERENT round-status response"):
        _run_integration(receipt_b, downloader)
    assert downloader.fetches == 1, "nothing was deleted or re-fetched"


def test_an_unknown_output_layout_is_not_fresh(tmp_path):
    """If the official layout cannot be read, freshness is unknown -- which means not fresh."""
    from minos_engine.protocol import round_status as module

    downloader = _NeverWrites(tmp_path)
    _plant_cached_bam(downloader)
    receipt = _receipt()
    saved = (
        module.is_verified_official_miner,
        module.is_verified_production_receipt,
        module._official_output_bam_path,
    )
    wrapper = type("VerifiedOfficialMiner", (), {"miner": downloader})()
    module.is_verified_official_miner = lambda candidate: candidate is wrapper
    module.is_verified_production_receipt = lambda candidate: candidate is receipt
    module._official_output_bam_path = lambda round_id: None  # layout unreadable
    try:
        with pytest.raises(PlatformRoundStatusError, match="no provenance record exists"):
            module.download_production_round_inputs(receipt=receipt, miner=wrapper)
    finally:
        (
            module.is_verified_official_miner,
            module.is_verified_production_receipt,
            module._official_output_bam_path,
        ) = saved


def test_a_returned_path_outside_the_official_layout_is_not_fresh(tmp_path):
    """A miner that writes somewhere unexpected cannot claim this call wrote the file."""
    from minos_engine.protocol import round_status as module

    class Elsewhere(_CachingDownloader):
        def _download_bam(self, round_data, round_id):
            stray = self.root / "elsewhere" / "input.bam"
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(b"written outside the official layout")
            Path(str(stray) + ".bai").write_bytes(b"index")
            return stray

    downloader = Elsewhere(tmp_path)
    receipt = _receipt()
    saved = (
        module.is_verified_official_miner,
        module.is_verified_production_receipt,
        module._official_output_bam_path,
    )
    wrapper = type("VerifiedOfficialMiner", (), {"miner": downloader})()
    module.is_verified_official_miner = lambda candidate: candidate is wrapper
    module.is_verified_production_receipt = lambda candidate: candidate is receipt
    module._official_output_bam_path = downloader.expected_path
    try:
        with pytest.raises(PlatformRoundStatusError, match="no provenance record exists"):
            module.download_production_round_inputs(receipt=receipt, miner=wrapper)
    finally:
        (
            module.is_verified_official_miner,
            module.is_verified_production_receipt,
            module._official_output_bam_path,
        ) = saved


def test_a_rewritten_file_IS_fresh_even_with_an_unchanged_mtime(tmp_path):
    """The rule is inode state, not the clock: ctime moves even when mtime is restored."""
    import os

    class RewritesPreservingMtime(_CachingDownloader):
        def _download_bam(self, round_data, round_id):
            bam = self.expected_path(round_id)
            bam.parent.mkdir(parents=True, exist_ok=True)
            stamp = bam.stat().st_mtime_ns if bam.exists() else None
            self.fetches += 1
            bam.write_bytes(
                b"genuinely re-fetched under " + str(round_data.get("bam_presigned_url")).encode()
            )
            if stamp is not None:
                os.utime(bam, ns=(stamp, stamp))
            bai = Path(str(bam) + ".bai")
            bai.unlink(missing_ok=True)
            bai.write_bytes(b"index")
            return bam

    downloader = RewritesPreservingMtime(tmp_path)
    _plant_cached_bam(downloader)
    result = _run_integration(_receipt(bam_presigned_url=URL_B), downloader)
    assert result.bam_source_slot == "official-miner-download"
