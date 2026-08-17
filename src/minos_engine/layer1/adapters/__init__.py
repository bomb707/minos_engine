"""Layer 1 I/O adapters. The pysam adapter is the single BAM/FASTA I/O boundary.

It opens only the explicit BAM/BAI/FASTA/FAI paths it is given and never
enumerates a directory, so truth/mutation files that sit beside a round's BAM can
never be discovered or read through Layer 1.
"""
