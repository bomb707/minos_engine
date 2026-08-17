"""Layer 2 — profile-conditioned GATK controller (BLOCKED until L1-READY).

Stage 0 ships only the interface, the entry-gate verifier, and explicit blocked
behavior. Layer 2 may consume versioned Layer 1 contracts but must never open
BAM/BAI directly and must never hold submission side effects (Overall spec §6).
"""
