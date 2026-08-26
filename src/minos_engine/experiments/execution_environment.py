"""The identity of the RUNTIME a GATK execution actually ran under. Pure — no I/O.

The frozen plan says *what* to compute: this member, this CONFIG. It deliberately says nothing
about the machinery that computes it. That gap is what let five Phase-A jobs be recorded as
candidate failures when the truth was that GATK never started: the pinned launcher is a
``#!/usr/bin/env python`` script, no ``python`` existed on the worker's PATH, and ``env`` exited
127 before a single argument was parsed. Same launcher bytes, same JAR, same BAM, same CONFIG —
different result, decided by an ambient shell variable.

This module makes that machinery an identity. It binds what is reproducible about a runtime:

* the GATK launcher and the JAR it dispatches to, plus the version they report;
* the interpreter that actually executes the launcher, by content and by version;
* the ``java`` binary the JVM starts from, by content and by version;
* the version of the child-environment POLICY the run was made under.

and excludes everything host-specific — absolute paths, hostname, PID, worker id, timestamps,
the literal ``PATH``, any database URL. Two hosts with the same bundle, interpreter, JVM and
policy therefore produce the same ``execution_environment_hash``; the same host with a different
interpreter does not.

It is NOT part of the scientific plan. ``member x CONFIG`` remains the scientific question and
the Phase-A plan hash, job key and candidate identities are untouched by anything here.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "CHILD_ENVIRONMENT_POLICY_VERSION",
    "EXECUTION_ENVIRONMENT_DOMAIN",
    "EXECUTION_ENVIRONMENT_SCHEMA",
    "GatkExecutionEnvironment",
    "compute_execution_environment_hash",
]

EXECUTION_ENVIRONMENT_SCHEMA = "l2f-gatk-execution-environment-v1"
EXECUTION_ENVIRONMENT_DOMAIN = "minos:l2f-gatk-execution-environment:v1\n"

#: the version of the child-environment POLICY, not of any host's environment. ``v2`` is the
#: corrected policy: the launcher is invoked through an explicitly provisioned, content-verified
#: interpreter (never its shebang and never PATH), ``JAVA_HOME`` is explicitly provisioned and
#: content-verified, the child inherits only the allowlist, no JAR-override variable can reach it,
#: ``shell=False``, and the launcher and JAR are pinned by content. ``v1`` was the ambient-PATH
#: policy under which a worker without a ``python`` command silently produced candidate failures.
CHILD_ENVIRONMENT_POLICY_VERSION = "l2f-gatk-child-env-v2"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _validate_hex64(v: str) -> str:
    if not _HEX64.fullmatch(v):
        raise ValueError("must be a lowercase 64-character hex string")
    return v


Hex64 = Annotated[str, AfterValidator(_validate_hex64)]
_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)


class GatkExecutionEnvironment(BaseModel):
    """Everything about the runtime that can change a result, and nothing about the host."""

    model_config = _STRICT

    schema_version: Literal["l2f-gatk-execution-environment-v1"] = (
        "l2f-gatk-execution-environment-v1"
    )
    #: the launcher script alone — a dispatcher, not the scientific payload.
    gatk_launcher_sha256: Hex64
    #: launcher + the local JAR it actually runs + the version: the scientific payload identity.
    gatk_runtime_bundle_sha256: Hex64
    gatk_version: str = Field(min_length=1)
    #: the interpreter that executes the launcher. This is the field whose absence caused the
    #: contaminated campaign: with no ``python`` on PATH the launcher could not start at all.
    launcher_python_sha256: Hex64
    launcher_python_version: str = Field(min_length=1)
    java_sha256: Hex64
    java_version: str = Field(min_length=1)
    child_environment_policy_version: Literal["l2f-gatk-child-env-v2"] = "l2f-gatk-child-env-v2"

    def environment_hash(self) -> str:
        return compute_execution_environment_hash(self)


def compute_execution_environment_hash(environment: GatkExecutionEnvironment) -> str:
    """The domain-separated, host-independent identity of one execution runtime."""
    content = {
        "schema_version": environment.schema_version,
        "gatk_launcher_sha256": environment.gatk_launcher_sha256,
        "gatk_runtime_bundle_sha256": environment.gatk_runtime_bundle_sha256,
        "gatk_version": environment.gatk_version,
        "launcher_python_sha256": environment.launcher_python_sha256,
        "launcher_python_version": environment.launcher_python_version,
        "java_sha256": environment.java_sha256,
        "java_version": environment.java_version,
        "child_environment_policy_version": environment.child_environment_policy_version,
    }
    return sha256_hex(EXECUTION_ENVIRONMENT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))
