"""Post-mint immutability for verified capabilities.

**The problem this closes.** A capability is minted by a verifier, sealed with a module-private
token, and then handed downstream. The seal attests to the state the verifier actually checked --
but nothing stopped that state from being replaced afterwards. A legitimately loaded
``VerifiedRoundProfileAuthority`` accepted ``authority.partition = "live"`` and
``authority._by_round["fake"] = ...`` while still answering True to its own seal check, so the
seal certified a document that no longer existed.

Freezing has to reach nested containers too. Refusing ``authority.anchors = {...}`` while handing
out the live internal dict from a property is not immutability, it is a longer path to the same
mutation.

**The trust model this is inside.** See ``docs/layer2/L2H_CAPABILITY_TRUST_MODEL.md``. These are
not cryptographic guarantees against hostile code running in this interpreter: such code can
import underscore-prefixed globals, call ``object.__new__``, monkeypatch modules and write through
``object.__setattr__``. What is claimed is that *normal production code using supported APIs*,
*internal code that is wrong rather than malicious*, and *subclass or mutable-state misuse* cannot
alter the state a seal attests to. That is the class of defect these primitives exist to make
impossible, and it is the class that has actually occurred here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "FrozenAfterMint",
    "deep_plain",
    "frozen_deep",
    "frozen_map",
]


def frozen_map[V](mapping: Mapping[str, V]) -> Mapping[str, V]:
    """A read-only view over a defensive copy. Neither the caller's dict nor this one can write."""
    return MappingProxyType(dict(mapping))


def frozen_deep(value: Any) -> Any:
    """Recursively freeze a JSON-shaped value: mappings become read-only, sequences tuples.

    Used where a verified document is stored whole -- a controller policy, a set of entry-gate
    checks -- so that ``authority._policy["allowed_modes"].append(...)`` has nowhere to land.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): frozen_deep(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(frozen_deep(item) for item in value)
    return value


def deep_plain(value: Any) -> Any:
    """The inverse, for callers: ordinary ``dict``/``list`` again, detached from the capability.

    A frozen value must never leak out of an accessor, because ``MappingProxyType`` and ``tuple``
    do not canonicalize the way ``dict`` and ``list`` do -- and a scientific identity computed over
    the wrong one would silently differ. Every public accessor returns this, so what a caller sees
    is byte-for-byte what it saw before anything was frozen.
    """
    if isinstance(value, Mapping):
        return {str(k): deep_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_plain(item) for item in value]
    return value


class FrozenAfterMint:
    """Attributes may be written while the object is being built, and never afterwards.

    A constructor assigns its fields and finishes with ``self._freeze()``. From that moment every
    ``setattr`` and ``delattr`` raises the owning module's own error type, so a caller sees the
    vocabulary of the capability it misused rather than a generic ``AttributeError``.

    Subclasses set :attr:`_frozen_error` and :attr:`_frozen_noun`, and must include ``"_frozen"``
    in ``__slots__``. Deliberately independent of ``_seal``: the fixture terminus carries no seal
    and still must not be mutable, and the sealed capabilities set their seal *before* freezing.
    """

    __slots__ = ()

    _frozen_error: ClassVar[type[MinosEngineError]] = MinosEngineError
    _frozen_noun: ClassVar[str] = "capability"

    def _freeze(self) -> None:
        """Called LAST by the constructor. Everything assigned up to here is now final."""
        object.__setattr__(self, "_frozen", True)

    def _is_frozen(self) -> bool:
        # object.__getattribute__ rather than getattr: a subclass may proxy unknown names through
        # __getattr__, and a frozen check must never route into a caller-visible lookup.
        try:
            return bool(object.__getattribute__(self, "_frozen"))
        except AttributeError:
            return False

    def _refuse(self, name: str, verb: str) -> None:
        raise type(self)._frozen_error(
            f"a verified {type(self)._frozen_noun} is immutable once minted; {name!r} cannot be "
            f"{verb} -- its seal attests to the state that was verified, not to whatever the "
            "object was later told to hold"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if self._is_frozen():
            self._refuse(name, "reassigned")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if self._is_frozen():
            self._refuse(name, "deleted")
        object.__delattr__(self, name)
