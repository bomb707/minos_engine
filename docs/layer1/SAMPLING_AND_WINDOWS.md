# Windows and Deterministic Adaptive Sampling

## Windows (spec §3)
The exact interval is partitioned into fixed `window.primary_bp = 100000` windows;
the last may be shorter. Windows are a pure function of the normalized region and
window size — identical across runs/process restarts, never overlapping, never
overflowing the interval, stable by `window_id`.

## Sampling (spec §15)
Each window is assigned to every applicable stratum from its scan features
(uniform, boundary, low/high coverage, low MQ/BQ, clipping, NM, indel burden, low
entropy, homopolymer). Deterministic per-stratum quotas are reserved; the selected
set is deduplicated while retaining all reasons; ties are broken by
`H(BAM sha256, region, profiler version, config hash, window id)` — never by process
RNG, thread order, filesystem order, or wall-clock. Inclusion probability `πᵢ` and
analysis weight `1/πᵢ` are stored; weighted regional estimates use `Σ wᵢxᵢ / Σ wᵢ`.
The full pileup path analyzes all windows; the adaptive path analyzes the selected
subset and records the analyzed fraction and sampling uncertainty.
