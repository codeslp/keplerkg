"""Backend-free CLI contract tests for the cgraph JSON envelope.

These tests validate the envelope schema and ``make_envelope`` utility
without touching KuzuDB or any external services.  They form the
"contract-test split" described in Phase 2.5.
"""

from __future__ import annotations

import json
import os

import pytest

from codegraphcontext_ext.io.json_stdout import (
    SCHEMA_VERSION,
    _ENVELOPE_KEYS,
    emit_json,
    make_envelope,
)
from codegraphcontext_ext.io.schema_check import load_schema, validate_schema


# ── Envelope schema tests ───────────────────────────────────────────


def test_envelope_schema_loads():
    schema = load_schema("envelope.json")
    assert schema["title"] == "cgraph canonical JSON envelope"
    assert "ok" in schema["properties"]
    assert "kind" in schema["properties"]
    assert "schema_version" in schema["properties"]


def test_envelope_schema_requires_ok_kind_version():
    schema = load_schema("envelope.json")
    assert set(schema["required"]) == {"ok", "kind", "schema_version"}


def test_envelope_schema_version_pattern():
    schema = load_schema("envelope.json")
    pattern = schema["properties"]["schema_version"]["pattern"]
    import re

    assert re.match(pattern, "1.0")
    assert re.match(pattern, "2.13")
    assert not re.match(pattern, "abc")
    assert not re.match(pattern, "1")


# ── make_envelope tests ─────────────────────────────────────────────


def test_make_envelope_basic():
    env = make_envelope("test_cmd")
    assert env["ok"] is True
    assert env["kind"] == "test_cmd"
    assert env["schema_version"] == SCHEMA_VERSION
    assert "project" in env


def test_make_envelope_merges_data():
    env = make_envelope("audit", {"advisories": [], "counts": {"warn": 0}})
    assert env["advisories"] == []
    assert env["counts"] == {"warn": 0}
    assert env["ok"] is True
    assert env["kind"] == "audit"


def test_make_envelope_error():
    env = make_envelope("test_cmd", ok=False, error="something broke")
    assert env["ok"] is False
    assert env["error"] == "something broke"


def test_make_envelope_no_error_field_on_success():
    env = make_envelope("test_cmd")
    assert "error" not in env


def test_make_envelope_project_from_env(monkeypatch):
    monkeypatch.setenv("CGRAPH_PROJECT", "flask")
    env = make_envelope("test_cmd")
    assert env["project"] == "flask"


