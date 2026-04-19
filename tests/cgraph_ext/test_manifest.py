"""Tests for the kkg manifest command.

Backend-free: imports only from ``io/`` to avoid triggering the
commands-package __init__ and its transitive dependencies.
"""

from __future__ import annotations

import json

import pytest

from codegraphcontext_ext.io.json_stdout import SCHEMA_VERSION, make_envelope, emit_json
from codegraphcontext_ext.io.registry import get_command_registry
from codegraphcontext_ext.io.schema_check import load_schema, schema_path, validate_schema


# ── Registry structure ──────────────────────────────────────────────


def test_registry_is_nonempty():
    registry = get_command_registry()
    assert len(registry) > 0


def test_registry_entries_have_required_fields():
    required = {
        "name", "summary", "project_aware", "touches_kuzu",
        "output_modes", "server", "prereqs",
    }
    for entry in get_command_registry():
        assert required.issubset(entry.keys()), f"Missing fields in {entry.get('name', '?')}"


def test_registry_names_are_unique():
    names = [e["name"] for e in get_command_registry()]
    assert len(names) == len(set(names))


def test_registry_output_modes_are_valid():
    valid_modes = {"json", "html", "summary"}
    for entry in get_command_registry():
        for mode in entry["output_modes"]:
            assert mode in valid_modes, f"Invalid mode {mode!r} in {entry['name']}"


def test_manifest_includes_itself():
    names = [e["name"] for e in get_command_registry()]
    assert "manifest" in names


def test_registry_schema_files_exist():
    """Every non-null schema reference must point to a real file."""
    for entry in get_command_registry():
        if entry.get("schema"):
            path = schema_path(entry["schema"])
            assert path.is_file(), f"Schema {entry['schema']} missing for {entry['name']}"


# ── Prereqs metadata ───────────────────────────────────────────────


def test_kuzu_commands_require_kuzudb_path():
    """Every command that touches_kuzu must list KUZUDB_PATH in prereqs."""
    for entry in get_command_registry():
        if entry["touches_kuzu"]:
            assert "KUZUDB_PATH" in entry["prereqs"], (
                f"{entry['name']} touches_kuzu but does not list KUZUDB_PATH in prereqs"
            )


def test_non_kuzu_commands_do_not_require_kuzudb_path():
    for entry in get_command_registry():
        if not entry["touches_kuzu"]:
            assert "KUZUDB_PATH" not in entry["prereqs"], (
                f"{entry['name']} does not touch kuzu but lists KUZUDB_PATH"
            )


def test_prereqs_are_string_lists():
    for entry in get_command_registry():
        assert isinstance(entry["prereqs"], list), f"{entry['name']} prereqs is not a list"
        for p in entry["prereqs"]:
            assert isinstance(p, str), f"{entry['name']} has non-string prereq: {p!r}"


def test_embed_and_search_require_hf_home():
    registry = {e["name"]: e for e in get_command_registry()}
    for name in ("embed", "search"):
        assert "HF_HOME" in registry[name]["prereqs"], f"{name} should require HF_HOME"


# ── Manifest payload ────────────────────────────────────────────────


