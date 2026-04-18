"""Tests for the Phase 3 cgraph config layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from codegraphcontext_ext.config import (
    CgraphConfig,
    LaneConfig,
    _parse_cgraph_section,
    resolve_cgraph_config,
)


# ---------------------------------------------------------------------------
# _parse_cgraph_section
# ---------------------------------------------------------------------------

def test_empty_toml():
    cfg = _parse_cgraph_section("")
    assert cfg.enabled is False
    assert cfg.bin_path == "kkg"
    assert cfg.advise_on == ["lock_overlap", "drift", "packet_truncated"]


def test_minimal_cgraph_section():
    text = """\
[cgraph]
enabled = true
bin_path = "/usr/local/bin/kkg"
"""
    cfg = _parse_cgraph_section(text)
    assert cfg.enabled is True
    assert cfg.bin_path == "/usr/local/bin/kkg"


def test_full_cgraph_section():
    text = """\
[cgraph]
enabled = true
bin_path = "kkg"
source_checkout = "~/repos/cgraph"
db_path = "/Volumes/zombie/cgraph/db/kuzudb"
model_cache = "/Volumes/zombie/cgraph/hf-cache"
advise_on = ["lock_overlap", "drift"]
advise_on_resolution = true
"""
    cfg = _parse_cgraph_section(text)
    assert cfg.enabled is True
    assert cfg.source_checkout == Path("~/repos/cgraph").expanduser()
    assert cfg.db_path == Path("/Volumes/zombie/cgraph/db/kuzudb")
    assert cfg.model_cache == Path("/Volumes/zombie/cgraph/hf-cache")
    assert cfg.advise_on == ["lock_overlap", "drift"]
    assert cfg.advise_on_resolution is True


def test_lane_overrides():
    text = """\
[cgraph]
enabled = true

[cgraph.lanes.a]
disable_advise = true

[cgraph.lanes.b]
advise_on = ["drift"]
"""
    cfg = _parse_cgraph_section(text)
    assert "a" in cfg.lanes
    assert cfg.lanes["a"].disable_advise is True
    assert cfg.lanes["a"].advise_on is None

    assert "b" in cfg.lanes
    assert cfg.lanes["b"].disable_advise is False
    assert cfg.lanes["b"].advise_on == ["drift"]


def test_ignores_non_cgraph_sections():
    text = """\
[agents]
active = ["claude", "codex"]

[cgraph]
enabled = true

[lanes]
ids = ["a", "b"]
"""
    cfg = _parse_cgraph_section(text)
    assert cfg.enabled is True
    assert len(cfg.lanes) == 0


def test_comments_stripped():
    text = """\
[cgraph]
enabled = true  # enable cgraph
bin_path = "kkg"  # default binary
"""
    cfg = _parse_cgraph_section(text)
    assert cfg.enabled is True
    assert cfg.bin_path == "kkg"


# ---------------------------------------------------------------------------
# resolve_cgraph_config
# ---------------------------------------------------------------------------

def test_resolve_no_file():
    """No project.toml → defaults."""
    cfg = resolve_cgraph_config(Path("/nonexistent/project.toml"))
    assert cfg.enabled is False
    assert cfg.bin_path == "kkg"


def test_resolve_from_file(tmp_path):
    toml = tmp_path / ".btrain" / "project.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("""\
[cgraph]
enabled = true
db_path = "/tmp/test-db"
""")
    cfg = resolve_cgraph_config(toml)
    assert cfg.enabled is True
    assert cfg.db_path == Path("/tmp/test-db")
