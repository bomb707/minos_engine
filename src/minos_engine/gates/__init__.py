"""Gates — generic stage-gate contract and verifier.

A gate artifact is machine-generated evidence that a stage's mandatory checks
passed. A PASS gate cannot be constructed when any mandatory check is false or
missing (assignment §5.5).
"""
