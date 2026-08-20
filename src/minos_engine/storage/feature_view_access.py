"""E5 production feature-view access API (fail-closed, minimal).

The smallest safe surface later Layer-2 stages use to consume the frozen production
matrices. Every operation returns a :class:`VerifiedFeatureView` whose identity was
INDEPENDENTLY verified against accepted upstream + operational reality — never a mutable
object that could redefine canonical identity.

Fail-closed guarantees (enforced by construction, not documentation):
  * a production caller cannot choose an arbitrary partition — only the two explicit
    ``open_train_feature_view`` / ``open_validation_feature_view`` entry points exist,
    and ``test`` can never be named;
  * no caller-supplied artifact path, matrix identity, feature ordering, or feature
    registry — the verifier derives the feature set internally and reconstructs identity
    from accepted evidence;
  * no external override bypassing accepted snapshot / split identity;
  * train and validation stay credential/role separated in the operational store (the
    verifier reads each partition's own artifact; enforcement of cross-identity denial is
    the partition-credential boundary proven in E3/E4).

This module intentionally exposes NO training, HPO, ranking, prediction, OOD detection,
score estimation, config generation, or ``select_config`` capability — those belong to
later stages and are out of scope for a feature view.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from minos_engine.storage.feature_view_verify import (
    VerifiedFeatureView,
    verify_feature_view,
)

__all__ = [
    "open_train_feature_view",
    "open_validation_feature_view",
    "verify_feature_view",
]


def open_train_feature_view(engine: Engine) -> VerifiedFeatureView:
    """Open + independently verify the production TRAIN feature view (epoch 1).

    Training workflows consume train only. Fails closed on any discrepancy."""
    return verify_feature_view(engine, "train")


def open_validation_feature_view(engine: Engine) -> VerifiedFeatureView:
    """Open + independently verify the production VALIDATION feature view (epoch 1).

    Evaluator/validation workflows consume validation only. Fails closed on any
    discrepancy."""
    return verify_feature_view(engine, "validation")
