# ADR-0003 — Truth isolation boundary

## Status
Accepted (Stage 0).

## Context
Live engine code must never access `truth.vcf.gz`, `mutations.vcf.gz`, confident
truth BED, hap.py results, `AdvancedScorer` results, the hidden score, the
leaderboard, or a previous winning CONFIG as a direct lookup (Overall spec §FORBID;
assignment §2.3). Truth-enabled evaluation exists only in a separate offline
boundary (the Validator Twin, a later stage). The local memory also records that
locked-test truth (`FINAL_TEST_*`) is burned/exposed and must fail closed.

## Decision
1. Stage 0 ships **no** evaluator, scorer, hap.py, truth, or mutation code and
   adds none of those runtime dependencies.
2. A leakage test statically scans the source tree (AST-based, excluding
   docstrings/comments) and fails if any module imports a truth/evaluation
   module or references a truth data-file path.
3. `configs/engine/default.yaml` sets `truth_isolation.enabled: true`, asserted
   by tests.
4. Layer 1 (later) will read BAM/reference only and must never import
   evaluation/truth/Layer 2 — enforced by architecture tests already present.
5. Provenance identities (scorer hash, image digests) are treated as *runtime
   snapshot data*, not truth; they are hashes/identifiers, never truth content.

## Consequences
- The dependency boundary is established structurally now, before the Validator
  Twin exists.
- No truth or locked-test data was read during Stage 0 development.
