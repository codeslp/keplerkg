"""Tests for the Phase 3 advise command."""

from __future__ import annotations

import json
from unittest.mock import patch

import jsonschema
import pytest
from typer.testing import CliRunner

from codegraphcontext_ext.commands.advise import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _generate_advisory_id,
    build_advise_payload,
)
from codegraphcontext_ext.config import CgraphConfig, LaneConfig
from codegraphcontext_ext.io.schema_check import schema_path

runner = CliRunner()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_command_metadata():
    assert COMMAND_NAME == "advise"
    assert SCHEMA_FILE == "advise.json"
    assert isinstance(SUMMARY, str) and len(SUMMARY) > 0


# ---------------------------------------------------------------------------
# advisory_id generation
# ---------------------------------------------------------------------------

def test_advisory_id_format():
    aid = _generate_advisory_id("lock_overlap", {"lane": "b"})
    assert aid.startswith("adv_")
    parts = aid.split("_")
    assert len(parts) == 3
    assert len(parts[1]) == 10  # YYYYMMDDHH
    assert len(parts[2]) == 6   # hex hash


def test_advisory_id_deterministic():
    a = _generate_advisory_id("drift", {"lane": "a"})
    b = _generate_advisory_id("drift", {"lane": "a"})
    # Same inputs in same hour → same ID
    assert a == b


def test_advisory_id_varies_by_situation():
    a = _generate_advisory_id("drift", {"lane": "a"})
    b = _generate_advisory_id("lock_overlap", {"lane": "a"})
    assert a != b


# ---------------------------------------------------------------------------
# build_advise_payload — known situations
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = CgraphConfig()


def test_lock_overlap_tip():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = build_advise_payload("lock_overlap", {"files": "src/auth/"}, lane="b")
    assert result["situation"] == "lock_overlap"
    assert result["advisory_id"] is not None
    assert "blast-radius" in result["suggestion"]
    assert result["rationale"]


def test_drift_tip():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = build_advise_payload("drift", lane="a")
    assert "drift-check" in result["suggestion"]


def test_packet_truncated_tip():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = build_advise_payload("packet_truncated")
    assert result["suggestion"] is not None
    assert "truncat" in result["rationale"].lower()


# ---------------------------------------------------------------------------
# build_advise_payload — unknown situation
# ---------------------------------------------------------------------------

def test_unknown_situation():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = build_advise_payload("nonexistent_thing")
    assert result["situation"] == "nonexistent_thing"
    assert result["advisory_id"] is None
    assert result["suggestion"] is None


# ---------------------------------------------------------------------------
# build_advise_payload — config filtering
# ---------------------------------------------------------------------------

def test_lane_advise_disabled():
    cfg = CgraphConfig(lanes={"b": LaneConfig(disable_advise=True)})
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=cfg,
    ):
        result = build_advise_payload("lock_overlap", lane="b")
    assert result["suggestion"] is None
    assert "disabled" in result["rationale"].lower()


def test_lane_advise_on_filter():
    cfg = CgraphConfig(lanes={"b": LaneConfig(advise_on=["drift"])})
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=cfg,
    ):
        result = build_advise_payload("lock_overlap", lane="b")
    assert result["suggestion"] is None
    assert "not in advise_on" in result["rationale"]


def test_project_level_advise_on_filter():
    cfg = CgraphConfig(advise_on=["drift"])
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=cfg,
    ):
        result = build_advise_payload("lock_overlap")
    assert result["suggestion"] is None


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _load_schema():
    import json as _json
    return _json.loads(schema_path("advise.json").read_text())


def test_schema_validation_known():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = build_advise_payload("lock_overlap", {"files": "src/"}, lane="a")
    jsonschema.validate(result, _load_schema())


def test_schema_validation_unknown():
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = build_advise_payload("made_up")
    jsonschema.validate(result, _load_schema())


def test_schema_validation_suppressed():
    cfg = CgraphConfig(lanes={"b": LaneConfig(disable_advise=True)})
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=cfg,
    ):
        result = build_advise_payload("lock_overlap", lane="b")
    jsonschema.validate(result, _load_schema())


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_basic():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = runner.invoke(app, ["advise", "lock_overlap"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["situation"] == "lock_overlap"


def test_cli_with_context():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    ctx = json.dumps({"files": "src/auth/", "lane": "b"})
    with patch(
        "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        return_value=_DEFAULT_CONFIG,
    ):
        result = runner.invoke(app, ["advise", "lock_overlap", "--context", ctx, "--lane", "b"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "src/auth/" in payload["suggestion"]


def test_cli_invalid_context():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    result = runner.invoke(app, ["advise", "lock_overlap", "--context", "not-json"])
    assert result.exit_code != 0
