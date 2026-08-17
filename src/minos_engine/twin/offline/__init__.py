"""Explicitly OFFLINE Twin namespace — the ONLY place truth loading may occur.

Truth / comparison-truth artifacts may be loaded here for offline practice
evaluation. Nothing in the production / live path (protocol, callers, layer1,
layer2, intake, manifests) may import this package. An architecture test fails
if a prohibited import appears, and a leakage test proves a truth sentinel
cannot reach any production contract.
"""
