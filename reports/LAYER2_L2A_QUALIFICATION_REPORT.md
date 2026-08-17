# Layer 2 — L2-A Qualification Report

**Stage:** L2-A — exact entry gate, foundational contracts, feature-eligibility
registry, and architecture enforcement. **Runtime:** CPython 3.12.x.
**This is not L2-READY.** Layer 2 remains **blocked**:
`Layer2Service.select_config` raises `StageNotReadyError`, and no controller,
storage (PostgreSQL/SQLAlchemy/Alembic), dataset manifest, ingestion, feature view,
candidate-generation, model, or ranking code exists.

## Bound source (Commit I)
```
L2-A source commit (Commit I): 71a3b12f6773edc01dcc610d16b66377c32ff66a
L2-A source tree:              130cb7e10c3308fa861e4febd20fa1a19e2a06ae
parent (owner acceptance H):   f96ea78e0943e33f751afe2eb1709512445e9437
```

Combined L2-A source hash (SHA-256 over the sorted `sha256  path` lines below):
```
fe2ca126f137614b601bb1e6b78e76a79c2587273989f175aed915fd8c2a89be
```

| SHA-256 (committed bytes) | Path |
|---|---|
| `d486caedfeea6b515e73ce57cb60bbf845eb29e9d49562ea75ab4611f09c772c` | src/minos_engine/layer2/prerequisites.py |
| `6d4c1539f2d733dcbe2822f96004497e4e1a05027110f3da49e2aca8a491d43c` | src/minos_engine/layer2/entry_gate.py |
| `001c9053125b39b83d2fcddce6f4aca7591baaaf3e8bfee56aa845a617f26469` | src/minos_engine/layer2/contracts.py |
| `97ff58c900175f311fc6de660b334ad1eba871b77120de3e0a31a5f9ed3baf72` | src/minos_engine/layer2/feature_registry.py |
| `fc29c51adbee3d0ff7c592e1b442a9a6a5f50d8fe26ae4df5a9d84f69f29fbaf` | src/minos_engine/layer2/service.py |
| `0cc7f9c109940aa8ce2b733a63a5aabeff63800dcbd32efd1a8ac041f1d20732` | src/minos_engine/layer2/__init__.py |
| `5a3d90229c95979f6a89c1146ecd88abec2390a267cd32514f40246d6c711b55` | docs/layer2/ARCHITECTURE.md |
| `f2c1be99ec71eb0e8ea4847f7056477dd50dcf56f44a4c18081a847b8884d55d` | docs/layer2/ENTRY_GATE.md |
| `8b2f3c7fb1b5fecdd992c5b510ab28a79af736e4284de1c1d15ddee8ced71250` | docs/layer2/FEATURE_REGISTRY.md |
| `0de298186adc5d2509b897988c9f4b9be1428eee4595e84f0e1bac35176a8cbd` | tests/acceptance/layer2/test_prerequisites.py |
| `ab183487804c2a12cc02a8acfe48469b91451a29fb661e5acc94de4bf1651950` | tests/acceptance/layer2/test_entry_gate_git.py |
| `fb852d5c56ab81a5878b1b487ddf6bd25fb19cdc056f1f6e29eaf2c5d8306412` | tests/acceptance/layer2/test_entry_gate_content.py |
| `5b70bafbfff4ed281ecc5380a75f4ec14ed497b1b38ed0cf56628cc7b4056937` | tests/unit/layer2/test_contracts.py |
| `80fe3491644b15391dc743513f6eaf9f79362756c4fe7f3fec6b29b46d96cdc6` | tests/unit/layer2/test_feature_registry.py |
| `27a5c9840afe323d21efe6543bbc74c48238227b40754f3182ed39e7686ebab5` | tests/acceptance/test_l1_entry_gate.py |
| `82caea6f0d58ae1145cdb156518f3be2fcbc328c144dc2e51355f093b4a84491` | tests/leakage/test_architecture_boundaries.py |

## Pinned accepted prerequisite identities (`layer2/prerequisites.py`)
```
L1-READY gate hash:      aeabfea898edd09f68dbe5662b9aebe9dc87d69c97a10b7c8fb3e9d913b5ef5b
PROTOCOL-READY gate:     b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675
TWIN-READY gate:         3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a
layer1_schema_hash:      cbb6efb28ad2c6a407c0658d0f2313df5b3cbae2cf0bbd364053aff62ac457a9
profiler_config_hash:    d01b8e7a9da8e31adad1b9cba17230506771a885256a67ec2e96c74a13c07670
profiler_version:        layer1-profiler-v1
qualified source commit: 743c9d9f203c485010db2fa683b5767187fe62b0  (tree 0d1f827d53e61d66b055d2259ee89134b721344f)
accepted artifact commit:ceadf70ba16c044a62585e7fa88bbf47fbfefae1  (tree b5d5f5cbe5ef53a59a2874d54b39460dbdbe970a)
v2 framework commit:     fe0c2d116e4e4771dbe51dbc3193b7626fa39e89
v2 evidence commit:      fa2a7696a497254fd38251072eb39a278ff24d4d
owner acceptance commit: f96ea78e0943e33f751afe2eb1709512445e9437
```
These are repository-owned constants — no environment, CLI, caller, or network
override exists. The entry-gate request carries only runtime paths.

## Entry gate
The hardened verifier proves 34 invariants (see `docs/layer2/ENTRY_GATE.md`) and
passes against the real repository (all 34 checks true). It fails closed on missing
git objects, shallow/incomplete history, divergent/sibling/rewritten/unrelated
history, tampered gate/evidence/report, non-accepted identities, and evidence path/
symlink escape, each with a deterministic machine-readable reason code.

## Feature-eligibility registry
```
registry hash:  9d597b3c2f17a2ec29215e6c38c9d791e0926e4454cbee105560e26d113eea95
records:        196  (184 Layer 1 analytical fields + 12 external FORBIDDEN sentinels)
ELIGIBLE 80 · CONDITIONAL 56 · RESEARCH_ONLY 1 · FORBIDDEN 59
```
`variant_evidence.candidate_snp_density_per_base` is RESEARCH_ONLY. FORBIDDEN and
RESEARCH_ONLY fields can never enter a production feature vector; CONDITIONAL fields
require an explicit owner promotion; a promotion can never be justified by test-set
results.

## Validation (Commit I)
- `ruff check .`: pass · `ruff format --check .`: pass · `mypy src`: pass (103 files)
- `pytest`: 558 tests, 0 failures, 0 errors, 0 skipped (68 L2-A-focused).
- Coverage: 94.27% total (threshold 90%); Layer 2 package 98%.
- Five accepted gate/check commands PASS with unchanged hashes:
  PROTOCOL-READY `b9cda0ba…`, TWIN-READY `3464fb76…`, L1-READY `aeabfea8…`
  (require-pass ×3, twin qualify --check, layer1 qualify --check).

## Blocked-state confirmations
- `Layer2Service.select_config` raises `StageNotReadyError` (single decision path).
- No PostgreSQL/SQLAlchemy/Alembic/storage code; no dataset split manifest; no
  profile ingestion; no candidate generation; no GATK execution; no baseline
  discovery; no model training; no ranking; no controller modes; no L2-READY gate.
- Accepted PROTOCOL/TWIN/L1 gate artifacts are unmodified.

## Limitations
L2-A enforces the prerequisite and the feature-eligibility contract only. Storage
(L2-B/DB-READY), the immutable 50/10/15 manifest (L2-C), profile ingestion (L2-D),
and the production feature view (L2-E) — and all later controller stages — remain
unbuilt and require explicit owner authorization per stage.
