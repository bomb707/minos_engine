"""Shared provisioning for tests that construct a real ``SubprocessGatkRunner``.

Production requires an EXPLICIT, content-verified interpreter and an explicit ``JAVA_HOME``;
nothing is discovered through ``PATH``. Tests therefore have to provision the same three things,
and these helpers do it once so every suite provisions them identically.

The interpreter is the real ``python3`` binary this test run is using (resolved through
``realpath``, because the runner refuses a symlinked interpreter — a symlink can be re-pointed
between the check and the run). The JVM is a stub: the run boundary verifies that
``JAVA_HOME/bin/java`` exists and is executable, and only the environment-identity path ever
executes it, so a stub is honest here and a real JDK would be pretending.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "PYTHON_INTERPRETER",
    "gatk_launcher_source",
    "java_home",
    "runtime_kwargs",
]

#: the REAL interpreter, with symlinks resolved.
PYTHON_INTERPRETER = Path(os.path.realpath(sys.executable))


def java_home(tmp_path: Path) -> Path:
    """A provisioned JAVA_HOME whose ``bin/java`` exists and is executable."""
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True, exist_ok=True)
    java = home / "bin" / "java"
    if not java.exists():
        java.write_text("#!/bin/sh\necho 'openjdk version \"17.0.11\"' 1>&2\n", encoding="utf-8")
        java.chmod(0o755)
    return home


def runtime_kwargs(tmp_path: Path) -> dict[str, Any]:
    """The three runtime arguments every ``SubprocessGatkRunner`` construction now requires."""
    return {
        "launcher_python": PYTHON_INTERPRETER,
        "expected_python_sha256": hashlib.sha256(PYTHON_INTERPRETER.read_bytes()).hexdigest(),
        "java_home": java_home(tmp_path),
    }


def gatk_launcher_source(body: str) -> str:
    """Wrap a fixture launcher body as PYTHON source.

    The real GATK launcher is a Python script, so a Python fixture is the faithful stand-in: it is
    what the production boundary now executes through its explicit interpreter, and a fixture that
    were a shell script would be testing an invocation shape production no longer uses.
    """
    return body
