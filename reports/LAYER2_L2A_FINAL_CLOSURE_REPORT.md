# Layer 2 — L2-A Final Closure Report

**Runtime:** CPython 3.12.3. **Not L2-READY.** Layer 2 remains **blocked**
(`Layer2Service.select_config` raises `StageNotReadyError`); no PostgreSQL,
SQLAlchemy, Alembic, dataset manifest, ingestion, Optuna/SMAC, experiment harness,
model, or controller code exists. This closes the single remaining L2-A validation
defect (COUNT value semantics) and hardens `CanonicalFeatureVector`.

## Defect and fix
`validate_production_feature_mapping` accepted fractional values for COUNT fields
because it float-coerced the input and only checked `val < 0`, so `1.5`/`0.1`/`3.999`
passed. **Fix:** a documented per-kind validator `validate_scalar_value` enforces:
- **COUNT** — built-in `int`, not `bool`, `0 <= v <= 2**53` (exactly representable);
  floats (including integral `1.0`), `str`, `Decimal`, NumPy scalars, and `None` are
  rejected; conversion to float is lossless and never rounded.
- **REAL/FRACTION** — built-in `int` or `float` (never `bool`), finite (no
  NaN/Infinity), integers within `±2**53`; FRACTION additionally in `[0.0, 1.0]`.

`CanonicalFeatureVector` gains a before-validator rejecting bool/NaN/Infinity/
non-numeric values, complementing its existing sorted/unique-field, length-match,
registry-hash-shape, and vector-hash-binding checks, so no invalid vector can be
constructed directly to bypass the mapping validator.

## Bound source (Commit M)
```
Commit M SHA:  c2ceed0cd8566442ca229eaa41d9a096c0b4ccea
Commit M tree: e581ff76223210895ceff1521dabba751de72f9a
parent (N):    9a2b347ecf14209106a67a6d199fa06377e1cebf
```

| SHA-256 (committed bytes at Commit M) | Path |
|---|---|
| `d4c23158952bd1356e1b1941dd070e92fee818db1eda26a60664ffca5b33aa0d` | src/minos_engine/layer2/contracts.py |
| `4a52e0272567c1b1e25a3493197be5899d3ecfd0463ae254672f1784b5dcd1e4` | src/minos_engine/layer2/feature_registry.py |
| `02d7d62fe4d888bb170c9228e1ccc39f8ff32dfd4ad7c6940d4410d0aa5bb446` | tests/unit/layer2/test_count_validation.py |
| `0b055c3bf6cc4af83cd515aed0c30475eb825e29ad5d80e857de81255f7fff7e` | docs/layer2/FEATURE_REGISTRY.md |

## Validation (Commit M, CPython 3.12.3)
- `ruff check .`: pass · `ruff format --check .`: pass (258 files) · `mypy src`: pass (103 files)
- `pytest`: 658 tests, 0 failures, 0 errors, 0 skipped (57 in `test_count_validation`).
- `pytest --cov=src/minos_engine --cov-fail-under=90`: **94%** (threshold met).

## COUNT boundary test results
- **Accepted:** `0`, `1`, `42`, `2**53` (exact, lossless float conversion; deterministic).
- **Rejected:** `True`, `False`, `-1`, `0.0`, `1.0`, `1.5`, `-0.5`, `2**53 + 1`, `"1"`,
  `None`, NaN, `+inf`, `-inf`, `Decimal("1")`.
- **REAL/FRACTION regression:** int and float REAL accepted; NaN/Infinity/bool/str
  rejected; FRACTION boundaries `0`, `0.0`, `1`, `1.0` accepted; `<0`/`>1` rejected;
  bool rejected for every numeric kind.

## CanonicalFeatureVector hardening results
Direct construction rejects: NaN, `+inf`/`-inf`, bool, duplicate fields, unsorted
fields, field/value length mismatch, malformed registry hash, and an incorrect
supplied vector hash. Mapping input order does not change fields, values, or
`vector_hash`; changing a field or a value changes `vector_hash`; COUNT conversion is
deterministic and exact within `[0, 2**53]`.

## Reconciliation (unchanged)
`reports/LAYER2_FEATURE_REGISTRY_RECONCILIATION.json` re-generates **byte-identical**
to the committed artifact — this was a runtime-validation-only change:
missing = 0, duplicate = 0, unclassified = 0, unknown = 0,
non-scalar-model-feature = 0.
Registry hash unchanged: `0d8612707c6673060546511d8f5e8d1ba47048ef440e6c2dcf238fdc297f6e0c`.

## Five accepted gate checks (unchanged hashes)
| Check | Result | Hash |
|---|---|---|
| PROTOCOL-READY require-pass | PASS | `b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675` |
| TWIN-READY require-pass | PASS | `3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a` |
| L1-READY require-pass | PASS | `aeabfea898edd09f68dbe5662b9aebe9dc87d69c97a10b7c8fb3e9d913b5ef5b` |
| TWIN qualify --check | PASS (ok) | `3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a` |
| L1-READY qualify --check | PASS (ok) | `aeabfea898edd09f68dbe5662b9aebe9dc87d69c97a10b7c8fb3e9d913b5ef5b` |

## Confirmations
- No L2-B code added (no PostgreSQL/SQLAlchemy/Alembic/Optuna/SMAC/ingestion/
  experiment/model/controller).
- No 50/10/15 dataset split manifest created.
- `Layer2Service.select_config` remains blocked (`StageNotReadyError`).
- No accepted gate artifact or accepted identity modified; registry identity unchanged.
