"""Shared provisioning for tests that construct a real ``SubprocessGatkRunner``.

Production requires an EXPLICIT, content-verified interpreter and an explicit ``JAVA_HOME``;
nothing is discovered through ``PATH``. Tests therefore have to provision the same three things,
and these helpers do it once so every suite provisions them identically.

The interpreter is the real ``python3`` binary this test run is using (resolved through
``realpath``, because the runner refuses a symlinked interpreter — a symlink can be re-pointed
between the check and the run). The JVM is a stub: the run boundary verifies that
``JAVA_HOME/bin/java`` exists and is executable, and only the environment-identity path ever
executes it, so a stub is honest here and a real JDK would be pretending.

Provisioning also puts that stub's ``bin`` on ``PATH``, because production does. Broad's launcher
starts a BARE ``java``, so the runner now proves the child ``PATH`` resolves it to the pinned JVM;
a fixture that pinned ``JAVA_HOME`` without provisioning the dispatch would be describing a
deployment nobody runs. ``PATH`` is restored after every test by an autouse fixture in
``tests/conftest.py``.
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
    "pin_java_dispatch",
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


def pin_java_dispatch(home: Path) -> Path:
    """Put the provisioned JVM where the launcher's bare ``java`` will resolve it. Idempotent."""
    bin_dir = str(home / "bin")
    current = os.environ.get("PATH", "")
    if bin_dir not in current.split(os.pathsep):
        os.environ["PATH"] = os.pathsep.join([bin_dir, current]) if current else bin_dir
    return home / "bin" / "java"


def runtime_kwargs(tmp_path: Path) -> dict[str, Any]:
    """The three runtime arguments every ``SubprocessGatkRunner`` construction now requires.

    Provisioning the JVM means provisioning both halves of it: the pinned identity the runner
    hashes, and the ``PATH`` entry the upstream launcher's bare ``java`` will resolve.
    """
    home = java_home(tmp_path)
    pin_java_dispatch(home)
    return {
        "launcher_python": PYTHON_INTERPRETER,
        "expected_python_sha256": hashlib.sha256(PYTHON_INTERPRETER.read_bytes()).hexdigest(),
        "java_home": home,
    }


def gatk_launcher_source(body: str) -> str:
    """Wrap a fixture launcher body as PYTHON source.

    The real GATK launcher is a Python script, so a Python fixture is the faithful stand-in: it is
    what the production boundary now executes through its explicit interpreter, and a fixture that
    were a shell script would be testing an invocation shape production no longer uses.
    """
    return body
