import json

from codegraphcontext_ext.io.json_stdout import emit_json


def test_emit_json_round_trips_through_json_loads():
    payload = {"a": 1, "b": [1, 2], "c": {"nested": True}, "d": None}
    assert json.loads(emit_json(payload)) == payload


def test_emit_json_sorts_keys_for_stable_stdout_output():
    assert emit_json({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


def test_emit_json_preserves_unicode_after_round_trip():
    assert json.loads(emit_json({"name": "café"})) == {"name": "café"}


def test_emit_json_serializes_empty_collections():
    assert emit_json({}) == "{}"
    assert emit_json([]) == "[]"
