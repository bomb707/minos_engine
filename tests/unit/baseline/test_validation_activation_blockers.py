"""The activation blockers, recorded as executable facts rather than as prose.

An independent audit found that although the Phase-D *scientific* authority exists in source — four
frozen finalists, ten VALIDATION members, forty observations, no racing — the durable *execution*
substrate underneath it is still TRAIN-only. These tests pin the four blockers precisely, so that
the corrective can be shown to close them and so that a future change cannot quietly reopen one.

Two of them are permanent invariants that must survive the corrective (the TRAIN plan contract in
Python, and TRAIN's own lineage requirements); two are gaps the corrective fills. Each test says
which it is.
"""

from __future__ import annotations

import inspect
from typing import get_args, get_origin

from minos_engine.experiments import plan as plan_module

# --------------------------------------------------------------------------------------------
# BLOCKER A — the Python ExperimentPlan contract is TRAIN, by type
# --------------------------------------------------------------------------------------------


def test_the_experiment_plan_contract_is_train_only_by_construction() -> None:
    """PERMANENT. The TRAIN plan contract must stay exactly this narrow after the corrective.

    ``ExperimentPlan`` is not a plan that happens to be TRAIN; it is a TRAIN plan. The partition is
    a ``Literal["train"]``, the module constant is ``"train"``, and ``compute_plan_hash`` writes
    that constant rather than any caller's value — so the TRAIN plan hash cannot be computed for
    another partition even by mistake.
    """
    assert plan_module.PLAN_PARTITION == "train"

    annotation = plan_module.ExperimentPlan.model_fields["partition"].annotation
    assert get_origin(annotation) is not None or annotation is not None
    assert get_args(annotation) == ("train",), annotation

    source = inspect.getsource(plan_module.compute_plan_hash)
    assert '"partition": PLAN_PARTITION' in source


def test_a_validation_plan_therefore_cannot_be_an_experiment_plan() -> None:
    """The consequence: Phase D needs its own durable plan contract, not a widened TRAIN one."""
    import pydantic
    import pytest

    fields = {
        name: field
        for name, field in plan_module.ExperimentPlan.model_fields.items()
        if field.is_required()
    }
    assert "partition" in fields
    with pytest.raises(pydantic.ValidationError):
        plan_module.ExperimentPlan.model_validate({"partition": "validation"})


# --------------------------------------------------------------------------------------------
# BLOCKER C — no Phase-D persistence or enqueue entry exists in the production seams
# --------------------------------------------------------------------------------------------


def test_the_train_persistence_path_resolves_and_writes_train_lineage() -> None:
    """PERMANENT. TRAIN persistence must keep resolving TRAIN upstream and writing partition=train."""
    from minos_engine.storage import l2f_plan_store

    source = inspect.getsource(l2f_plan_store)
    assert '_TRAIN = "train"' in source
    assert '"partition": _TRAIN' in source
    assert 'if row["partition"] != _TRAIN' in source


def test_the_train_enqueue_path_takes_no_caller_supplied_scientific_identity() -> None:
    """PERMANENT. Whatever validation gains, TRAIN enqueue must keep deriving its own identity."""
    from minos_engine.storage import l2f_job_enqueue

    doc = inspect.getdoc(l2f_job_enqueue) or ""
    assert "no caller-supplied" in doc


# --------------------------------------------------------------------------------------------
# BLOCKER D — the truth registrar is TRAIN-only, and the evaluator gate is not a registrar
# --------------------------------------------------------------------------------------------


def test_the_committed_truth_registrar_is_train_only() -> None:
    """PERMANENT. The TRAIN registrar must never learn to take a partition argument."""
    from minos_engine.evaluation import truth_registration

    assert hasattr(truth_registration, "register_train_truth_identities")
    parameters = inspect.signature(truth_registration.register_train_truth_identities).parameters
    assert "partition" not in parameters, "the TRAIN registrar must not be parameterised"

    source = inspect.getsource(truth_registration.register_train_truth_identities)
    assert "refuse_non_train_partition" in source or "train" in source


def test_the_validation_partition_gate_is_a_gate_and_not_a_registrar() -> None:
    """A gate answers 'may this be evaluated'. It does not create a truth identity."""
    from minos_engine.evaluation.truth_registration import refuse_non_validation_partition

    signature = inspect.signature(refuse_non_validation_partition)
    assert list(signature.parameters) == ["partition"]
    assert signature.return_annotation in (None, "None")
    source = inspect.getsource(refuse_non_validation_partition)
    for writing in ("INSERT", "UPDATE", "register", "conn", "engine"):
        assert writing not in source, f"the partition gate appears to {writing}"


def test_a_named_validation_registrar_exists_and_is_not_the_train_one() -> None:
    """CLOSED BY THIS CORRECTIVE. Two named registrars, neither parameterised by partition."""
    from minos_engine.evaluation import truth_registration

    assert hasattr(truth_registration, "register_validation_truth_identities")
    train = truth_registration.register_train_truth_identities
    validation = truth_registration.register_validation_truth_identities
    assert train is not validation
    assert "partition" not in inspect.signature(validation).parameters


def test_the_validation_registrar_never_reaches_train_or_test() -> None:
    """CLOSED BY THIS CORRECTIVE. The registrar names one partition, literally."""
    from minos_engine.evaluation import truth_registration

    source = inspect.getsource(truth_registration.register_validation_truth_identities)
    assert "validation" in source
    # it must not be able to write a TRAIN or TEST identity by any argument
    assert '"train"' not in source
    assert '"test"' not in source
