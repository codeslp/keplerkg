"""Integration tests: cross-module interactions between preflight, config, advise, and serve.

These tests verify that modules compose correctly — not just that each
works in isolation.  They mock at system boundaries (filesystem, DB,
subprocess) but let the real module code interact.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext_ext.config import CgraphConfig, LaneConfig


# ---------------------------------------------------------------------------
# Preflight → command pipeline
# ---------------------------------------------------------------------------

class TestPreflightBlocksCommands:
    """Verify that commands fail closed when storage is offline."""

    def test_get_kuzu_connection_fails_when_offline(self, monkeypatch):
        """io/kuzu.py calls require_storage() before touching KuzuDBManager."""
        monkeypatch.setenv("KUZUDB_PATH", "/Volumes/ghost/db/kuzudb")
        with patch(
            "codegraphcontext_ext.preflight._mounted_volumes",
            return_value=set(),
        ):
            with pytest.raises(SystemExit) as exc_info:
                from codegraphcontext_ext.io.kuzu import get_kuzu_connection
                get_kuzu_connection()
            assert exc_info.value.code == 1

    def test_preflight_passes_then_db_accessed(self, monkeypatch):
        """When storage is online, preflight passes and KuzuDBManager is called."""
        monkeypatch.setenv("KUZUDB_PATH", "/Volumes/zombie/db/kuzudb")
        mock_manager = MagicMock()
        mock_driver = MagicMock()
        mock_manager.return_value.get_driver.return_value = mock_driver
        mock_driver.conn = "fake_conn"

        with patch(
            "codegraphcontext_ext.preflight._mounted_volumes",
            return_value={"/Volumes/zombie"},
        ), patch(
            "codegraphcontext.core.database_kuzu.KuzuDBManager",
            mock_manager,
        ):
            from codegraphcontext_ext.io.kuzu import get_kuzu_connection
            conn = get_kuzu_connection()
            assert conn == "fake_conn"

    def test_preflight_skipped_when_local_path(self, monkeypatch):
        """Local paths don't trigger mount checks."""
        monkeypatch.setenv("KUZUDB_PATH", "/Users/test/local/kuzudb")
        monkeypatch.delenv("HF_HOME", raising=False)
        mock_manager = MagicMock()
        mock_driver = MagicMock()
        mock_manager.return_value.get_driver.return_value = mock_driver
        mock_driver.conn = "local_conn"

        with patch(
            "codegraphcontext.core.database_kuzu.KuzuDBManager",
            mock_manager,
        ):
            from codegraphcontext_ext.io.kuzu import get_kuzu_connection
            conn = get_kuzu_connection()
            assert conn == "local_conn"


# ---------------------------------------------------------------------------
# Config → advise pipeline
# ---------------------------------------------------------------------------

class TestConfigDrivesAdvise:
    """Verify that config settings flow through to advise behavior."""

    def test_advise_respects_lane_disable(self, tmp_path):
        """advise returns suppressed payload when lane has disable_advise=true."""
        toml = tmp_path / ".btrain" / "project.toml"
        toml.parent.mkdir(parents=True)
        toml.write_text("""\
[cgraph]
enabled = true

[cgraph.lanes.b]
disable_advise = true
""")
        with patch(
            "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        ) as mock_cfg:
            from codegraphcontext_ext.config import resolve_cgraph_config as real_resolve
            mock_cfg.return_value = real_resolve(toml)

            from codegraphcontext_ext.commands.advise import build_advise_payload
            result = build_advise_payload("lock_overlap", lane="b")

        assert result["suggestion"] is None
        assert "disabled" in result["rationale"].lower()

    def test_advise_respects_advise_on_filter(self, tmp_path):
        """Only situations in advise_on get tips."""
        toml = tmp_path / ".btrain" / "project.toml"
        toml.parent.mkdir(parents=True)
        toml.write_text("""\
[cgraph]
enabled = true
advise_on = ["drift"]
""")
        with patch(
            "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
        ) as mock_cfg:
            from codegraphcontext_ext.config import resolve_cgraph_config as real_resolve
            mock_cfg.return_value = real_resolve(toml)

            from codegraphcontext_ext.commands.advise import build_advise_payload
            # drift is allowed
            drift = build_advise_payload("drift", lane="a")
            assert drift["suggestion"] is not None

            # lock_overlap is filtered out
            overlap = build_advise_payload("lock_overlap", lane="a")
            assert overlap["suggestion"] is None

    def test_advise_works_without_btrain(self):
        """When no .btrain/project.toml exists, advise still works with defaults."""
        with patch(
            "codegraphcontext_ext.config.find_btrain_project_toml",
            return_value=None,
        ):
            from codegraphcontext_ext.commands.advise import build_advise_payload
            result = build_advise_payload("lock_overlap", {"files": "src/"})
        assert result["suggestion"] is not None
        assert result["advisory_id"] is not None


# ---------------------------------------------------------------------------
# Daemon → command dispatch integration
# ---------------------------------------------------------------------------

class TestDaemonDispatchIntegration:
    """Verify daemon correctly routes to command handlers."""

    def test_daemon_advise_with_config(self):
        """Daemon advise dispatch uses real config resolution."""
        cfg = CgraphConfig(advise_on=["lock_overlap", "drift", "packet_truncated"])
        with patch(
            "codegraphcontext_ext.commands.advise.resolve_cgraph_config",
            return_value=cfg,
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            result = _dispatch("advise", {
                "situation": "lock_overlap",
                "lane": "a",
                "context": {"files": "src/auth/"},
            })
        assert result["situation"] == "lock_overlap"
        assert "blast-radius" in result["suggestion"]

    def test_daemon_blast_radius_without_db(self):
        """blast-radius dispatch degrades gracefully without DB."""
        with patch(
            "codegraphcontext_ext.commands.blast_radius.get_kuzu_connection",
            side_effect=Exception("DB unavailable"),
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            result = _dispatch("blast-radius", {"files": ["src/foo.py"]})
        assert result["ok"] is True
        advisories = result.get("advisories", [])
        assert any(a["kind"] == "no_graph" for a in advisories)

    def test_daemon_dispatch_propagates_exceptions(self):
        """_dispatch raises; _handle_client wraps it as internal_error."""
        with patch(
            "codegraphcontext_ext.commands.advise.build_advise_payload",
            side_effect=RuntimeError("boom"),
        ):
            from codegraphcontext_ext.daemon.serve import _dispatch
            with pytest.raises(RuntimeError, match="boom"):
                _dispatch("advise", {"situation": "drift"})
