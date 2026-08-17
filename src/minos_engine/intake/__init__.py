"""Intake — round-artifact identities and reference resolution.

Intake owns *what we are operating on*: the exact region representation, the
content identity of BAM/BAI/reference artifacts, and reference-genome
resolution. It never opens genomic files in Stage 0 and never touches truth.
"""
