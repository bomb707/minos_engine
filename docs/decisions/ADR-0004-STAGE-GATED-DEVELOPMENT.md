# ADR-0004 — Stage-gated development

## Status
Accepted (Stage 0).

## Context
The mandatory development order (assignment §2.5; Overall spec §4) is:

```
S0 Protocol foundation  -> protocol-ready.json
S1 Validator Twin       -> twin-ready.json
S2 Layer 1 implementation
S3 Layer 1 qualification -> l1-ready.json   (L1-READY gate)
S4+ Layer 2 ...
```

Layer 2 must remain blocked until Layer 1 produces a valid `l1-ready.json`. A
breaking Layer 1 schema/semantic change invalidates L1-READY and re-blocks
Layer 2.

## Decision
1. Each stage exits with a signed PASS gate artifact; a PASS gate is not
   constructible when a mandatory check is false or missing (`GateArtifact`
   invariant).
2. Stage 0 implements only architecture + protocol foundation. Layer 1
   (`Layer1Service.analyze`) raises `StageNotReadyError`. Layer 2
   (`Layer2Service.select_config`) raises `StageNotReadyError` ("blocked until
   L1-READY").
3. `layer2.entry_gate.verify_l1_ready` rejects a missing/non-PASS
   `l1-ready.json`, a missing qualification report, a Layer 1 schema-hash
   mismatch, a profiler-config-hash mismatch, or missing mandatory evidence.
4. No fake adaptive behavior is added to make blocked interfaces look complete.

## Consequences
- Layer 2 cannot consume a provisional Layer 1 schema.
- The gate chain is machine-checkable and tamper-evident (hash excludes only the
  creation timestamp).
