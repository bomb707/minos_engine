"""Independent Layer 1 acceptance-qualification framework (not production code).

The oracle here computes expected Layer 1 values from BAM/FASTA/region using
pysam and direct CIGAR/pileup traversal ONLY. It never imports the production
Layer 1 calculation modules (scan, coverage, pileup, reference_profile,
aggregators, difficulty), so agreement is genuine cross-validation.
"""
