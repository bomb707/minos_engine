"""Qualification engine — production code that runs Stage-gate qualification.

This package builds stage-gate evidence and artifacts. It is production code and
MUST NOT import from ``tests.*`` (enforced by an architecture test). Tests may
import these helpers; the dependency direction is one-way (tests -> production).
"""

QUALIFICATION_TOOL_VERSION = "stage0-qualifier-v2"
