"""F4 job claiming — pure unit tests (no database).

Covers the frozen worker-identity contract (validated BEFORE any database access), the frozen
F4 transition table, the public API surface, and the absence of every F5+ concern (leases,
stale-claim reclamation, heartbeats, automatic retry, terminal-status handling).
"""

from __future__ import annotations

import inspect
from uuid import UUID, uuid4

import pytest

from minos_engine.storage import l2f_job_claim as JC
from minos_engine.storage.l2f_job_claim import (
    F4_MIGRATION_REVISION,
    F4_TRANSITIONS,
    WORKER_ID_MAX_LENGTH,
    InvalidWorkerIdError,
    claim_next_accepted_job,
    release_accepted_job,
    start_accepted_job,
    validate_worker_id,
)


def test_f4_transition_table_is_exactly_the_three_pre_execution_moves() -> None:
    assert F4_TRANSITIONS == (
        ("PENDING", "CLAIMED"),
        ("CLAIMED", "PENDING"),
        ("CLAIMED", "RUNNING"),
    )
    reachable = {dst for _, dst in F4_TRANSITIONS} | {src for src, _ in F4_TRANSITIONS}
    assert reachable == {"PENDING", "CLAIMED", "RUNNING"}
    # terminal execution states remain unreachable until F5.
    assert not reachable & {"SUCCEEDED", "FAILED", "CANCELLED"}


def test_required_revision_is_the_f4_migration() -> None:
    assert F4_MIGRATION_REVISION == "0007_l2f_job_claiming"


@pytest.mark.parametrize(
    "worker_id",
    ["w", "worker-1", "runner.01", "host:worker_9", "W" * WORKER_ID_MAX_LENGTH, "a1_b2.c3:d4-e5"],
)
def test_valid_worker_ids_accepted(worker_id: str) -> None:
    assert validate_worker_id(worker_id) == worker_id


@pytest.mark.parametrize(
    "worker_id",
    [
        "",  # empty
        " ",  # blank
        "W" * (WORKER_ID_MAX_LENGTH + 1),  # too long
        "-leading-dash",  # must start alphanumeric
        ".leading-dot",
        "has space",
        "has/slash",
        "quote'inject",
        "semi;colon",
        "new\nline",
        "unicodé",
    ],
)
def test_invalid_worker_ids_rejected(worker_id: str) -> None:
    with pytest.raises(InvalidWorkerIdError):
        validate_worker_id(worker_id)


def test_non_string_worker_id_rejected() -> None:
    with pytest.raises(InvalidWorkerIdError):
        validate_worker_id(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidWorkerIdError):
        validate_worker_id(7)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "not-a-worker id", "-x"])
def test_invalid_worker_id_fails_before_any_database_access(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """With MINOS_DATABASE_URL unset, an invalid worker id must still raise the worker-contract
    error — proving validation happens before the database is touched."""
    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    with pytest.raises(InvalidWorkerIdError):
        claim_next_accepted_job(worker_id=bad)
    with pytest.raises(InvalidWorkerIdError):
        start_accepted_job(job_id=uuid4(), worker_id=bad)
    with pytest.raises(InvalidWorkerIdError):
        release_accepted_job(job_id=uuid4(), worker_id=bad)


def test_public_api_signatures_are_keyword_only_with_no_defaults() -> None:
    for fn, params in (
        (claim_next_accepted_job, {"worker_id"}),
        (start_accepted_job, {"job_id", "worker_id"}),
        (release_accepted_job, {"job_id", "worker_id"}),
    ):
        sig = inspect.signature(fn)
        assert set(sig.parameters) == params
        for name in params:
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert sig.parameters[name].default is inspect.Parameter.empty
    assert inspect.signature(start_accepted_job).parameters["job_id"].annotation == "UUID"


def test_trust_boundaries_are_private_and_unexported() -> None:
    for name in ("_claim_next_job_with_trust", "_start_job_with_trust", "_release_job_with_trust"):
        assert hasattr(JC, name)
        assert name not in JC.__all__
    for name in ("claim_next_accepted_job", "start_accepted_job", "release_accepted_job"):
        assert name in JC.__all__


def test_no_lease_heartbeat_reclaim_or_retry_surface() -> None:
    """F4 deliberately implements none of the F5+ worker-liveness concerns. Names are compared
    per identifier token (``release_accepted_job`` legitimately contains the substring
    ``lease``), so the guard cannot be satisfied or defeated by accident."""
    banned_tokens = {"lease", "leases", "heartbeat", "reclaim", "retry", "expire", "stale"}

    def _tokens(name: str) -> set[str]:
        return set(name.lower().replace(".", "_").split("_"))

    for name in JC.__all__:
        assert not (_tokens(name) & banned_tokens), name
    for name in dir(JC):
        if name.startswith("__"):
            continue
        assert not (_tokens(name) & banned_tokens), name


def test_terminal_status_helpers_are_absent() -> None:
    for banned in ("succeed_accepted_job", "fail_accepted_job", "cancel_accepted_job"):
        assert not hasattr(JC, banned)


def test_job_id_must_be_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    with pytest.raises(JC.InvalidJobTransitionError):
        JC._coerce_job_id("not-a-uuid")  # type: ignore[arg-type]
    assert JC._coerce_job_id(UUID("11111111-2222-3333-4444-555555555555"))
