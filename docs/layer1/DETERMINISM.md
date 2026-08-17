# Determinism and Content Identity (spec §15, §17)

## ContextFingerprint (content identity)
The fingerprint binds only semantic identities and canonical feature values:
profile schema version, profiler algorithm version, profiler config hash, BAM
sha256, index identity/status, reference identity/status, normalized region,
sampling-plan hash, read-filter-policy hash, completed feature families,
degradation status, and a `feature_values_hash` over the measurement families.

It **excludes** machine paths, timestamps, elapsed runtime, hostnames, temp
filenames, and process/thread ids. Repeated runs over identical semantic inputs
produce the same `fingerprint_hash`; changing any semantic input changes the
appropriate identity. Verified on the real chr19 BAM: two full runs produced an
identical fingerprint.

## Serialization
`bam-profile-v1.json` and `profile-manifest-v1.json` are canonical JSON (sorted
keys, compact separators, shortest round-trip floats, NaN/Inf rejected, UTC
timestamps). Aggregation order, histogram bins, and quantile algorithm are fixed in
config. The measurement sections are byte-stable across runs; only the operational
sections (`stage_timings`, `runtime_complexity`, `degradation`) carry wall-clock
values and are excluded from content identity.

## Window Parquet
`window-profile-v1.parquet` uses a fixed, dictionary-free Arrow schema (written with
`compression=none`, statistics off). Its bytes are stable under the pinned
`pyarrow`; the manifest records its sha256. Content identity for windows is the
canonical-JSON row content inside the fingerprint, not the Parquet bytes — so
determinism tests compare the row content and fingerprint, which are environment
independent, while the Parquet file remains a faithful, pinned serialization.
