"""Helpers for loading scaffolded JSON schema files."""

from __future__ import annotations

import json
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


def validate_schema(schema: dict[str, Any], payload: Any, *, location: str = "$") -> None:
    """Validate a subset of JSON Schema features used by cgraph stubs."""

    if "oneOf" in schema:
        branch_errors: list[str] = []
        for branch in schema["oneOf"]:
            try:
                validate_schema(branch, payload, location=location)
            except SchemaValidationError as exc:
                branch_errors.append(str(exc))
            else:
                return
        raise SchemaValidationError(
            f"{location} did not match any allowed schema: {'; '.join(branch_errors)}"
        )

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(payload, dict):
            raise SchemaValidationError(f"{location} expected object, got {type(payload).__name__}")
        _validate_object(schema, payload, location)
    elif expected_type == "array":
        if not isinstance(payload, list):
            raise SchemaValidationError(f"{location} expected array, got {type(payload).__name__}")
        _validate_array(schema, payload, location)
    elif expected_type == "string":
        if not isinstance(payload, str):
            raise SchemaValidationError(f"{location} expected string, got {type(payload).__name__}")
        min_length = schema.get("minLength")
        if min_length is not None and len(payload) < min_length:
            raise SchemaValidationError(f"{location} expected string length >= {min_length}")
    elif expected_type == "integer":
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise SchemaValidationError(f"{location} expected integer, got {type(payload).__name__}")
        minimum = schema.get("minimum")
        if minimum is not None and payload < minimum:
            raise SchemaValidationError(f"{location} expected integer >= {minimum}")
    elif expected_type == "boolean":
        if not isinstance(payload, bool):
            raise SchemaValidationError(f"{location} expected boolean, got {type(payload).__name__}")

    if "const" in schema and payload != schema["const"]:
        raise SchemaValidationError(f"{location} expected constant value {schema['const']!r}")


def _validate_object(schema: dict[str, Any], payload: dict[str, Any], location: str) -> None:
    required = schema.get("required", [])
    for key in required:
        if key not in payload:
            raise SchemaValidationError(f"{location} missing required property {key!r}")

    properties = schema.get("properties", {})
    additional_properties = schema.get("additionalProperties", True)

    for key, value in payload.items():
        child_location = f"{location}.{key}"
        if key in properties:
            validate_schema(properties[key], value, location=child_location)
            continue
        if additional_properties is False:
            raise SchemaValidationError(f"{location} does not allow additional property {key!r}")
        if isinstance(additional_properties, dict):
            validate_schema(additional_properties, value, location=child_location)


def _validate_array(schema: dict[str, Any], payload: list[Any], location: str) -> None:
    item_schema = schema.get("items")
    if not isinstance(item_schema, dict):
        return

    for index, item in enumerate(payload):
        validate_schema(item_schema, item, location=f"{location}[{index}]")
