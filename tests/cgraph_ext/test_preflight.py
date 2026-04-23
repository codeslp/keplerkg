"""Tests for the Phase 1.5 Step 7 fail-closed preflight."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from codegraphcontext_ext.preflight import (
    _requires_mount,
    check_storage,
    require_storage,
)


@pytest.fixture(autouse=True)
def _clear_preflight_env(monkeypatch):
    """Keep preflight expectations stable regardless of prior project activation."""
    for key in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "KUZUDB_PATH",
        "FALKORDB_PATH",
        "FALKORDB_SOCKET_PATH",
        "HF_HOME",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# _requires_mount
# ---------------------------------------------------------------------------

def test_requires_mount_volumes_path():
    assert _requires_mount("/Volumes/zombie/cgraph/db/kuzudb") == "/Volumes/zombie"


def test_requires_mount_nested():
    assert _requires_mount("/Volumes/backup/data") == "/Volumes/backup"


def test_requires_mount_local_path():
    assert _requires_mount("/Users/someone/data") is None


def test_requires_mount_relative():
    # Relative paths resolve against cwd — should not match /Volumes
    assert _requires_mount("relative/path") is None


# ---------------------------------------------------------------------------
# check_storage — all clear
# ---------------------------------------------------------------------------

def test_check_storage_no_paths(monkeypatch):
    """No active backend path or HF_HOME → nothing to check → None."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.delenv("KUZUDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    # Prevent upstream config lookup from finding a value
    with patch(
        "codegraphcontext_ext.preflight.check_storage.__module__",
        new="codegraphcontext_ext.preflight",
    ):
        monkeypatch.delenv("KUZUDB_PATH", raising=False)
        monkeypatch.delenv("FALKORDB_PATH", raising=False)
        monkeypatch.delenv("FALKORDB_SOCKET_PATH", raising=False)
        monkeypatch.delenv("HF_HOME", raising=False)
        # Patch the upstream import away
        with patch(
            "codegraphcontext.cli.config_manager.get_config_value",
            return_value=None,
        ):
            result = check_storage()
    assert result is None


def test_check_storage_local_paths(monkeypatch):
    """Paths on local disk → no mount needed → None."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Users/someone/data/falkordb")
    monkeypatch.setenv("FALKORDB_SOCKET_PATH", "/Users/someone/data/falkordb.sock")
    monkeypatch.setenv("HF_HOME", "/Users/someone/cache/hf")
    assert check_storage() is None


def test_check_storage_mounted(monkeypatch):
    """Paths under /Volumes/zombie but zombie IS mounted → None."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Volumes/zombie/cgraph/db/falkordb")
    monkeypatch.setenv("HF_HOME", "/Volumes/zombie/cgraph/hf-cache")
    with patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value={"/Volumes/zombie", "/Volumes/other"},
    ):
        assert check_storage() is None


# ---------------------------------------------------------------------------
# check_storage — offline
# ---------------------------------------------------------------------------

def test_check_storage_unmounted(monkeypatch):
    """Zombie not mounted → storage_offline payload."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Volumes/zombie/cgraph/db/falkordb")
    monkeypatch.delenv("HF_HOME", raising=False)
    with patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value={"/Volumes/other"},
    ):
        result = check_storage()
    assert result is not None
    assert result["ok"] is False
    assert result["kind"] == "storage_offline"
    assert len(result["offline"]) == 1
    assert result["offline"][0]["variable"] == "FALKORDB_PATH"
    assert result["offline"][0]["mount_point"] == "/Volumes/zombie"


def test_check_storage_both_offline(monkeypatch):
    """Active backend path and HF_HOME on an unmounted volume."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Volumes/zombie/cgraph/db/falkordb")
    monkeypatch.setenv("HF_HOME", "/Volumes/zombie/cgraph/hf-cache")
    with patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value=set(),
    ):
        result = check_storage()
    assert result is not None
    assert len(result["offline"]) == 2
    variables = {e["variable"] for e in result["offline"]}
    assert variables == {"FALKORDB_PATH", "HF_HOME"}


def test_check_storage_partial_offline(monkeypatch):
    """Active backend path local but HF_HOME on unmounted volume."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Users/someone/data/falkordb")
    monkeypatch.setenv("HF_HOME", "/Volumes/zombie/cgraph/hf-cache")
    with patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value=set(),
    ):
        result = check_storage()
    assert result is not None
    assert len(result["offline"]) == 1
    assert result["offline"][0]["variable"] == "HF_HOME"


# ---------------------------------------------------------------------------
# check_storage — upstream config fallback
# ---------------------------------------------------------------------------

def test_check_storage_reads_upstream_config(monkeypatch):
    """When FALKORDB_PATH is not in env, falls back to upstream config_manager."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.delenv("KUZUDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    with patch(
        "codegraphcontext.cli.config_manager.get_config_value",
        side_effect=lambda key: {
            "DEFAULT_DATABASE": "falkordb",
            "FALKORDB_PATH": "/Volumes/zombie/cgraph/db/falkordb",
            "FALKORDB_SOCKET_PATH": None,
        }.get(key),
    ), patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value=set(),
    ):
        result = check_storage()
    assert result is not None
    assert result["offline"][0]["variable"] == "FALKORDB_PATH"


def test_check_storage_honors_explicit_kuzudb_backend(monkeypatch):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setenv("KUZUDB_PATH", "/Volumes/zombie/cgraph/db/kuzudb")
    monkeypatch.delenv("FALKORDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_SOCKET_PATH", raising=False)
    with patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value=set(),
    ):
        result = check_storage()
    assert result is not None
    assert result["offline"][0]["variable"] == "KUZUDB_PATH"


# ---------------------------------------------------------------------------
# require_storage
# ---------------------------------------------------------------------------

def test_require_storage_exits_on_offline(monkeypatch):
    """require_storage() raises SystemExit(1) when offline."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Volumes/zombie/cgraph/db/falkordb")
    with patch(
        "codegraphcontext_ext.preflight._mounted_volumes",
        return_value=set(),
    ):
        with pytest.raises(SystemExit) as exc_info:
            require_storage()
        assert exc_info.value.code == 1


def test_require_storage_passes_when_ok(monkeypatch):
    """require_storage() returns None when storage is available."""
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("FALKORDB_PATH", "/Users/local/falkordb")
    monkeypatch.delenv("HF_HOME", raising=False)
    # Should not raise
    require_storage()
