# Runbook — Building a Protocol Snapshot

## Purpose
Turn raw Minos round state into an immutable, self-identifying
`RoundProtocolSnapshot` (and `RoundContext`) that downstream stages consume.

## Stage 0 (fixture-backed, deterministic)
```bash
minos-engine protocol snapshot --fixture tests/fixtures/api/valid_round.json
minos-engine protocol snapshot --fixture tests/fixtures/api/valid_round.json --json
```
Output includes `snapshot_id`, `round_id`/status, the exact region (0-based
half-open), `parameter_space_hash`, `scorer_hash`, `minos_upstream_commit`, and
`stale`.

## Fixture shape
A fixture is `{retrieved_at, source_endpoints, payload}` where `payload`
contains `round`, `artifacts`, `parameter_space`, `network_config`, and
`provenance`. See `tests/fixtures/api/valid_round.json`.

## Programmatic use
```python
from minos_engine.protocol.client import FixtureProtocolClient

client = FixtureProtocolClient("tests/fixtures/api/valid_round.json")
snapshot = client.load_snapshot()  # fails closed on missing identities
context = client.load_round_context(snapshot)
```

## Staleness check
```python
from minos_engine.protocol.state_sync import assert_usable, FallbackPolicy

assert_usable(
    snapshot, now_iso="2026-08-17T12:01:00+00:00", policy=FallbackPolicy(max_age_seconds=300)
)  # StaleStateError if stale & not allowed
```

## Live fetching
Not enabled in Stage 0. `LiveProtocolClient.fetch_raw()` raises
`UnavailableError` because the live endpoint schema is not authoritatively
available. Implement it against the real API in a later stage; keep tests on
fixtures.

## Failure modes
| Symptom | Cause | Action |
|---|---|---|
| `SnapshotIncompleteError` | a required identity/section missing | fix the source payload; do not fabricate |
| `ParameterSpaceError` | empty/non-gatk parameter space | fail closed; re-fetch legal ranges |
| `StaleStateError` | cached snapshot too old | re-fetch, or set an explicit `allow_stale` policy |
| `ProtocolError: fixture not found` | bad `--fixture` path | correct the path |
