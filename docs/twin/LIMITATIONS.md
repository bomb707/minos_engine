# Validator Twin — Known Limitations (Stage 1)

1. **Composite AdvancedScorer is UNAVAILABLE.** The pinned scorer formula is not
   defined in the authoritative specifications; the final/composite Minos score
   is typed-unavailable (`AUTHORITATIVE_SCORER_NOT_AVAILABLE`). No numerical
   validator parity is claimed. Resolution requires the owner/spec to provide the
   pinned scorer.
2. **No real tool execution.** GATK and hap.py are not run (plan construction,
   result parsing, and fixture replay only). Declared parity is `FIXTURE_REPLAY`,
   not `TOOL_EXECUTION`.
3. **hap.py normalization not reproduced.** The parser consumes a normalized
   synthetic raw result and records `raw_result_hash`; it does not reproduce
   hap.py's own left-align/decompose/vcfeval normalization.
4. **Live protocol integration remains fixture-backed** (Stage 0); commit-reveal
   remains typed-unavailable (owner-reported, not yet verified through the
   integrated protocol source).
5. **Layer 1 remains unimplemented; Layer 2 remains blocked.** The Twin does not
   change either.

## Conditions required before Layer 1 begins
- Twin parity + reproducibility qualified (this stage: `twin-ready.json` PASS).
- An authoritative AdvancedScorer (for eventual numerical parity) — not required
  to start Layer 1, but required before any HPO/optimization claims scores.
- Explicit user review + approval of the qualified public repository.
