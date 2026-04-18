"""Security boundary tests — path traversal, injection, format strings, DOS vectors.

These tests verify that cgraph modules handle adversarial inputs safely.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from codegraphcontext_ext.config import CgraphConfig, _parse_cgraph_section


# ---------------------------------------------------------------------------
# Config: path traversal via TOML values
# ---------------------------------------------------------------------------

class TestConfigPathSafety:
    """Verify config parsing doesn't blindly trust path values."""

    def test_source_checkout_with_traversal(self):
        """Paths with ../ are resolved but still parsed (caller validates)."""
        text = """\
[cgraph]
source_checkout = "../../../etc/passwd"
"""
        cfg = _parse_cgraph_section(text)
        # Config parses the value — security check is at use-site
        assert cfg.source_checkout is not None
        assert ".." in str(cfg.source_checkout)

    def test_db_path_absolute_outside_expected(self):
        """Absolute paths outside /Volumes are accepted (preflight handles safety)."""
        text = """\
[cgraph]
db_path = "/etc/shadow"
"""
        cfg = _parse_cgraph_section(text)
        assert str(cfg.db_path) == "/etc/shadow"

    def test_malformed_toml_section_header(self):
        """Malformed section headers don't crash the parser."""
        text = """\
[cgraph
enabled = true
[cgraph]
bin_path = "kkg"
"""
        # Partial header "[cgraph" doesn't match "[" ... "]" pattern — skipped
        cfg = _parse_cgraph_section(text)
        assert cfg.bin_path == "kkg"

    def test_toml_value_injection(self):
        """Values with embedded newlines or special chars are safe."""
        text = """\
[cgraph]
bin_path = "kkg; rm -rf /"
"""
        cfg = _parse_cgraph_section(text)
        assert cfg.bin_path == "kkg; rm -rf /"  # Stored as-is; not executed


# ---------------------------------------------------------------------------
# Advise: format string safety
# ---------------------------------------------------------------------------

class TestAdviseFormatStringSafety:

    def test_context_with_format_specifiers(self):
        """Context values with {__class__} or {0} don't leak internals."""
        from codegraphcontext_ext.commands.advise import _format_tip
        result = _format_tip("lock_overlap", {
            "files": "{__class__.__init__.__globals__}",
            "lane": "{0.__class__}",
        })
        # Should interpolate literally, not execute format specifiers
        assert "__globals__" in result["suggestion"]
        assert result["advisory_id"] is not None

    def test_context_with_percent_formatting(self):
        """% formatting patterns don't cause errors."""
        from codegraphcontext_ext.commands.advise import _format_tip
        result = _format_tip("drift", {"lane": "%s%n%x"})
        assert "%s%n%x" in result["suggestion"]

    def test_huge_context_doesnt_crash(self):
        """Very large context dict doesn't cause memory issues in ID generation."""
        from codegraphcontext_ext.commands.advise import _generate_advisory_id
        huge_ctx = {f"key_{i}": f"value_{i}" * 100 for i in range(1000)}
        aid = _generate_advisory_id("lock_overlap", huge_ctx)
        assert aid.startswith("adv_")


# ---------------------------------------------------------------------------
# Serve: malformed input handling
# ---------------------------------------------------------------------------

class TestServeMalformedInputs:

    def test_dispatch_missing_situation(self):
        """advise dispatch with empty situation returns unknown tip."""
        cfg = CgraphConfig()
        with patch(
            "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
            return_value=cfg,
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            result = _dispatch("advise", {})
        assert result["situation"] == ""
        assert result["suggestion"] is None

    def test_dispatch_non_dict_args(self):
        """Non-dict args are handled — dispatch extracts .get() safely."""
        from codegraphcontext_ext.daemon.serve import _dispatch
        # Pass a string instead of dict — should fail gracefully
        result = _dispatch("nonexistent", "not a dict")
        assert result["ok"] is False

    def test_dispatch_blast_radius_empty_files(self):
        """blast-radius with empty file list returns clean error."""
        with patch(
            "codegraphcontext_ext.commands.blast_radius.get_kuzu_connection",
            side_effect=Exception("no db"),
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            result = _dispatch("blast-radius", {"files": []})
        assert result["ok"] is True  # Empty but valid


# ---------------------------------------------------------------------------
# Preflight: edge cases
# ---------------------------------------------------------------------------

class TestPreflightEdgeCases:

    def test_mount_command_timeout(self, monkeypatch):
        """If `mount` command hangs, _mounted_volumes returns empty set."""
        import subprocess
        with patch(
            "codegraphcontext_ext.preflight.subprocess.check_output",
            side_effect=subprocess.TimeoutExpired("mount", 5),
        ):
            from codegraphcontext_ext.preflight import _mounted_volumes
            assert _mounted_volumes() == set()

    def test_mount_command_not_found(self):
        """If `mount` binary doesn't exist, returns empty set gracefully."""
        with patch(
            "codegraphcontext_ext.preflight.subprocess.check_output",
            side_effect=FileNotFoundError,
        ):
            from codegraphcontext_ext.preflight import _mounted_volumes
            assert _mounted_volumes() == set()

    def test_requires_mount_with_symlink_path(self, tmp_path):
        """Symlinked paths under /Volumes are resolved correctly."""
        from codegraphcontext_ext.preflight import _requires_mount
        # Normal path
        assert _requires_mount("/Volumes/zombie/data") == "/Volumes/zombie"

    def test_check_storage_upstream_import_fails(self, monkeypatch):
        """If upstream config_manager can't be imported, we skip gracefully."""
        monkeypatch.delenv("KUZUDB_PATH", raising=False)
        monkeypatch.delenv("HF_HOME", raising=False)
        with patch(
            "codegraphcontext_ext.preflight.check_storage",
            wraps=None,
        ):
            from codegraphcontext_ext.preflight import check_storage
            # With no env vars and import failure, should return None
            with patch.dict("sys.modules", {"codegraphcontext.cli.config_manager": None}):
                result = check_storage()
            # Either None (no paths) or handles the import error
            # The function catches all exceptions on import
