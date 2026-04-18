"""Tests for io/kuzu.py — the shared KùzuDB connection accessor.

Focuses on the preflight gate: get_kuzu_connection() must call
require_storage() before touching KuzuDBManager.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


class TestGetKuzuConnection:
    """Verify get_kuzu_connection() preflight and delegation."""

    def test_calls_require_storage_before_db(self, monkeypatch):
        """require_storage() is called before KuzuDBManager is instantiated."""
        call_order = []

        def mock_require():
            call_order.append("require_storage")

        mock_manager_cls = MagicMock()
        mock_driver = MagicMock()
        mock_driver.conn = "test_conn"
        mock_manager_cls.return_value.get_driver.return_value = mock_driver

        def mock_manager_init(*a, **kw):
            call_order.append("KuzuDBManager")
            return mock_manager_cls.return_value

        mock_manager_cls.side_effect = mock_manager_init

        with patch(
            "codegraphcontext_ext.preflight.require_storage",
            mock_require,
        ), patch(
            "codegraphcontext.core.database_kuzu.KuzuDBManager",
            mock_manager_cls,
        ):
            from codegraphcontext_ext.io.kuzu import get_kuzu_connection
            get_kuzu_connection()

        assert call_order[0] == "require_storage"

    def test_propagates_preflight_exit(self, monkeypatch):
        """If require_storage() raises SystemExit, it propagates."""
        monkeypatch.setenv("KUZUDB_PATH", "/Volumes/gone/db")
        with patch(
            "codegraphcontext_ext.preflight._mounted_volumes",
            return_value=set(),
        ):
            with pytest.raises(SystemExit):
                from codegraphcontext_ext.io.kuzu import get_kuzu_connection
                get_kuzu_connection()

    def test_returns_connection_on_success(self):
        """Happy path: returns driver.conn from KuzuDBManager."""
        mock_manager = MagicMock()
        mock_driver = MagicMock()
        mock_driver.conn = "real_conn"
        mock_manager.return_value.get_driver.return_value = mock_driver

        with patch(
            "codegraphcontext_ext.preflight.require_storage",
        ), patch(
            "codegraphcontext.core.database_kuzu.KuzuDBManager",
            mock_manager,
        ):
            from codegraphcontext_ext.io.kuzu import get_kuzu_connection
            assert get_kuzu_connection() == "real_conn"
