"""Helpers for loading scaffolded JSON schema files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when a payload does not match a repo-local schema stub."""


def schema_path(schema_name: str) -> Path:
    """Return the repo-local schema path for a scaffolded cgraph command."""

    return Path(__file__).resolve().parents[3] / "schemas" / schema_name


def load_schema(schema_name: str) -> dict[str, Any]:
    """Load one scaffolded schema document from disk."""

    return json.loads(schema_path(schema_name).read_text(encoding="utf-8"))


def validate_payload(schema_name: str, payload: Any) -> None:
    """Validate a payload against one repo-local schema."""

    validate_schema(load_schema(schema_name), payload)


def validate_schema(
    schema: dict[str, Any],
    payload: Any,
    *,
    location: str = "$",
    _root: dict[str, Any] | None = None,
) -> None:
    """Validate JSON Schema features used by cgraph.

    Covers: type (object, array, string, integer, number, boolean, null,
    union via list), const, enum, oneOf, required, properties,
    additionalProperties, items, minItems, minLength, minimum, maximum,
    pattern, and $ref (within the same document via $defs).
    """
    if _root is None:
        _root = schema

    # Resolve $ref before anything else
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], _root)

    if "oneOf" in schema:
        branch_errors: list[str] = []
        for branch in schema["oneOf"]:
            try:
                validate_schema(branch, payload, location=location, _root=_root)
            except SchemaValidationError as exc:
                branch_errors.append(str(exc))
            else:
                return
        raise SchemaValidationError(
            f"{location} did not match any allowed schema: {'; '.join(branch_errors)}"
        )

    expected_type = schema.get("type")

    # Union types: {"type": ["string", "null"]}
    if isinstance(expected_type, list):
        _validate_union_type(expected_type, payload, location)
    elif expected_type == "object":
        if not isinstance(payload, dict):
            raise SchemaValidationError(f"{location} expected object, got {type(payload).__name__}")
        _validate_object(schema, payload, location, _root)
    elif expected_type == "array":
        if not isinstance(payload, list):
            raise SchemaValidationError(f"{location} expected array, got {type(payload).__name__}")
        _validate_array(schema, payload, location, _root)
    elif expected_type == "string":
        if not isinstance(payload, str):
            raise SchemaValidationError(f"{location} expected string, got {type(payload).__name__}")
        _validate_string(schema, payload, location)
    elif expected_type == "integer":
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise SchemaValidationError(f"{location} expected integer, got {type(payload).__name__}")
        _validate_numeric(schema, payload, location)
    elif expected_type == "number":
        if isinstance(payload, bool) or not isinstance(payload, (int, float)):
            raise SchemaValidationError(f"{location} expected number, got {type(payload).__name__}")
        _validate_numeric(schema, payload, location)
    elif expected_type == "boolean":
        if not isinstance(payload, bool):
            raise SchemaValidationError(f"{location} expected boolean, got {type(payload).__name__}")
    elif expected_type == "null":
        if payload is not None:
            raise SchemaValidationError(f"{location} expected null, got {type(payload).__name__}")

    if "const" in schema and payload != schema["const"]:
        raise SchemaValidationError(f"{location} expected constant value {schema['const']!r}")

    if "enum" in schema and payload not in schema["enum"]:
        raise SchemaValidationError(
            f"{location} value {payload!r} not in enum {schema['enum']}"
        )


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a JSON Pointer ``$ref`` within the root document.

    Only supports ``#/$defs/<name>`` and ``#/definitions/<name>`` forms.
    """
    if not ref.startswith("#/"):
        raise SchemaValidationError(f"Unsupported $ref: {ref!r}")
    parts = ref[2:].split("/")
    node: Any = root
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise SchemaValidationError(f"Cannot resolve $ref {ref!r}: missing {part!r}")
    if not isinstance(node, dict):
        raise SchemaValidationError(f"$ref {ref!r} resolved to non-object")
    return node


def _validate_union_type(
    allowed_types: list[str], payload: Any, location: str,
) -> None:
    """Check payload matches at least one type in a union list."""
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    for t in allowed_types:
        if t == "null" and payload is None:
            return
        if t == "integer" and isinstance(payload, bool):
            continue
        if t == "number" and isinstance(payload, bool):
            continue
        expected = type_map.get(t)
        if expected and isinstance(payload, expected):
            return
    raise SchemaValidationError(
        f"{location} expected one of {allowed_types}, got {type(payload).__name__}"
    )


def _validate_string(schema: dict[str, Any], payload: str, location: str) -> None:
    min_length = schema.get("minLength")
    if min_length is not None and len(payload) < min_length:
        raise SchemaValidationError(f"{location} expected string length >= {min_length}")
    pattern = schema.get("pattern")
    if pattern is not None and not re.search(pattern, payload):
        raise SchemaValidationError(
            f"{location} string {payload!r} does not match pattern {pattern!r}"
        )


def _validate_numeric(schema: dict[str, Any], payload: int | float, location: str) -> None:
    minimum = schema.get("minimum")
    if minimum is not None and payload < minimum:
        raise SchemaValidationError(f"{location} expected value >= {minimum}")
    maximum = schema.get("maximum")
    if maximum is not None and payload > maximum:
        raise SchemaValidationError(f"{location} expected value <= {maximum}")


def _validate_object(
    schema: dict[str, Any],
    payload: dict[str, Any],
    location: str,
    _root: dict[str, Any],
) -> None:
    required = schema.get("required", [])
    for key in required:
        if key not in payload:
            raise SchemaValidationError(f"{location} missing required property {key!r}")

    properties = schema.get("properties", {})
    additional_properties = schema.get("additionalProperties", True)

    for key, value in payload.items():
        child_location = f"{location}.{key}"
        if key in properties:
            validate_schema(properties[key], value, location=child_location, _root=_root)
            continue
        if additional_properties is False:
            raise SchemaValidationError(f"{location} does not allow additional property {key!r}")
        if isinstance(additional_properties, dict):
            validate_schema(additional_properties, value, location=child_location, _root=_root)


def _validate_array(
    schema: dict[str, Any],
    payload: list[Any],
    location: str,
    _root: dict[str, Any],
) -> None:
    min_items = schema.get("minItems")
    if min_items is not None and len(payload) < min_items:
        raise SchemaValidationError(
            f"{location} expected array with >= {min_items} items, got {len(payload)}"
        )

    item_schema = schema.get("items")
    if not isinstance(item_schema, dict):
        return

    for index, item in enumerate(payload):
        validate_schema(item_schema, item, location=f"{location}[{index}]", _root=_root)
