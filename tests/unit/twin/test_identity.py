"""Test group B — canonical identity: repeatability, order, mutation, timestamps."""

from __future__ import annotations

from minos_engine.qualification.twin_checks import make_request
from minos_engine.twin.contracts import (
    DECLARED_PARITY_LEVEL,
    ParityExpectation,
    ParityObservation,
)
from minos_engine.twin.execution_plan import build_execution_plan
from minos_engine.twin.identities import content_hash
from minos_engine.twin.parity import assess_parity

_H = "a" * 64


def test_plan_hash_repeatable_and_order_independent():
    a = build_execution_plan(make_request({"min_pruning": 3, "max_alternate_alleles": 4}))
    b = build_execution_plan(make_request({"max_alternate_alleles": 4, "min_pruning": 3}))
    assert a.plan_hash == b.plan_hash


def test_semantic_mutation_changes_hash():
    a = build_execution_plan(make_request({"min_pruning": 3}))
    b = build_execution_plan(make_request({"min_pruning": 4}))
    assert a.plan_hash != b.plan_hash


def test_parity_report_hash_excludes_created_at():
    def report(ts: str):
        return assess_parity(
            name="p",
            expectation=ParityExpectation(name="p", expected_hash=_H),
            observation=ParityObservation(name="p", observed_hash=_H),
            declared_level=DECLARED_PARITY_LEVEL,
            created_at=ts,
        )

    a = report("2026-08-17T12:00:00+00:00")
    b = report("2030-01-01T00:00:00+00:00")
    assert a.content_hash() == b.content_hash()


def test_content_hash_exclude_helper():
    plan = build_execution_plan(make_request())
    full = content_hash(plan)
    without_hash = content_hash(plan, exclude={"plan_hash"})
    assert full != without_hash  # excluding a field changes the hash
    assert plan.plan_hash == without_hash  # plan_hash is defined over the rest
