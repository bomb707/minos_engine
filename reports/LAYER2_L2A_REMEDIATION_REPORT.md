# Layer 2 — L2-A Remediation Report

**Runtime:** CPython 3.12.x. **Not L2-READY.** Layer 2 remains **blocked**
(`Layer2Service.select_config` raises `StageNotReadyError`); no storage, optimizer,
dataset manifest, ingestion, experiment, model, or controller code exists. This
report supplements (does not replace) `reports/LAYER2_L2A_QUALIFICATION_REPORT.md`.

## Defects and root causes
- **Defect 1 — registry not scalar-exhaustive.** Root cause: the original registry
  classified structured *container* paths (e.g. `mapping_quality.quantiles`) as if
  they were features and never expanded them to the concrete scalar leaves a
  production vector carries (e.g. `mapping_quality.quantiles.P50`); `state_for`
  therefore rejected real leaves as unknown. **Fix:** config-bound dynamic maps are
  expanded to concrete scalar leaves derived from `configs/layer1/default.yaml` and
  bound to the accepted profiler-config hash; data-dependent maps remain
  documentation containers (`model_feature = False`).
- **Defect 2 — caller-forged promotions.** Root cause: `assert_production_feature_vector`
  accepted a caller-supplied `promotions` mapping whose `PromotionRecord` (an
  arbitrary Pydantic object with a free-text `approved_by`) could authorize a
  CONDITIONAL field. **Fix:** the promotions parameter is removed; CONDITIONAL and
  RESEARCH_ONLY are always rejected; `PromotionRecord` is descriptive only.
- **Defect 3 — optional mandatory identities.** Root cause: `Layer1ProfileReference`
  declared `bai_sha256`/`reference_sha256`/`fai_sha256` as `str | None`. **Fix:** all
  eight identities are mandatory 64-hex lowercase values (fail closed on
  missing/empty/uppercase/malformed) and an `identity_tuple_hash` is derived.

## Bound source (Commit K)
```
L2-A remediation source commit (Commit K): a4637d8d6abc35787d231d8d00b86e4603c904d0
L2-A remediation source tree:              8dd292d5014ccd73904673314d065ba7dad3627b
parent (Commit J):                         1cbccb4bffbd70288d866e1b653e4c7c16157d42
```

| SHA-256 (committed bytes at Commit K) | Path |
|---|---|
| `3270078f100d48916b3c3614eeb97b34b3e4734ea4a57ec9562fcf2dd149b37e` | src/minos_engine/layer2/contracts.py |
| `e5a8deb47b903e2316084e7d719d4cb3e10c57f9bcfb58fff7e0870c0eb43724` | src/minos_engine/layer2/entry_gate.py |
| `cf5ee45811b0782991d080fd52ecafaeeac5b0f24084baac31c43883296733c0` | src/minos_engine/layer2/feature_registry.py |
| `d486caedfeea6b515e73ce57cb60bbf845eb29e9d49562ea75ab4611f09c772c` | src/minos_engine/layer2/prerequisites.py |
| `fc29c51adbee3d0ff7c592e1b442a9a6a5f50d8fe26ae4df5a9d84f69f29fbaf` | src/minos_engine/layer2/service.py |
| `17b6638eb0afbd41cd8d0477ac2a4cabf18a03d71c49e49ecfcf7005a7898ac2` | schemas/layer2-feature-reconciliation-v1.schema.json |
| `7cd89f640c2d6079d03911ea9f5e1db51c1e907062cdc3a227618ac23234bda7` | scripts/qualification/layer2_feature_reconciliation.py |
| `3db626d4224b9654d45ec0e947cc9d986861e193d50ce49cbce7717b0d470d30` | docs/layer2/ENTRY_GATE.md |
| `20ce704c9f351d2f9cbf9db537d011c607d58d73fd01e63b6b24f0bd1642478c` | docs/layer2/FEATURE_REGISTRY.md |
| `139a4822de01b6fa77ac35719a4540383d2258443e1058d3f9e0178dbd941d9b` | tests/unit/layer2/test_feature_registry.py |
| `030b63dfe79cfa8649c5054cec4e0cf761229366aec0e88bfb32ea29b72f37f7` | tests/unit/layer2/test_contracts.py |
| `3d21f431b762f0124536e5e49f623529223f6f4f293c13029baa60dc61b74cc1` | tests/unit/layer2/test_reconciliation.py |
| `f604d8d4db34eea33082741cac075ab9d167f0504c1140cb4ff1c4cf88ffd387` | tests/acceptance/layer2/test_profile_identity.py |
| `e8b64fafcb024f148670358a163a34a65bc341ee56968a98195668fb467a64e3` | tests/acceptance/layer2/test_promotion_security.py |
| `38c9c375909ceddabeee0101303511da7ed30497238728d07e8d55d7769ff574` | tests/acceptance/layer2/test_entry_gate_content.py |
| `1825a0f25ae38490286c6cace8c061c3a387e9e1209c4e896c194a4581e693be` | tests/acceptance/test_l1_entry_gate.py |

## Feature registry (remediated)
```
registry hash: 0d8612707c6673060546511d8f5e8d1ba47048ef440e6c2dcf238fdc297f6e0c
records: 285   ELIGIBLE 147 · CONDITIONAL 60 · RESEARCH_ONLY 2 · FORBIDDEN 76
value kinds: REAL 103 · FRACTION 55 · IDENTIFIER 55 · COUNT 43 · CONTAINER 21 · OPERATIONAL 5 · BOOL 2 · CATEGORICAL 1
scalar model features 198 (bam-profile 184 + window 14); containers 21
```
Reconciliation (`reports/LAYER2_FEATURE_REGISTRY_RECONCILIATION.json`,
schema-validated, sha256
`3bdf544be4b368beec62681656ac6217f1e24b3fdcf20bf2eb29b76be3fd5483`):
missing = 0, duplicate = 0, unclassified = 0, unknown = 0,
non-scalar-model-feature = 0 → **PASS**.

## Validation (Commit K)
- `ruff check .`: pass · `ruff format --check .`: pass · `mypy src`: pass (103 files)
- `pytest`: 601 tests, 0 failures, 0 errors, 0 skipped (111 L2-A-focused).
- Coverage: 94% total (threshold 90%); Layer 2 package 96%.
- Five accepted gate checks PASS, hashes unchanged: PROTOCOL-READY `b9cda0ba…`,
  TWIN-READY `3464fb76…`, L1-READY `aeabfea8…`.

## Exit criteria (all satisfied)
missing/duplicate/unclassified analytical scalar paths = 0; non-scalar
model-feature paths = 0; caller-authorized promotions impossible; mandatory profile
identities enforced; entry-path escape tests pass; service remains blocked; all
tests pass; coverage ≥ 90%; ruff/format/mypy pass; all existing gates pass; accepted
gate hashes unchanged; no database/optimizer code; no 50/10/15 manifest.

## Remaining limitations
L2-A enforces the prerequisite, the scalar feature-eligibility contract, and input
identity only. The homopolymer histogram and stratum window counts are documented as
data-dependent containers (not fixed features). Storage/DB-READY (L2-B), the
immutable 50/10/15 manifest (L2-C), profile ingestion (L2-D), the production feature
view (L2-E), a real hash/git-bound promotion mechanism, and all later controller
stages remain unbuilt and require explicit owner authorization per stage.
