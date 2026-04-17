import json

import pytest

from codegraphcontext_ext.io.schema_check import (
    SchemaValidationError,
    load_schema,
    schema_path,
    validate_payload,
    validate_schema,
)


def test_schema_path_points_at_an_existing_repo_schema():
    assert schema_path("sync-check.json").is_file()


def test_load_schema_parses_sync_check_stub():
    schema = load_schema("sync-check.json")
    assert schema["title"] == "cgraph sync-check response"
    assert "oneOf" in schema


def test_all_scaffolded_schemas_parse_as_json_objects():
    schemas_dir = schema_path("sync-check.json").parent
    stubs = sorted(schemas_dir.glob("*.json"))
    assert stubs, "expected scaffolded schema stubs in schemas/"
    for stub in stubs:
        payload = json.loads(stub.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        assert payload.get("title")


def test_validate_payload_accepts_a_skipped_sync_check_payload():
    validate_payload(
        "sync-check.json",
        {"skipped": True, "reason": "demo", "suggestion": "try again"},
    )


def test_validate_payload_accepts_review_packet_advisory_contract():
    validate_payload(
        "review-packet.json",
        {
            "source": "locked_files",
            "base": None,
            "head": None,
            "diff_stats": {"files": 0, "additions": 0, "deletions": 0},
            "touched_nodes": [],
            "callers_not_in_diff": [],
            "callees_not_in_diff": [],
            "cross_module_impact": [],
            "advisories": [
                {"level": "warn", "kind": "stale_index", "detail": "src/auth.py"},
                {"level": "warn", "kind": "excluded_by_cgcignore", "detail": "ignored.py"},
                {"level": "warn", "kind": "unsupported_repo_shape", "detail": "bare_repo"},
            ],
        },
    )


def test_validate_schema_requires_declared_keys_on_objects():
    schema = {"type": "object", "required": ["a"]}
    with pytest.raises(SchemaValidationError, match="missing required property 'a'"):
        validate_schema(schema, {})


def test_validate_schema_rejects_wrong_root_type():
    with pytest.raises(SchemaValidationError, match="expected object"):
        validate_schema({"type": "object"}, "not an object")


def test_validate_schema_additional_properties_false_rejects_extras():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "integer"}},
    }
    with pytest.raises(SchemaValidationError, match="additional property 'b'"):
        validate_schema(schema, {"a": 1, "b": 2})


def test_validate_schema_additional_properties_schema_validates_extras():
    schema = {
        "type": "object",
        "additionalProperties": {"type": "integer"},
        "properties": {},
    }
    validate_schema(schema, {"x": 1})
    with pytest.raises(SchemaValidationError, match="expected integer"):
        validate_schema(schema, {"x": "nope"})


def test_validate_schema_array_items_bubble_index_into_location():
    schema = {"type": "array", "items": {"type": "string"}}
    validate_schema(schema, ["a", "b"])
    with pytest.raises(SchemaValidationError, match=r"\[1\] expected string"):
        validate_schema(schema, ["a", 2])


def test_validate_schema_string_min_length():
    schema = {"type": "string", "minLength": 3}
    validate_schema(schema, "abc")
    with pytest.raises(SchemaValidationError, match="length >= 3"):
        validate_schema(schema, "ab")


def test_validate_schema_integer_minimum_rejects_smaller_values():
    schema = {"type": "integer", "minimum": 0}
    validate_schema(schema, 0)
    with pytest.raises(SchemaValidationError, match=">= 0"):
        validate_schema(schema, -1)


def test_validate_schema_integer_rejects_booleans():
    with pytest.raises(SchemaValidationError, match="expected integer"):
        validate_schema({"type": "integer"}, True)


def test_validate_schema_boolean_accepts_bool_only():
    validate_schema({"type": "boolean"}, True)
    with pytest.raises(SchemaValidationError, match="expected boolean"):
        validate_schema({"type": "boolean"}, "true")


def test_validate_schema_const_rejects_other_values():
    schema = {"const": "fixed"}
    validate_schema(schema, "fixed")
    with pytest.raises(SchemaValidationError, match="expected constant"):
        validate_schema(schema, "other")


def test_validate_schema_one_of_selects_first_matching_branch():
    schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    validate_schema(schema, "x")
    validate_schema(schema, 3)


def test_validate_schema_one_of_reports_all_branch_errors_on_mismatch():
    schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    with pytest.raises(SchemaValidationError, match="did not match any allowed schema"):
        validate_schema(schema, {"k": "v"})
