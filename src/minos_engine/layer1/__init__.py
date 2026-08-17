"""Layer 1 — truth-free BAM measurement (NOT implemented in Stage 0).

Only the stable public interface and explicit not-ready behavior exist here.
Layer 1 must never import truth, scores, evaluation, hap.py, Layer 2, or
historical winners (Layer 1 spec BOUNDARY). Those imports are absent by
construction and enforced by architecture tests.
"""
