"""ArtifactIdentity: a filename alone is never an identity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.common.errors import ContractValidationError
from minos_engine.intake.artifact_identity import (
    VerificationStrength,
    build_artifact_identity,
)
from minos_engine.intake.contracts import ArtifactIdentity

_H = "a" * 64


def test_valid_identity():
    ident = build_artifact_identity(
        uri="s3://x/input.bam",
        sha256=_H,
        size_bytes=100,
        media_type="application/x-bam",
        observed_at="2026-08-17T12:00:00+00:00",
    )
    assert ident.sha256 == _H
    assert len(ident.identity_hash()) == 64


def test_unverified_strength_rejected():
    with pytest.raises(ContractValidationError):
        build_artifact_identity(
            uri="input.bam",
            sha256=_H,
            size_bytes=1,
            media_type="application/x-bam",
            observed_at="2026-08-17T12:00:00+00:00",
            strength=VerificationStrength.UNVERIFIED,
        )


def test_bad_sha_rejected():
    with pytest.raises(ValidationError):
        ArtifactIdentity(
            uri="x",
            sha256="not-a-hash",
            size_bytes=1,
            media_type="application/x-bam",
            created_at_or_observed_at="2026-08-17T12:00:00+00:00",
        )


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        ArtifactIdentity(
            uri="x",
            sha256=_H,
            size_bytes=1,
            media_type="application/x-bam",
            created_at_or_observed_at="2026-08-17T12:00:00",
        )