def test_make_envelope_project_null_when_unset(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    env = make_envelope("test_cmd")
    assert env["project"] is None


def test_make_envelope_explicit_project_overrides_env(monkeypatch):
    monkeypatch.setenv("CGRAPH_PROJECT", "wrong")
    env = make_envelope("test_cmd", project="flask")
    assert env["project"] == "flask"


def test_make_envelope_explicit_project_none():
    env = make_envelope("test_cmd", project=None)
    assert env["project"] is None


def test_make_envelope_rejects_reserved_key_collision():
    with pytest.raises(ValueError, match="reserved envelope key"):
        make_envelope("test_cmd", {"ok": True, "extra": 1})


def test_make_envelope_rejects_kind_collision():
    with pytest.raises(ValueError, match="reserved envelope key"):
        make_envelope("test_cmd", {"kind": "sneaky"})


def test_make_envelope_validates_against_envelope_schema(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    schema = load_schema("envelope.json")
    env = make_envelope("test_cmd", {"extra": "field"})
    # Envelope schema does not use additionalProperties: false,
    # so extra command-specific fields are allowed.
    validate_schema(schema, env)


def test_make_envelope_with_error_validates(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    schema = load_schema("envelope.json")
    env = make_envelope("test_cmd", ok=False, error="fail")
    validate_schema(schema, env)


# ── emit_json tests ─────────────────────────────────────────────────


def test_emit_json_produces_valid_json():
    payload = make_envelope("test", {"count": 42})
    raw = emit_json(payload)
    parsed = json.loads(raw)
    assert parsed["kind"] == "test"
    assert parsed["count"] == 42


def test_emit_json_sort_keys():
    raw = emit_json({"z": 1, "a": 2})
    keys = list(json.loads(raw).keys())
    assert keys == ["a", "z"]


# ── Schema version constant ─────────────────────────────────────────


def test_schema_version_format():
    parts = SCHEMA_VERSION.split(".")
    assert len(parts) == 2
    assert all(p.isdigit() for p in parts)


# ── Envelope key set ────────────────────────────────────────────────


def test_envelope_keys_constant():
    assert "ok" in _ENVELOPE_KEYS
    assert "kind" in _ENVELOPE_KEYS
    assert "schema_version" in _ENVELOPE_KEYS
    assert "project" in _ENVELOPE_KEYS
    assert "error" in _ENVELOPE_KEYS


# ── Cross-schema compatibility ──────────────────────────────────────
# Verify that existing per-command schemas don't conflict with the
# envelope's required fields.


_SCHEMAS_WITH_KIND = [
    "audit.json",
    "blast-radius.json",
]


@pytest.mark.parametrize("schema_file", _SCHEMAS_WITH_KIND)
def test_existing_schemas_compatible_with_envelope(schema_file):
    """Schemas that already declare 'kind' must use it as a string."""
    schema = load_schema(schema_file)
    props = schema.get("properties", {})
    if "kind" in props:
        kind_def = props["kind"]
        assert kind_def.get("type") in ("string", None) or "const" in kind_def or "enum" in kind_def


# ── schema_check validator coverage ─────────────────────────────────

from codegraphcontext_ext.io.schema_check import SchemaValidationError


def test_schema_check_validates_number_type():
    schema = {"type": "number", "minimum": 0, "maximum": 1}
    validate_schema(schema, 0.5)
    with pytest.raises(SchemaValidationError):
        validate_schema(schema, -1)
    with pytest.raises(SchemaValidationError):
        validate_schema(schema, 2)
    with pytest.raises(SchemaValidationError):
        validate_schema(schema, "not a number")


def test_schema_check_validates_pattern():
    schema = {"type": "string", "pattern": r"^\d+\.\d+$"}
    validate_schema(schema, "1.0")
    with pytest.raises(SchemaValidationError, match="pattern"):
        validate_schema(schema, "abc")


def test_schema_check_validates_enum():
    schema = {"type": "string", "enum": ["warn", "hard"]}
    validate_schema(schema, "warn")
    with pytest.raises(SchemaValidationError, match="enum"):
        validate_schema(schema, "info")


def test_schema_check_validates_union_type():
    schema = {"type": ["string", "null"]}
    validate_schema(schema, "hello")
    validate_schema(schema, None)
    with pytest.raises(SchemaValidationError):
        validate_schema(schema, 42)


def test_schema_check_validates_min_items():
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 1}
    validate_schema(schema, ["a"])
    with pytest.raises(SchemaValidationError, match=">=.*1 items"):
        validate_schema(schema, [])


def test_schema_check_validates_null_type():
    schema = {"type": "null"}
    validate_schema(schema, None)
    with pytest.raises(SchemaValidationError):
        validate_schema(schema, "not null")


def test_schema_check_validates_const():
    schema = {"const": "manifest"}
    validate_schema(schema, "manifest")
    with pytest.raises(SchemaValidationError, match="constant"):
        validate_schema(schema, "other")


def test_schema_check_resolves_ref():
    """$ref resolution: items pointing to $defs are followed."""
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/entry"},
            }
        },
        "$defs": {
            "entry": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
                "additionalProperties": False,
            }
        },
    }
    validate_schema(schema, {"items": [{"id": "a"}]})
    with pytest.raises(SchemaValidationError, match="missing required"):
        validate_schema(schema, {"items": [{}]})
    with pytest.raises(SchemaValidationError, match="additional property"):
        validate_schema(schema, {"items": [{"id": "a", "extra": 1}]})
