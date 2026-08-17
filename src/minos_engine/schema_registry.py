"""Load and validate against the repository JSON Schemas in ``schemas/``.

A single place resolves schema files and runs Draft 2020-12 validation with
local ``$ref`` resolution between the schema files.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from minos_engine.common.errors import ContractValidationError

__all__ = ["schemas_dir", "load_schema", "validate_against", "available_schemas"]


def schemas_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "schemas"


def _schema_path(name: str) -> Path:
    filename = name if name.endswith(".schema.json") else f"{name}.schema.json"
    return schemas_dir() / filename


@cache
def load_schema(name: str) -> dict[str, Any]:
    path = _schema_path(name)
    if not path.exists():
        raise ContractValidationError(f"schema not found: {path.name}")
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def available_schemas() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in schemas_dir().glob("*.schema.json")))


@cache
def _registry() -> Registry:
    resources = []
    for p in schemas_dir().glob("*.schema.json"):
        with p.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        resource = Resource.from_contents(doc)
        uri = doc.get("$id", p.name)
        resources.append((uri, resource))
        # Also register under the bare filename so relative $refs resolve.
        resources.append((p.name, resource))
    return Registry().with_resources(resources)


def _validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    return Draft202012Validator(schema, registry=_registry())


def validate_against(name: str, obj: Any) -> None:
    """Validate ``obj`` against schema ``name``; raise on the first error."""
    validator = _validator(name)
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise ContractValidationError(
            f"schema {name} validation failed at {location}: {first.message}"
        )
