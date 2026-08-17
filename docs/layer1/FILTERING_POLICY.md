# Read-Filter Policy (spec §8)

One shared `ReadFilterPolicy` is used by every applicable profiler. A read is
classified into exactly one bucket by a fixed priority: `unmapped → secondary →
supplementary → duplicate → qcfail → below_mapq`; the first match wins, so buckets
never overlap and `observed = included + Σ excluded`. Analysis eligibility drops
unmapped/secondary/supplementary/duplicate/qcfail reads and reads below the MQ floor
(default 0). Both raw (all observed) and analysis (eligible) views are reported.

**Coverage eligibility** is separate: mapped, non-secondary, non-supplementary,
non-qcfail primary reads (duplicates included for the duplicate-including view;
excluded for the fragment-primary view). Missing base qualities are handled
explicitly (denominator = bases with quality available). Overlapping mates are
merged before coverage events in the fragment-primary view.

The policy has a canonical `policy_hash` (`layer1-read-filter-v1`) bound into the
ContextFingerprint, so a filter change changes identity.
