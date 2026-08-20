"""E4 production-builder contract (unit; no database or filesystem required).

Covers the fail-closed source-level guarantees of the E4 materialization surface: the
mandatory identity flag, refusal of the credential-proof fixture directory, the
train/validation-only partition set (test excluded), the frozen 129 ordered columns, and
that ``Layer2Service.select_config`` stays blocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minos_engine.common.errors import MatrixAccessError, StageNotReadyError
from minos_engine.layer2.features.contracts import build_feature_set_manifest
from minos_engine.layer2.features.extraction import MATRIX_PARTITIONS
from minos_engine.storage import feature_matrix as fm
from minos_engine.storage.feature_matrix_production import (
    CREDENTIAL_PROOF_ROOT,
    PRODUCTION_PARTITIONS,
    build_operational_feature_matrices,
)


class _BoomEngine:
    """Any DB/fs access fails the test — the guards under test must trip first."""

    def connect(self):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("database must not be touched")

    def begin(self):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("database must not be touched")


# (proof 1) mandatory identity flag — omission raises TypeError before any DB/fs action
def test_persist_feature_matrix_requires_identity_flag() -> None:
    engine = _BoomEngine()
    with pytest.raises(TypeError):
        fm._persist_feature_matrix(  # type: ignore[call-arg]
            engine,  # type: ignore[arg-type]
            snapshot=None,  # type: ignore[arg-type]
            matrix=None,  # type: ignore[arg-type]
            vectors=(),
            publisher=None,  # type: ignore[arg-type]
        )  # require_operational_identity intentionally omitted


def test_persist_feature_matrix_accepts_explicit_flag_signature() -> None:
    import inspect

    sig = inspect.signature(fm._persist_feature_matrix)
    p = sig.parameters["require_operational_identity"]
    assert p.default is inspect.Parameter.empty  # no implicit bypass default
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


# refuse the credential-proof fixture directory as a production matrix root (train or val)
@pytest.mark.parametrize("which", ["train", "validation"])
def test_operational_builder_refuses_credential_proof_root(which: str) -> None:
    roots = {"train": Path("/srv/minos/l2e/train"), "validation": Path("/srv/minos/l2e/validation")}
    roots[which] = CREDENTIAL_PROOF_ROOT / "l2e" / which
    with pytest.raises(MatrixAccessError, match="credential-proof"):
        build_operational_feature_matrices(
            _BoomEngine(),  # type: ignore[arg-type]
            member_manifest_bytes=b"{}",
            train_root=roots["train"],
            validation_root=roots["validation"],
        )


def test_operational_builder_refuses_exact_credential_proof_root() -> None:
    with pytest.raises(MatrixAccessError, match="credential-proof"):
        build_operational_feature_matrices(
            _BoomEngine(),  # type: ignore[arg-type]
            member_manifest_bytes=b"{}",
            train_root=CREDENTIAL_PROOF_ROOT,
            validation_root=Path("/srv/minos/l2e/validation"),
        )


# (proof 5, structural) test partition is never in the production materialization set
def test_production_partitions_are_train_validation_only() -> None:
    assert PRODUCTION_PARTITIONS == ("train", "validation")
    assert "test" not in PRODUCTION_PARTITIONS
    assert "test" not in MATRIX_PARTITIONS


# (proof 6) frozen 129 ordered BAM-only feature columns
def test_frozen_129_ordered_columns() -> None:
    manifest = build_feature_set_manifest()
    assert manifest.column_count == 129
    assert [c.index for c in manifest.columns] == list(range(129))


# (proof 11) select_config remains blocked
def test_select_config_remains_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]