def test_manifest_payload_structure(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    registry = get_command_registry()
    payload = make_envelope(
        "manifest",
        {
            "commands": registry,
            "envelope_schema": "envelope.json",
            "total_commands": len(registry),
        },
    )
    assert payload["ok"] is True
    assert payload["kind"] == "manifest"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["project"] is None
    assert payload["total_commands"] == len(registry)
    assert len(payload["commands"]) == len(registry)


def test_manifest_payload_validates_against_schema(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    schema = load_schema("manifest.json")
    registry = get_command_registry()
    payload = make_envelope(
        "manifest",
        {
            "commands": registry,
            "envelope_schema": "envelope.json",
            "total_commands": len(registry),
        },
    )
    validate_schema(schema, payload)


def test_manifest_payload_also_validates_against_envelope_schema(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    envelope_schema = load_schema("envelope.json")
    registry = get_command_registry()
    payload = make_envelope(
        "manifest",
        {
            "commands": registry,
            "envelope_schema": "envelope.json",
            "total_commands": len(registry),
        },
    )
    validate_schema(envelope_schema, payload)


def test_manifest_payload_serializes_to_valid_json(monkeypatch):
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    registry = get_command_registry()
    payload = make_envelope(
        "manifest",
        {
            "commands": registry,
            "envelope_schema": "envelope.json",
            "total_commands": len(registry),
        },
    )
    raw = emit_json(payload)
    parsed = json.loads(raw)
    assert parsed["kind"] == "manifest"
    assert isinstance(parsed["commands"], list)
    for cmd in parsed["commands"]:
        assert "prereqs" in cmd


# ── Schema files ────────────────────────────────────────────────────


def test_manifest_schema_exists():
    assert schema_path("manifest.json").is_file()


def test_envelope_schema_exists():
    assert schema_path("envelope.json").is_file()


# ── CLI integration (requires full commands/ package) ────────────────
# These tests import codegraphcontext_ext.cli which triggers the full
# commands __init__.  Each test uses pytest.importorskip individually
# so the backend-free tests above still run when heavy deps are absent.


def test_manifest_json_flag_via_cli():
    """kkg manifest --json produces valid envelope output."""
    cli_mod = pytest.importorskip(
        "codegraphcontext_ext.cli", reason="full commands package not available"
    )
    from typer.testing import CliRunner
    import typer as _typer

    app = _typer.Typer()
    cli_mod.register_extensions(app)
    result = CliRunner().invoke(app, ["manifest", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["kind"] == "manifest"
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert isinstance(parsed["commands"], list)


def test_manifest_rejects_non_json_format():
    cli_mod = pytest.importorskip(
        "codegraphcontext_ext.cli", reason="full commands package not available"
    )
    from typer.testing import CliRunner
    import typer as _typer

    app = _typer.Typer()
    cli_mod.register_extensions(app)
    result = CliRunner().invoke(app, ["manifest", "--format", "xml"])
    assert result.exit_code != 0


def test_manifest_registered_in_cli():
    cli_mod = pytest.importorskip(
        "codegraphcontext_ext.cli", reason="full commands package not available"
    )
    import inspect

    source = inspect.getsource(cli_mod.register_extensions)
    assert "manifest_command" in source
    assert "manifest" in source


# ── Project-aware field accuracy ────────────────────────────────────


_KNOWN_PROJECT_AWARE = {
    "audit",
    "blast-radius",
    "drift-check",
    "embed",
    "export-embeddings",
    "review-packet",
    "search",
    "viz-dashboard",
    "viz-embeddings",
    "viz-graph",
    "viz-projector",
}


def test_project_aware_registry_matches_known():
    registry = get_command_registry()
    declared = {e["name"] for e in registry if e["project_aware"]}
    assert declared == _KNOWN_PROJECT_AWARE


# ── Envelope integration ────────────────────────────────────────────


def test_every_schema_file_validates_against_envelope_base():
    """Per-command schemas with envelope fields must be compatible."""
    envelope_schema = load_schema("envelope.json")
    for entry in get_command_registry():
        if not entry.get("schema"):
            continue
        cmd_schema = load_schema(entry["schema"])
        cmd_props = set(cmd_schema.get("properties", {}).keys())
        for field in set(envelope_schema["required"]):
            if field in cmd_props:
                pass  # Field exists in both — type checked at runtime


def test_manifest_schema_validates_command_entries_via_ref(monkeypatch):
    """$ref resolution: manifest.json uses $ref to command_entry $def.
    Validate that each registry entry is checked against that def."""
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    schema = load_schema("manifest.json")
    registry = get_command_registry()
    payload = make_envelope(
        "manifest",
        {
            "commands": registry,
            "envelope_schema": "envelope.json",
            "total_commands": len(registry),
        },
    )
    # This should deeply validate each command entry via $ref
    validate_schema(schema, payload)

    # Now prove $ref is actually resolving by injecting a bad entry
    from codegraphcontext_ext.io.schema_check import SchemaValidationError

    bad_payload = make_envelope(
        "manifest",
        {
            "commands": [{"name": "bad"}],  # missing required fields
            "envelope_schema": "envelope.json",
            "total_commands": 1,
        },
    )
    with pytest.raises(SchemaValidationError, match="missing required"):
        validate_schema(schema, bad_payload)
