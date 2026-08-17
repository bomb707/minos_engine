# Protocol Contracts

All contracts are frozen pydantic v2 models with `extra="forbid"`. JSON Schemas
live in `schemas/` and are validated in tests.

## RoundProtocolSnapshot (`round-protocol-snapshot-v1`)
Immutable, self-identifying snapshot of live round state + provenance.

| Field | Notes |
|---|---|
| `schema_version` | `round-protocol-snapshot-v1` |
| `snapshot_id` | `sha256` of canonical content **excluding** `snapshot_id`; auto-computed, tamper-evident |
| `retrieved_at`, `deadline_at` | timezone-aware ISO-8601 UTC |
| `round_id` | non-empty (subnet uses an ISO-8601 timestamp id) |
| `round_status` | `pending`/`open`/`scoring`/`completed`/`unknown` |
| `exact_region` | `Region` (zero-based half-open) |
| `commit_reveal_state` | explicit; `available:false` on the current platform (no crypto commit-reveal) — never fabricated |
| `parameter_ranges_raw`, `network_config_raw` | verbatim raw payloads (raw and parsed kept separate) |
| `parameter_space_hash` | 64-hex; identifies the legal-range content |
| `minos_upstream_commit`, `scorer_hash`, `gatk_image_digest`, `happy_image_digest` | **required, non-empty** — unknown ⇒ fail closed |
| `reference_sha256` | required, 64-hex |
| `stale` | explicit boolean |

Fail-closed rule: any missing required identity raises `SnapshotIncompleteError`
in `protocol.snapshot.build_snapshot`; no snapshot is produced.

## RoundContext (`round-context-v1`)
Truth-free per-round context: `round_id`, `status`, `exact_region`,
`time_remaining_seconds`, `bam_artifact`/`bai_artifact`/`reference_artifact`
(`ArtifactIdentity`), `protocol_snapshot_id`.

## ArtifactIdentity (`artifact-identity-v1`)
`uri`, `sha256` (64-hex), `size_bytes` (≥0), `media_type`,
`created_at_or_observed_at`. A filename alone is never an identity —
`build_artifact_identity` rejects `UNVERIFIED` strength.

## ParameterSpaceSnapshot (`parameter-space-snapshot-v1`)
`caller` (must be `gatk`), `parameters` (name → range), `source`,
`retrieved_at`, `parameter_space_hash`, `stale`. The hash covers caller + range
content only (fetch time excluded), so a changed range creates a *new
compatibility domain* — never a silent mutation (DYNAMIC RANGE RULE).

## Staleness (`protocol.state_sync`)
`evaluate_staleness` / `assert_usable` with a `FallbackPolicy`. A cached
snapshot is usable only when it is stale **and** the policy explicitly permits
stale use; otherwise `StaleStateError`. `now_iso` is supplied by the caller
(deterministic).

## Submission (`protocol.submission_contract`)
`build_submission_envelope(effective_config, version)` → `{tool:"gatk",
version, gatk_options}` with infra keys (`threads`, `memory_gb`, `timeout`,
`ref_build`, `num_threads`) stripped. No network I/O; the live submit call fails
closed in Stage 0.

## Clients (`protocol.client`)
`ProtocolClient` (ABC) → `FixtureProtocolClient` (deterministic, used by all
tests) and `LiveProtocolClient` (raises `UnavailableError`: the live endpoint
schema is not authoritatively available in Stage 0).
