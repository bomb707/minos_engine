"""Qualification internals: JUnit accounting, coverage, evidence hashing."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import MinosEngineError
from minos_engine.qualification.coverage import parse_coverage_xml
from minos_engine.qualification.evidence import sha256_directory, sha256_file
from minos_engine.qualification.pytest_accounting import parse_junit_xml, suite_passes

_SUCCESS_XML = (
    '<?xml version="1.0"?>'
    '<testsuites><testsuite name="pytest" tests="155" failures="0" errors="0" '
    'skipped="0" time="1.08"></testsuite></testsuites>'
)
_FAIL_XML = '<testsuite name="pytest" tests="10" failures="2" errors="0" skipped="1" time="0.5"/>'
_ERROR_XML = '<testsuite name="pytest" tests="10" failures="0" errors="3" skipped="0" time="0.5"/>'
_ZERO_XML = '<testsuite name="pytest" tests="0" failures="0" errors="0" skipped="0" time="0.0"/>'


def test_parse_success_counts():
    acc = parse_junit_xml(_SUCCESS_XML, exit_code=0)
    assert (acc.collected, acc.passed, acc.failed, acc.errors, acc.skipped) == (155, 155, 0, 0, 0)
    assert acc.duration_seconds == pytest.approx(1.08)
    assert suite_passes(acc)


def test_quiet_mode_cannot_zero_the_count():
    # The count comes from structured XML attributes, never terminal text, so a
    # suppressed "N passed" summary can never make the collected count zero.
    acc = parse_junit_xml(_SUCCESS_XML, exit_code=0)
    assert acc.collected == 155


def test_zero_collected_cannot_pass():
    acc = parse_junit_xml(_ZERO_XML, exit_code=0)
    assert acc.collected == 0
    assert not suite_passes(acc)


def test_failing_test_cannot_pass():
    acc = parse_junit_xml(_FAIL_XML, exit_code=1)
    assert acc.failed == 2
    assert not suite_passes(acc)


def test_execution_error_cannot_pass():
    acc = parse_junit_xml(_ERROR_XML, exit_code=1)
    assert acc.errors == 3
    assert not suite_passes(acc)


def test_nonzero_exit_cannot_pass_even_if_counts_clean():
    acc = parse_junit_xml(_SUCCESS_XML, exit_code=2)
    assert acc.failed == 0 and acc.errors == 0
    assert not suite_passes(acc)


def test_malformed_junit_raises():
    with pytest.raises(MinosEngineError):
        parse_junit_xml("<not-xml", exit_code=0)


def test_no_testsuite_raises():
    with pytest.raises(MinosEngineError):
        parse_junit_xml("<other/>", exit_code=0)


def test_parse_coverage_percent():
    xml = '<coverage lines-covered="94" lines-valid="100" line-rate="0.94"></coverage>'
    cov = parse_coverage_xml(xml)
    assert cov.line_coverage_percent == pytest.approx(94.0)
    assert cov.covered_lines == 94
    assert cov.missing_lines == 6
    assert cov.meets(90.0)
    assert not cov.meets(95.0)


def test_coverage_line_rate_fallback():
    cov = parse_coverage_xml('<coverage line-rate="0.9"></coverage>')
    assert cov.line_coverage_percent == pytest.approx(90.0)


def test_sha256_file_and_directory(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("beta")
    digest1, files1 = sha256_directory(tmp_path)
    digest2, _ = sha256_directory(tmp_path)
    assert digest1 == digest2  # deterministic
    assert {f.path for f in files1} == {"a.txt", "sub/b.txt"}


def test_directory_digest_changes_on_tamper(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    before, _ = sha256_directory(tmp_path)
    (tmp_path / "a.txt").write_text("ALPHA")
    after, _ = sha256_directory(tmp_path)
    assert before != after


def test_directory_digest_excludes_pycache(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    before, _ = sha256_directory(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"junk")
    after, _ = sha256_directory(tmp_path)
    assert before == after  # volatile artifacts excluded


def test_sha256_file_missing(tmp_path):
    from minos_engine.common.errors import ContractValidationError

    with pytest.raises(ContractValidationError):
        sha256_file(tmp_path / "nope.txt")
