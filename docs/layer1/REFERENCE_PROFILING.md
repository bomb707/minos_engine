# Reference Profiling (spec §11–§12)

Reference **validation** (identity/compatibility: contig present, BAM/FASTA lengths
equal, region in bounds) is distinct from reference **profiling** (measurable
sequence properties of the target interval).

Profiled from the FASTA interval sequence: `gc_fraction = (G+C)/(A+C+G+T)`,
`n_fraction = non-ACGT / length`, Shannon `entropy_bits = −Σ p_b log2 p_b` over
ACGT, homopolymer density + run-length histogram (runs ≥ `homopolymer_min_run`), and
a dinucleotide-repeat fraction (repeated 2-mers ≥ `dinucleotide_min_run` units).

FASTA-derived indicators are **not** official repeat-mask or mappability tracks and
are never labelled as such. If no reference sequence is available the result is
marked unavailable; nothing is inferred from the reads (no BAM-consensus GC).
