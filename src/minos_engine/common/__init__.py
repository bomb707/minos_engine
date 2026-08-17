"""Common, dependency-free utilities shared across MINOS_ENGINE.

This package must not import from domain packages (protocol, intake, callers,
layer1, layer2, gates, manifests). It provides canonical serialization,
hashing, monotonic time budgets, version identities, and the error hierarchy.
"""
