# Performance (spec §19–§20)

Budgets (config, spec §3): soft target **180 s**, hard limit **300 s**, pileup soft
**90 s**, serialization reserve **10 s**. A single monotonic `Deadline` is shared by
all stages and checked at bounded work units.

## Bounded memory
Integer counters, Welford mean/variance, fixed integer histograms, a deterministic
quantile/MAD sketch, per-window accumulators, and `int64` coverage difference arrays.
Reads and bases are never all retained; the mate-overlap map is evicted by position.

## Cost model (spec §14)
`pred_seconds = exp(b0 + b1·log(region_bp) + b2·log(reads+1) + b3·mean_depth +
b4·max_depth_proxy + b5·clipping_rate + b6·cigar_complexity)`. FULL pileup is chosen
only when `pred ≤ min(pileup_soft, remaining − serialization_reserve)`; otherwise the
deterministic adaptive path is used. Coefficients are conservative and versioned;
calibration on the development BAMs is deferred (see the audit).

## Measured (real chr19 BAM, ~9.9 Mbp, 1.57M reads)
Cold and warm runs each completed in **~102 s** (< 180 s soft, < 300 s hard) with
peak RSS **~630 MB**, ADAPTIVE pileup, and identical fingerprints across runs.
Qualification fails if the hard limit is exceeded. The real-BAM region is
protocol-scale (not an unrealistically tiny window).
