# Layer 2 — Architecture (L2-A)

Layer 2 is the profile-conditioned GATK configuration controller. It is **blocked**
until the accepted L1-READY prerequisite verifies. L2-A ships only the foundation:
the exact entry gate, foundational contracts, the feature-eligibility registry, and
the architecture boundaries that keep later stages honest. No PostgreSQL, storage,
candidate generation, model, or controller code exists yet, and
`Layer2Service.select_config` still raises `StageNotReadyError`.

## Modules (L2-A)
| Module | Responsibility |
|---|---|
| `layer2/prerequisites.py` | Single source of truth for accepted Layer 1 identities (pinned constants + frozen `ACCEPTED`). |
| `layer2/entry_gate.py` | Hardened L1-READY verifier (34 invariants; deterministic reason codes; fail-closed). |
| `layer2/contracts.py` | Frozen, extra-forbid foundational contracts (identities, limits, decision, feature records). |
| `layer2/feature_registry.py` | Code-owned, exhaustive, canonical feature-eligibility registry + production-selection guard. |
| `layer2/service.py` | The single production entry point `select_config`, explicitly blocked. |

## Import boundaries (enforced by `tests/leakage/`)
Layer 2 may import: `common`, the gate/qualification verification helpers, and
**typed Layer 1 contracts only** (`minos_engine.layer1.contracts`). Layer 2 must
**not** import: `pysam` or any BAM/BAI reader, Layer 1 file-opening internals
(`layer1.adapters/scan/pileup/coverage/reference_profile/orchestrator/service/…`),
`minos_engine.intake`, truth/mutation/hap.py/scoring/evaluator packages, or any
database/network dependency (`sqlalchemy`, `alembic`, `psycopg`, `requests`, …).
Pure Layer 2 domain modules open no files and never read the environment.

## Single decision path
`Layer2Service.select_config(DecisionRequest) -> DecisionResult` is the only
production CONFIG-emission entry point (`test_single_config_emission_interface`).
No second engine or parallel selection path exists. SAFE_BASELINE is the always-
available fallback once the controller is built (L2-F+).

## What is deliberately absent in L2-A
PostgreSQL/SQLAlchemy/Alembic (L2-B), the immutable 50/10/15 manifest (L2-C),
profile ingestion (L2-D), the production feature view (L2-E), candidate generation,
GATK execution, baseline discovery, model training, ranking, control modes, and
`select_config` behavior (L2-F+). See the corrected sequence in
`reports/LAYER2_PREIMPLEMENTATION_AUDIT.md`.
