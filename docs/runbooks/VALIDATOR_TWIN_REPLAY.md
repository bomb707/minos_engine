# Runbook — Validator Twin Replay

## Purpose
Deterministically replay a Twin evaluation from a synthetic fixture: build the
GATK execution plan, ingest a comparison result, produce scoring inputs (score
typed-unavailable), and emit an immutable run manifest. **Fixture-backed only —
not a live validator.**

## Prerequisite
A valid Stage 0 `PROTOCOL-READY` gate (`gates/protocol-ready.json`). The service
calls `require_gate_pass`; if the prerequisite is not satisfied, replay fails.

## Commands
```bash
# Build the GATK execution plan (NEVER executes GATK):
minos-engine twin plan --request tests/fixtures/twin/replay/valid.json

# Deterministic fixture replay (requires PROTOCOL-READY):
minos-engine twin replay --fixture tests/fixtures/twin/replay/valid.json --json

# Parity: compare an expectation to an observation:
minos-engine twin parity \
  --expected tests/fixtures/twin/parity/expectation.json \
  --observed tests/fixtures/twin/parity/observation_match.json

# TWIN-READY qualification (writes gate + report):
minos-engine twin qualify --json
```

## Output
`twin replay --json` emits `manifest_hash`, `plan_hash`, `comparison_hash`,
`scorer_status` (UNAVAILABLE), `declared_parity_level` (FIXTURE_REPLAY), and the
`prerequisite_gate_hash`. Comparison and score sub-artifacts are validated
against `twin-comparison-result-v1` and `twin-score-result-v1`.

## Truth isolation
Truth VCF identities appear only in offline comparison inputs. No truth content
enters production prediction features; `twin.offline` is imported only by offline
paths/tests.

## Failure modes
| Symptom | Cause | Action |
|---|---|---|
| `GateError` | PROTOCOL-READY gate missing/not PASS | run/verify Stage 0 gate first |
| `ComparisonError` | malformed/inconsistent raw comparison | fix the fixture |
| `PolicyViolationError` | non-GATK caller / hash mismatch | use gatk; align parameter-space hash |
| exit 1 on `twin parity` | expectation ≠ observation | inspect the reported differences |
