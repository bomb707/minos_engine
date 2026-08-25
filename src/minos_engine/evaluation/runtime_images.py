"""Prove which local container bytes the pinned upstream scorer will actually run.

The pinned MINOS_SUBNET source references hap.py by digest and bcftools by **tag**. MINOS_ENGINE
does not rewrite that tag — doing so would change the command upstream constructs, which is
exactly the substitution the scoring-oracle architecture exists to prevent. But a tag is a moving
name, so "upstream will run bcftools 1.20" is not by itself a reproducible statement.

This module closes that gap from the outside: before any real score, it asks the local Docker
daemon what the reference *currently resolves to* and requires it to be the audited immutable
digest. Upstream still runs its own command against its own reference; MINOS_ENGINE has simply
proven, first, that the reference names the bytes the authority audited.

Deliberately narrow. It inspects and reports:

* never pulls — a scoring run must not silently fetch new bytes off the network;
* never tags, retags or removes anything;
* never runs a container;
* fixed argv, ``shell=False``, bounded timeout, minimal environment.

If the image is absent, or resolves to something else, evaluation fails closed. An operator
provisioning step is the right place to fetch an image, not a scoring call.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - fixed argv, shell=False, no caller-supplied executable
from collections.abc import Callable
from dataclasses import dataclass

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "DEFAULT_INSPECT_TIMEOUT_SECONDS",
    "LocalImage",
    "RuntimeImageAbsentError",
    "RuntimeImageContentError",
    "RuntimeImageError",
    "RuntimeImageInspector",
    "inspect_local_image",
    "verify_runtime_image",
]

#: docker inspect is a local metadata read; if it has not answered by now something is wrong.
DEFAULT_INSPECT_TIMEOUT_SECONDS = 60

#: the ONLY variables the inspect subprocess inherits.
_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "DOCKER_HOST", "LANG", "LC_ALL")


class RuntimeImageError(MinosEngineError):
    """The container the pinned scorer would run cannot be proven to be the audited one."""


class RuntimeImageAbsentError(RuntimeImageError):
    """The reference names no image on this host."""


class RuntimeImageContentError(RuntimeImageError):
    """The reference resolves locally to content the authority did not audit."""


@dataclass(frozen=True)
class LocalImage:
    """What the local daemon says a reference currently names."""

    reference: str
    image_id: str
    repo_digests: tuple[str, ...]


#: the inspection seam. Production uses :func:`inspect_local_image`; tests substitute a fake so
#: the whole verification policy is exercised without a Docker daemon.
RuntimeImageInspector = Callable[[str], LocalImage]


def inspect_local_image(
    reference: str, *, timeout_seconds: int = DEFAULT_INSPECT_TIMEOUT_SECONDS
) -> LocalImage:
    """Read one LOCAL image's identity. Never pulls, never mutates, never runs anything."""
    if not reference or reference.isspace():
        raise RuntimeImageError("an empty image reference cannot be verified")
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["docker", "image", "inspect", reference, "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeImageError(
            f"docker image inspect {reference!r} exceeded {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise RuntimeImageError(f"cannot run docker image inspect: {exc}") from exc

    if completed.returncode != 0:
        raise RuntimeImageAbsentError(
            f"{reference!r} is not present on this host; MINOS_ENGINE never pulls during "
            "scoring, so the image must be provisioned first"
        )
    try:
        document = json.loads(completed.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeImageError(f"docker image inspect {reference!r} did not return JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeImageError(f"docker image inspect {reference!r} returned {type(document)}")

    image_id = document.get("Id")
    digests = document.get("RepoDigests")
    if not isinstance(image_id, str) or not image_id:
        raise RuntimeImageError(f"docker image inspect {reference!r} reported no image Id")
    if digests is None:
        digests = []
    if not isinstance(digests, list) or any(not isinstance(item, str) for item in digests):
        raise RuntimeImageError(
            f"docker image inspect {reference!r} reported malformed RepoDigests"
        )
    return LocalImage(reference=reference, image_id=image_id, repo_digests=tuple(digests))


def verify_runtime_image(
    reference: str,
    *,
    expected_digest: str,
    label: str,
    inspector: RuntimeImageInspector | None = None,
) -> LocalImage:
    """Require ``reference`` to resolve locally to exactly ``expected_digest``.

    The comparison is on ``RepoDigests`` — the content identity as distributed — rather than the
    local image Id, because the Id is a local build artifact and two hosts can legitimately
    disagree about it while naming the same distributed content.
    """
    local = (inspector or inspect_local_image)(reference)
    if expected_digest in local.repo_digests:
        return local
    raise RuntimeImageContentError(
        f"{label} reference {reference!r} resolves locally to {list(local.repo_digests) or '[]'} "
        f"(image id {local.image_id}), but the scoring authority audited {expected_digest!r}. "
        "Scoring refuses rather than running unaudited bytes."
    )
