# Validator Twin — Parity Levels

The specifications define gate states (PASS/HOLD/PATCH/REJECT) and "byte-level or
declared-semantic" output comparison but no named parity ladder. Stage 1
introduces a non-conflicting ladder. **A lower level is never reported as a
higher level** (enforced by `TwinParityReport.declared_level` + tests).

| Level | Meaning | Stage 1 |
|---|---|---|
| `STRUCTURAL` | contracts, execution plan, and content identities reproduced deterministically | achieved |
| `FIXTURE_REPLAY` | + deterministic replay of comparison results and recomputed metrics via injected adapters | **declared/achieved** |
| `TOOL_EXECUTION` | + real GATK + hap.py executed in a resource-capped container | not achieved (out of Stage 1 scope) |
| `VALIDATOR_CONFIRMED` | + pinned AdvancedScorer numerical parity confirmed vs the live validator | not achieved (scorer formula unavailable) |

`DECLARED_PARITY_LEVEL = FIXTURE_REPLAY`. The TWIN-READY gate records the declared
level; `scoring_matches_declared_level` asserts that, because the level is
`FIXTURE_REPLAY`, the composite score is returned UNAVAILABLE — no numerical
validator parity is claimed.

## Why not higher
- `TOOL_EXECUTION`: Stage 1 does not run real GATK/hap.py (plan + parse + replay
  only). Runner ports exist for a later stage.
- `VALIDATOR_CONFIRMED`: the pinned AdvancedScorer formula/weights are not defined
  in the authoritative specifications (see `SCORING_CONTRACT.md`).
