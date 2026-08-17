"""Runtime preflight — Python 3.12 is the only supported runtime."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import UnsupportedRuntimeError
from minos_engine.common.runtime import (
    SUPPORTED_RUNTIME_LABEL,
    is_supported_runtime,
    require_supported_runtime,
    runtime_identity,
    runtime_report,
)


def test_python_312_accepted():
    assert is_supported_runtime((3, 12))
    assert is_supported_runtime((3, 12, 7))
    require_supported_runtime((3, 12))  # no raise


@pytest.mark.parametrize("version", [(3, 11), (3, 13), (3, 10), (4, 0), (2, 7)])
def test_unsupported_versions_rejected(version):
    assert not is_supported_runtime(version)
    with pytest.raises(UnsupportedRuntimeError):
        require_supported_runtime(version)


def test_runtime_identity_has_no_patch_level():
    assert runtime_identity() == "CPython 3.12"
    assert SUPPORTED_RUNTIME_LABEL == "CPython 3.12"


def test_runtime_report_shape():
    report = runtime_report()
    assert report["supported"] == "CPython 3.12.x"
    assert report["supported_identity"] == "CPython 3.12"
    assert isinstance(report["is_supported"], bool)


def test_current_interpreter_is_supported():
    # The test suite itself runs under the supported runtime.
    assert is_supported_runtime()
