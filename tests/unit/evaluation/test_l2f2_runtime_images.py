"""Runtime container provenance — what a reference resolves to on THIS host, before scoring.

The pinned upstream source names bcftools by tag. MINOS_ENGINE never rewrites that tag, so the
only way "upstream will run the audited bcftools" becomes a reproducible statement is to ask the
local daemon what the tag currently resolves to and require the audited digest.

These tests drive the real parsing and policy through a fake ``docker image inspect``, so the
whole verifier is exercised without a Docker daemon and without pulling anything.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from minos_engine.evaluation.runtime_images import (
    DEFAULT_INSPECT_TIMEOUT_SECONDS,
    LocalImage,
    RuntimeImageAbsentError,
    RuntimeImageContentError,
    RuntimeImageError,
    inspect_local_image,
    verify_runtime_image,
)

_TAG = "quay.io/biocontainers/bcftools:1.20--h8b25389_0"
_DIGEST = "quay.io/biocontainers/bcftools@sha256:" + "b" * 64
_OTHER = "quay.io/biocontainers/bcftools@sha256:" + "9" * 64


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_docker(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[list[str]]:
    """Replace the subprocess call, recording the exact argv the verifier constructs."""
    calls: list[list[str]] = []

    def _run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        assert kwargs.get("check") is False
        assert "shell" not in kwargs, "the verifier must never use a shell"
        assert isinstance(kwargs.get("timeout"), int)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


# --------------------------------------------------------------------------- #
# inspection
# --------------------------------------------------------------------------- #
def test_inspection_reads_the_local_image_without_pulling(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.dumps({"Id": "sha256:" + "e" * 64, "RepoDigests": [_DIGEST]})
    calls = _fake_docker(monkeypatch, _Completed(0, stdout=document + "\n"))

    local = inspect_local_image(_TAG)

    assert local == LocalImage(_TAG, "sha256:" + "e" * 64, (_DIGEST,))
    assert calls == [["docker", "image", "inspect", _TAG, "--format", "{{json .}}"]]
    # a scoring run must never fetch bytes off the network.
    flat = " ".join(calls[0])
    for banned in ("pull", "tag", "run", "rmi", "push"):
        assert f" {banned}" not in f" {flat} ", banned


def test_a_missing_image_is_a_typed_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_docker(monkeypatch, _Completed(1, stderr="No such image"))
    with pytest.raises(RuntimeImageAbsentError, match="never pulls"):
        inspect_local_image(_TAG)


def test_a_timeout_is_refused_not_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_docker(monkeypatch, subprocess.TimeoutExpired(cmd="docker", timeout=1))
    with pytest.raises(RuntimeImageError, match="exceeded"):
        inspect_local_image(_TAG)


def test_a_missing_docker_binary_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_docker(monkeypatch, OSError("no docker"))
    with pytest.raises(RuntimeImageError, match="cannot run docker"):
        inspect_local_image(_TAG)


@pytest.mark.parametrize(
    ("stdout", "needle"),
    [
        ("not json at all", "did not return JSON"),
        ("[]", "returned"),
        (json.dumps({"RepoDigests": [_DIGEST]}), "no image Id"),
        (json.dumps({"Id": "", "RepoDigests": [_DIGEST]}), "no image Id"),
        (json.dumps({"Id": "sha256:x", "RepoDigests": "not-a-list"}), "malformed RepoDigests"),
        (json.dumps({"Id": "sha256:x", "RepoDigests": [1, 2]}), "malformed RepoDigests"),
    ],
    ids=["not-json", "wrong-type", "no-id", "empty-id", "digests-not-list", "digests-not-strings"],
)
def test_a_malformed_response_is_refused(
    monkeypatch: pytest.MonkeyPatch, stdout: str, needle: str
) -> None:
    _fake_docker(monkeypatch, _Completed(0, stdout=stdout))
    with pytest.raises(RuntimeImageError, match=needle):
        inspect_local_image(_TAG)


def test_an_empty_reference_is_refused() -> None:
    with pytest.raises(RuntimeImageError, match="empty image reference"):
        inspect_local_image("   ")


def test_the_inspect_timeout_is_bounded_by_default() -> None:
    assert 0 < DEFAULT_INSPECT_TIMEOUT_SECONDS <= 300


# --------------------------------------------------------------------------- #
# verification policy
# --------------------------------------------------------------------------- #
def _inspector(digests: tuple[str, ...]) -> Any:
    return lambda reference: LocalImage(reference, "sha256:" + "e" * 64, digests)


def test_a_matching_digest_passes() -> None:
    local = verify_runtime_image(
        _TAG, expected_digest=_DIGEST, label="bcftools", inspector=_inspector((_DIGEST,))
    )
    assert local.repo_digests == (_DIGEST,)


def test_a_tag_resolving_to_other_content_is_refused() -> None:
    """Exactly the moving-tag risk this verifier exists for."""
    with pytest.raises(RuntimeImageContentError, match="audited"):
        verify_runtime_image(
            _TAG, expected_digest=_DIGEST, label="bcftools", inspector=_inspector((_OTHER,))
        )


def test_an_image_with_no_repo_digests_is_refused() -> None:
    """A locally built image has no distributed content identity, so it cannot be the audited one."""
    with pytest.raises(RuntimeImageContentError, match="audited"):
        verify_runtime_image(
            _TAG, expected_digest=_DIGEST, label="bcftools", inspector=_inspector(())
        )


def test_matching_is_on_distributed_content_not_the_local_image_id() -> None:
    """Two hosts may legitimately disagree about a local Id while naming the same content."""
    inspector = lambda reference: LocalImage(reference, "sha256:" + "1" * 64, (_DIGEST,))  # noqa: E731
    assert verify_runtime_image(
        _TAG, expected_digest=_DIGEST, label="bcftools", inspector=inspector
    ).image_id.startswith("sha256:1")


def test_the_committed_authority_identities_verify_against_the_fake_daemon() -> None:
    """The policy is wired to the real authority values, not to test-local constants."""
    from minos_engine.evaluation.scoring_contract import load_scoring_authority

    authority = load_scoring_authority()
    for label, identity in (("hap.py", authority.happy), ("bcftools", authority.bcftools)):
        verify_runtime_image(
            identity.upstream_ref,
            expected_digest=identity.resolved_digest,
            label=label,
            inspector=_inspector((identity.resolved_digest,)),
        )
