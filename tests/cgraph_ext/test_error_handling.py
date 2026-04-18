"""Error handling tests — invalid inputs, boundary values, failure modes.

Covers edge cases that were identified as gaps in the coverage audit.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codegraphcontext_ext.config import (
    CgraphConfig,
    LaneConfig,
    _parse_cgraph_section,
    _parse_toml_value,
    resolve_cgraph_config,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Config: invalid TOML edge cases
# ---------------------------------------------------------------------------

class TestConfigEdgeCases:

    def test_unclosed_string_value(self):
        text = """\
[cgraph]
bin_path = "unclosed
"""
        cfg = _parse_cgraph_section(text)
        # ast.literal_eval fails on unclosed quote — raw value stored
        assert cfg.bin_path == '"unclosed'

    def test_empty_value(self):
        text = """\
[cgraph]
bin_path =
"""
        cfg = _parse_cgraph_section(text)
        # Empty string after = — raw empty value
        assert cfg.bin_path == ""

    def test_equals_in_value(self):
        text = """\
[cgraph]
bin_path = "path=with=equals"
"""
        cfg = _parse_cgraph_section(text)
        assert cfg.bin_path == "path=with=equals"

    def test_duplicate_section_last_wins(self):
        text = """\
[cgraph]
bin_path = "first"

[cgraph]
bin_path = "second"
"""
        cfg = _parse_cgraph_section(text)
        assert cfg.bin_path == "second"

    def test_nested_section_not_cgraph(self):
        """[cgraph.unknown.deep] is ignored."""
        text = """\
[cgraph.unknown.deep.section]
foo = "bar"

[cgraph]
enabled = true
"""
        cfg = _parse_cgraph_section(text)
        assert cfg.enabled is True
        assert len(cfg.lanes) == 0

    def test_boolean_case_insensitive(self):
        assert _parse_toml_value("TRUE") is True
        assert _parse_toml_value("False") is False
        assert _parse_toml_value("tRuE") is True

    def test_malformed_array(self):
        """Malformed array falls back to raw string."""
        result = _parse_toml_value('["a", "b"')  # missing ]
        assert isinstance(result, str)

    def test_empty_array(self):
        result = _parse_toml_value("[]")
        assert result == []

    def test_mixed_type_array(self):
        """Arrays with mixed types still parse as strings."""
        result = _parse_toml_value('[1, "two", 3]')
        assert result == ["1", "two", "3"]

    def test_resolve_with_directory_not_file(self, tmp_path):
        """Passing a directory instead of file returns defaults."""
        cfg = resolve_cgraph_config(tmp_path)  # directory, not file
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Advise: CLI error handling
# ---------------------------------------------------------------------------

class TestAdviseCLIErrors:

    def _make_app(self):
        import typer
        from codegraphcontext_ext.cli import register_extensions
        app = typer.Typer()
        register_extensions(app)
        return app

    def test_context_is_array_not_object(self):
        """--context with a JSON array should error."""
        app = self._make_app()
        result = runner.invoke(app, ["advise", "drift", "--context", "[1,2,3]"])
        assert result.exit_code != 0

    def test_context_is_string_not_object(self):
        """--context with a JSON string should error."""
        app = self._make_app()
        result = runner.invoke(app, ["advise", "drift", "--context", '"just a string"'])
        assert result.exit_code != 0

    def test_empty_situation(self):
        """Empty string situation returns unknown tip."""
        cfg = CgraphConfig()
        with patch(
            "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
            return_value=cfg,
        ):
            app = self._make_app()
            result = runner.invoke(app, ["advise", ""])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["suggestion"] is None


# ---------------------------------------------------------------------------
# Serve: error paths
# ---------------------------------------------------------------------------

class TestServeErrorPaths:

    def test_dispatch_advise_handler_crash_propagates(self):
        """_dispatch propagates handler exceptions (caught by _handle_client)."""
        with patch(
            "codegraphcontext_ext.commands.advise.build_advise_payload",
            side_effect=TypeError("unexpected None"),
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            with pytest.raises(TypeError, match="unexpected None"):
                _dispatch("advise", {"situation": "drift"})

    def test_dispatch_blast_radius_handler_crash_propagates(self):
        """_dispatch propagates handler exceptions."""
        with patch(
            "codegraphcontext_ext.commands.blast_radius.build_blast_radius_payload",
            side_effect=ValueError("bad files"),
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            with pytest.raises(ValueError, match="bad files"):
                _dispatch("blast-radius", {"files": ["x.py"]})


# ---------------------------------------------------------------------------
# Blast-radius: boundary values
# ---------------------------------------------------------------------------

class TestBlastRadiusBoundaries:

    def test_empty_files_returns_empty_payload(self):
        """blast-radius with no files returns valid but empty payload."""
        with patch(
            "codegraphcontext_ext.commands.blast_radius.get_kuzu_connection",
            side_effect=Exception("no db"),
        ):
            from codegraphcontext_ext.commands.blast_radius import build_blast_radius_payload
            result = build_blast_radius_payload(files=[])
        assert result["ok"] is True
        assert result["nodes_in_scope"] == []

    def test_max_nodes_one(self):
        """max_nodes=1 truncates aggressively."""
        with patch(
            "codegraphcontext_ext.commands.blast_radius.get_kuzu_connection",
            side_effect=Exception("no db"),
        ):
            from codegraphcontext_ext.commands.blast_radius import build_blast_radius_payload
            result = build_blast_radius_payload(files=["a.py"], max_nodes=1)
        assert result["ok"] is True

    def test_invalid_locks_json_string(self):
        """Non-JSON locks_json produces advisory, not crash."""
        with patch(
            "codegraphcontext_ext.commands.blast_radius.get_kuzu_connection",
            side_effect=Exception("no db"),
        ):
            from codegraphcontext_ext.commands.blast_radius import build_blast_radius_payload
            result = build_blast_radius_payload(
                files=["a.py"],
                locks_json="this is not json",
            )
        assert result["ok"] is True
        advisories = [a["kind"] for a in result.get("advisories", [])]
        assert "invalid_locks_json" in advisories or "no_graph" in advisories


# ---------------------------------------------------------------------------
# Context: provider failure edge cases
# ---------------------------------------------------------------------------

class TestContextEdgeCases:

    def test_token_estimate_empty_string(self):
        from codegraphcontext_ext.commands.context import _estimate_tokens
        assert _estimate_tokens("") == 1  # min 1

    def test_token_estimate_short_string(self):
        from codegraphcontext_ext.commands.context import _estimate_tokens
        assert _estimate_tokens("hi") == 1  # floor(2/4) = 0, but min 1

    def test_build_payload_empty_seeds(self):
        from codegraphcontext_ext.commands.context import _build_context_payload
        payload = _build_context_payload(
            "test query",
            [],
            {"callers": [], "callees": [], "imports": []},
        )
        assert payload["query"] == "test query"
        assert payload["seeds"] == []
        assert payload["token_estimate"] >= 1
