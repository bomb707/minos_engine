# Layer 1 Architecture — truth-free BAM/reference profiling

Layer 1 converts `(BAM, BAI, region, FASTA, FAI, budget)` into a deterministic,
immutable, **truth-free** `ProfileResult` plus three artifacts. It never selects a
GATK CONFIG, runs GATK/hap.py, reads truth/hidden/winner data, optimizes, or
imports Layer 2 or the Twin.

## Module → responsibility map
The Layer 1 spec §18 lists a fine-grained module set; this implementation merges
them into a coherent set (prompt §6 permits merging) under `src/minos_engine/layer1/`:

| Module | Responsibility (spec module folded in) |
|---|---|
| `contracts.py` | All frozen contracts + validation (contracts, region types) |
| `config.py` | Config load + identity hash |
| `region.py` | Step 1 exact region resolution |
| `validation.py` | Step 2–3 integrity + header profiler |
| `adapters/pysam_adapter.py` | The single BAM/FASTA I/O boundary |
| `windows.py` | Deterministic window partition |
| `filters.py` | One shared read-filter policy |
| `aggregators.py` | Welford, fixed histograms, quantiles, MAD |
| `scan.py` | Step 4 one-pass scan (read/cigar/pairing profilers) + per-window accumulators |
| `coverage.py` | Difference-array coverage (two views) |
| `reference_profile.py` | Reference-context profiler |
| `pileup.py` | Pileup policy + per-position evidence + aggregation |
| `cost_model.py` | Full-vs-adaptive runtime estimator |
| `sampling.py` | Deterministic adaptive window sampling |
| `difficulty.py` | Difficulty vector + confidence + completion |
| `fingerprint.py` | Deterministic ContextFingerprint |
| `serializer.py` | Atomic canonical JSON + Parquet + manifest |
| `orchestrator.py` | Deadline/degradation state machine (only sequencer) |
| `service.py` | Stable public `analyze`/`profile` API (DI) |

## Workflow
TWIN-READY verification → 3.12 runtime preflight → request validation →
BAM/index/header validation → reference/region validation → deterministic window +
sampling plan → shared read filtering → alignment/quality/fragment/CIGAR profiling →
coverage profiling → bounded pileup/evidence profiling → reference profiling →
context fingerprint → ProfileResult validation → atomic write.

`Layer1Service.analyze()` is the single production entry point. The filesystem
resolver, pysam adapter, monotonic clock, and prerequisite gate verifier are all
injected. No network access or dataset download is hidden in the service.
