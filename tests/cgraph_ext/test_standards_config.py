"""Tests for standards configuration: presets, overrides, category filtering."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codegraphcontext_ext.config import (
    CgraphConfig,
    StandardsConfig,
    STANDARDS_PRESETS,
    _apply_preset,
    _parse_cgraph_section,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# StandardsConfig defaults
# ---------------------------------------------------------------------------

def test_default_standards_config():
    cfg = CgraphConfig()
    assert cfg.standards.profile == "default"
    assert cfg.standards.categories == ["all"]
    assert "CGQ-A01" in cfg.standards.hard_stop


# ---------------------------------------------------------------------------
# Preset profiles
# ---------------------------------------------------------------------------

def test_preset_soc2():
    std = StandardsConfig(profile="soc2")
    _apply_preset(std)
    assert "compliance" in std.categories
    assert "CGQ-H01" in std.hard_stop
    assert "CGQ-H04" in std.hard_stop
    assert std.overrides.get("CGQ-H01") == "blocker"


def test_preset_strict():
    std = StandardsConfig(profile="strict")
    _apply_preset(std)
    assert "CGQ-A05" in std.hard_stop
    assert std.overrides.get("CGQ-A05") == "blocker"


def test_preset_minimal():
    std = StandardsConfig(profile="minimal")
    _apply_preset(std)
    assert std.categories == ["coupling"]
    assert len(std.hard_stop) == 3  # Only the 3 original


def test_preset_unknown_profile():
    std = StandardsConfig(profile="nonexistent")
    _apply_preset(std)
    # Should not crash — just no-op
    assert std.categories == ["all"]


def test_user_overrides_win_over_preset():
    """User explicit overrides are not clobbered by preset."""
    std = StandardsConfig(
        profile="soc2",
        overrides={"CGQ-H01": "warn"},  # User downgrades from preset's blocker
    )
    _apply_preset(std)
    # User value should win
    assert std.overrides["CGQ-H01"] == "warn"


def test_hard_stop_union():
    """User hard_stop + preset hard_stop = union."""
    std = StandardsConfig(
        profile="soc2",
        hard_stop=["CGQ-B01"],  # User adds their own
    )
    _apply_preset(std)
    assert "CGQ-B01" in std.hard_stop  # User's
    assert "CGQ-H01" in std.hard_stop  # Preset's
    assert "CGQ-A01" in std.hard_stop  # Preset's


# ---------------------------------------------------------------------------
# TOML parsing of [cgraph.standards]
# ---------------------------------------------------------------------------

def test_parse_standards_section():
    text = """\
[cgraph]
enabled = true

[cgraph.standards]
profile = "soc2"
categories = ["coupling", "compliance"]
hard_stop = ["CGQ-A01", "CGQ-A02", "CGQ-H01"]

[cgraph.standards.overrides]
CGQ-B04 = "off"
CGQ-A05 = "blocker"
"""
    cfg = _parse_cgraph_section(text)
    assert cfg.standards.profile == "soc2"
    assert "compliance" in cfg.standards.categories
    assert cfg.standards.overrides["CGQ-B04"] == "off"
    assert cfg.standards.overrides["CGQ-A05"] == "blocker"


def test_parse_standards_inherits_preset():
    text = """\
[cgraph.standards]
profile = "strict"
"""
    cfg = _parse_cgraph_section(text)
    # Strict preset should populate overrides
    assert "CGQ-A05" in cfg.standards.overrides


# ---------------------------------------------------------------------------
# Audit with config integration
# ---------------------------------------------------------------------------

class _FakeConn:
    def execute(self, query):
        return _FakeResult([])


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


def _make_standards_dir(tmp_path):
    std = tmp_path / "standards"
    std.mkdir()
    (std / "_exemptions.yaml").write_text("paths: []\n")
    (std / "rule_coupling.yaml").write_text(dedent("""\
        id: CGQ-A01
        advisory_kind: circular_imports
        severity: hard
        category: coupling
        summary: Circular imports
        query: "MATCH (f:Function) WHERE f.x > 5 RETURN f.uid, f.name, f.path, f.line_number"
    """))
    (std / "rule_complexity.yaml").write_text(dedent("""\
        id: CGQ-B01
        advisory_kind: high_complexity
        severity: warn
        category: complexity
        summary: High complexity
        query: "MATCH (f:Function) WHERE f.complexity > 10 RETURN f.uid, f.name, f.path, f.line_number"
    """))
    return std


def test_audit_category_filter_from_config(tmp_path):
    """Config categories filter which rules run."""
    std = _make_standards_dir(tmp_path)
    cfg = CgraphConfig(standards=StandardsConfig(categories=["coupling"]))

    with patch(
        "codegraphcontext_ext.commands.audit.resolve_cgraph_config",
        return_value=cfg,
    ), patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=_FakeConn(),
    ):
        from codegraphcontext_ext.commands.audit import build_audit_payload
        result = build_audit_payload(standards_dir=std)

    # Only coupling rule should run
    assert result["standards_evaluated"] == 1


def test_audit_rule_disabled_via_override(tmp_path):
    """Rules with override="off" are skipped."""
    std = _make_standards_dir(tmp_path)
    cfg = CgraphConfig(standards=StandardsConfig(
        overrides={"CGQ-A01": "off"},
    ))

    with patch(
        "codegraphcontext_ext.commands.audit.resolve_cgraph_config",
        return_value=cfg,
    ), patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=_FakeConn(),
    ):
        from codegraphcontext_ext.commands.audit import build_audit_payload
        result = build_audit_payload(standards_dir=std)

    # CGQ-A01 disabled, only CGQ-B01 should run
    assert result["standards_evaluated"] == 1


def test_audit_profile_soc2_via_cli(tmp_path):
    """--profile soc2 applies preset overrides."""
    std = _make_standards_dir(tmp_path)

    with patch(
        "codegraphcontext_ext.commands.audit.resolve_cgraph_config",
        return_value=CgraphConfig(),
    ), patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=_FakeConn(),
    ):
        from codegraphcontext_ext.commands.audit import build_audit_payload
        result = build_audit_payload(standards_dir=std, profile="soc2")

    # soc2 preset filters to coupling + compliance categories
    # Our test standards only have coupling, so should get 1
    assert result["standards_evaluated"] == 1


def test_audit_db_failure_hard_zero_false(tmp_path):
    """DB failure sets hard_zero=False (fail closed)."""
    std = _make_standards_dir(tmp_path)

    with patch(
        "codegraphcontext_ext.commands.audit.resolve_cgraph_config",
        return_value=CgraphConfig(),
    ), patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        from codegraphcontext_ext.commands.audit import build_audit_payload
        result = build_audit_payload(standards_dir=std)

    assert result["ok"] is False
    assert result["hard_zero"] is False  # Fixed: was True (bug)
