"""Tests for viz_server helpers shared by kkg viz-dashboard/viz-projector."""

from __future__ import annotations

from pathlib import Path

from codegraphcontext_ext.viz_server import _discover_projects


def _make_kuzu_store(project_dir: Path) -> Path:
    """Create a minimal Kuzu-style project store: <slug>/kuzudb/ dir."""
    kuzu = project_dir / "kuzudb"
    kuzu.mkdir(parents=True)
    (kuzu / "catalog.kz").write_bytes(b"\0" * 1024)
    return kuzu


def _make_falkor_store(project_dir: Path) -> Path:
    """Create a minimal Falkor-style project store: <slug>/falkordb file."""
    project_dir.mkdir(parents=True, exist_ok=True)
    falkor_db = project_dir / "falkordb"
    falkor_db.write_bytes(b"\0" * 2048)
    (project_dir / "falkordb.sock").write_bytes(b"")
    return falkor_db


def test_discover_projects_lists_kuzu_falkor_and_mixed_backends(tmp_path, monkeypatch):
    db_root = tmp_path / "db"
    db_root.mkdir()

    _make_kuzu_store(db_root / "kuzu-only")
    _make_falkor_store(db_root / "falkor-only")
    _make_kuzu_store(db_root / "both")
    _make_falkor_store(db_root / "both")

    monkeypatch.setenv("CGRAPH_DB_ROOT", str(db_root))
    projects = _discover_projects()

    by_key = {(p["slug"], p["backend"]): p for p in projects}
    assert set(by_key) == {
        ("both", "kuzudb"),
        ("both", "falkordb"),
        ("falkor-only", "falkordb"),
        ("kuzu-only", "kuzudb"),
    }

    assert by_key[("kuzu-only", "kuzudb")]["path"].endswith("/kuzu-only/kuzudb")
    assert by_key[("falkor-only", "falkordb")]["path"].endswith("/falkor-only/falkordb")

    for entry in projects:
        assert entry["size_mb"] >= 0
        assert isinstance(entry["size_mb"], float)


def test_discover_projects_skips_dotfiles_and_test_prefixes(tmp_path, monkeypatch):
    db_root = tmp_path / "db"
    db_root.mkdir()

    _make_kuzu_store(db_root / "real-project")
    _make_kuzu_store(db_root / "test-fixture")
    _make_kuzu_store(db_root / ".hidden")

    monkeypatch.setenv("CGRAPH_DB_ROOT", str(db_root))
    slugs = {p["slug"] for p in _discover_projects()}
    assert slugs == {"real-project"}


def test_discover_projects_returns_empty_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "nonexistent"))
    assert _discover_projects() == []
