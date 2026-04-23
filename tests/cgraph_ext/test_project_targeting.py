"""Tests for project targeting and per-project local backend routing."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import codegraphcontext.cli.main as cli_main
import codegraphcontext.cli.cli_helpers as cli_helpers
from codegraphcontext.cli.main import app as main_app
from codegraphcontext.cli.config_manager import ResolvedContext
from codegraphcontext_ext.commands.audit import audit_command
from codegraphcontext_ext.commands.blast_radius import blast_radius_command
from codegraphcontext_ext.commands.context import context_command
from codegraphcontext_ext.commands.drift_check import drift_check_command
from codegraphcontext_ext.commands.embed import embed_command
from codegraphcontext_ext.commands.export_embeddings import export_embeddings_command
from codegraphcontext_ext.commands.review_packet import review_packet_command
from codegraphcontext_ext.commands.viz_embeddings import viz_embeddings_command
from codegraphcontext_ext.commands.viz_graph import viz_graph_command
from codegraphcontext_ext.commands.viz_projector import viz_projector_command
from codegraphcontext_ext.project import activate_project, resolve_project_target

from .conftest import build_ext_app

runner = CliRunner()


def test_resolve_project_target_cli_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CGRAPH_PROJECT", "env-proj")
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)

    target = resolve_project_target("cli-proj", start_dir=tmp_path)

    assert target.slug == "cli-proj"
    assert target.source == "cli"
    assert target.database == "falkordb"
    assert target.db_path == tmp_path / "db" / "cli-proj" / "falkordb"
    assert target.path_env == "FALKORDB_PATH"
    assert target.socket_path == tmp_path / "db" / "cli-proj" / "falkordb.sock"


def test_resolve_project_target_env_beats_project_toml(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "src"
    nested.mkdir(parents=True)
    (repo / ".cgraph").mkdir()
    (repo / ".cgraph" / "project.toml").write_text('project = "toml-proj"\n', encoding="utf-8")
    monkeypatch.setenv("CGRAPH_PROJECT", "env-proj")
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)

    target = resolve_project_target(start_dir=nested)

    assert target.slug == "env-proj"
    assert target.source == "env"
    assert target.database == "falkordb"


def test_resolve_project_target_toml_beats_basename(monkeypatch, tmp_path):
    repo = tmp_path / "Strange Repo"
    nested = repo / "pkg"
    nested.mkdir(parents=True)
    (repo / ".cgraph").mkdir()
    (repo / ".cgraph" / "project.toml").write_text('project = "flask"\n', encoding="utf-8")
    monkeypatch.delenv("CGRAPH_PROJECT", raising=False)
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)

    target = resolve_project_target(start_dir=nested)

    assert target.slug == "flask"
    assert target.source == "toml"


def test_resolve_project_target_rejects_reserved_slugs(monkeypatch, tmp_path):
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))

    with pytest.raises(ValueError):
        resolve_project_target("global", start_dir=tmp_path)


def test_activate_project_creates_project_directory_and_sets_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.delenv("KUZUDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_SOCKET_PATH", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)

    target = activate_project("flask", start_dir=tmp_path)

    assert target.database == "falkordb"
    assert target.db_path == tmp_path / "db" / "flask" / "falkordb"
    assert target.db_path.parent.is_dir()
    assert os.environ["KUZUDB_PATH"] == str(tmp_path / "db" / "flask" / "kuzudb")
    assert os.environ["FALKORDB_PATH"] == str(target.db_path)
    assert os.environ["FALKORDB_SOCKET_PATH"] == str(tmp_path / "db" / "flask" / "falkordb.sock")


def test_resolve_project_target_uses_legacy_cgraph_store_when_present(monkeypatch, tmp_path):
    db_root = tmp_path / "db"
    db_root.mkdir()
    legacy = db_root / "kuzudb"
    legacy.write_text("", encoding="utf-8")
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(db_root))
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")

    target = resolve_project_target("cgraph", start_dir=tmp_path)

    assert target.database == "kuzudb"
    assert target.db_path == legacy


def test_project_resolution_does_not_leak_between_subprocess_invocations(tmp_path):
    repo_src = Path(__file__).resolve().parents[2] / "src"
    db_root = tmp_path / "db"
    flask_repo = tmp_path / "flask"
    redis_repo = tmp_path / "redis"
    flask_repo.mkdir()
    redis_repo.mkdir()

    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(repo_src)!r})\n"
        "from codegraphcontext_ext.project import activate_project\n"
        "target = activate_project(start_dir=Path.cwd())\n"
        "print(target.slug)\n"
        "print(target.db_path)\n"
    )

    env = os.environ.copy()
    env["CGRAPH_DB_ROOT"] = str(db_root)
    env.pop("CGRAPH_PROJECT", None)
    env.pop("CGC_RUNTIME_DB_TYPE", None)
    env.pop("DEFAULT_DATABASE", None)
    env.pop("KUZUDB_PATH", None)
    env.pop("FALKORDB_PATH", None)
    env.pop("FALKORDB_SOCKET_PATH", None)

    out_flask = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=flask_repo,
        env=env,
        text=True,
    ).strip().splitlines()
    out_redis = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=redis_repo,
        env=env,
        text=True,
    ).strip().splitlines()

    assert out_flask == ["flask", str(db_root / "flask" / "falkordb")]
    assert out_redis == ["redis", str(db_root / "redis" / "falkordb")]


def test_db_touching_commands_expose_project_option():
    command_functions = [
        audit_command,
        blast_radius_command,
        context_command,
        drift_check_command,
        embed_command,
        export_embeddings_command,
        review_packet_command,
        viz_embeddings_command,
        viz_graph_command,
        viz_projector_command,
        cli_main.index,
        cli_main.watch,
        cli_main.index_abbrev,
        cli_main.watch_abbrev,
    ]

    for fn in command_functions:
        assert "project" in inspect.signature(fn).parameters


def test_embed_command_routes_kuzudb_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setattr(
        "codegraphcontext_ext.commands.embed.probe_backend_support",
        lambda: {"ok": True, "backend": "kuzudb"},
    )
    monkeypatch.setattr(
        "codegraphcontext_ext.commands.embed.build_model_check_payload",
        lambda config, backend: {"ok": True, "kind": "ready", "backend": backend},
    )

    result = runner.invoke(build_ext_app(), ["embed", "--check-model", "--project", "flask"])

    assert result.exit_code == 0
    assert os.environ["KUZUDB_PATH"] == str(tmp_path / "db" / "flask" / "kuzudb")


def test_index_command_routes_falkordb_path(monkeypatch, tmp_path):
    repo = tmp_path / "flask"
    repo.mkdir()
    seen: dict[str, str] = {}
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "index_helper",
        lambda path, context: seen.update(path=path, env=os.environ.get("FALKORDB_PATH", "")),
    )

    result = runner.invoke(main_app, ["index", "--project", "flask", str(repo)])

    assert result.exit_code == 0
    assert seen["path"] == str(repo)
    assert seen["env"] == str(tmp_path / "db" / "flask" / "falkordb")


def test_watch_command_routes_falkordb_path(monkeypatch, tmp_path):
    repo = tmp_path / "flask"
    repo.mkdir()
    seen: dict[str, str] = {}
    monkeypatch.setenv("CGRAPH_DB_ROOT", str(tmp_path / "db"))
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "watch_helper",
        lambda path, context: seen.update(path=path, env=os.environ.get("FALKORDB_PATH", "")),
    )

    result = runner.invoke(main_app, ["watch", "--project", "flask", str(repo)])

    assert result.exit_code == 0
    assert seen["path"] == str(repo)
    assert seen["env"] == str(tmp_path / "db" / "flask" / "falkordb")


def test_initialize_services_prefers_project_kuzudb_path(monkeypatch, tmp_path):
    project_db = tmp_path / "db" / "flask" / "kuzudb"
    ctx = ResolvedContext(
        mode="global",
        context_name="",
        database="kuzudb",
        db_path=str(tmp_path / "global" / "kuzudb"),
        cgcignore_path=str(tmp_path / ".cgcignore"),
    )
    seen: dict[str, str] = {}

    class _FakeDBManager:
        def get_driver(self):
            return object()

    monkeypatch.setenv("KUZUDB_PATH", str(project_db))
    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(cli_helpers, "resolve_context", lambda _flag=None: ctx)
    monkeypatch.setattr(cli_helpers, "GraphBuilder", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_helpers, "CodeFinder", lambda *args, **kwargs: object())

    def _fake_get_database_manager(db_path=None):
        seen["db_path"] = db_path
        return _FakeDBManager()

    monkeypatch.setattr(cli_helpers, "get_database_manager", _fake_get_database_manager)

    *_services, resolved = cli_helpers._initialize_services()

    assert seen["db_path"] == str(project_db)
    assert resolved.db_path == str(project_db)


def test_initialize_services_prefers_project_falkordb_path(monkeypatch, tmp_path):
    project_db = tmp_path / "db" / "flask" / "falkordb"
    ctx = ResolvedContext(
        mode="global",
        context_name="",
        database="falkordb",
        db_path=str(tmp_path / "global" / "falkordb"),
        cgcignore_path=str(tmp_path / ".cgcignore"),
    )
    seen: dict[str, str] = {}

    class _FakeDBManager:
        def get_driver(self):
            return object()

    monkeypatch.setenv("FALKORDB_PATH", str(project_db))
    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(cli_helpers, "resolve_context", lambda _flag=None: ctx)
    monkeypatch.setattr(cli_helpers, "GraphBuilder", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_helpers, "CodeFinder", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "codegraphcontext_ext.project.reset_local_db_manager",
        lambda database, db_path: seen.update(reset_db=database, reset_path=str(db_path)),
    )

    def _fake_get_database_manager(db_path=None):
        seen["db_path"] = db_path
        return _FakeDBManager()

    monkeypatch.setattr(cli_helpers, "get_database_manager", _fake_get_database_manager)

    *_services, resolved = cli_helpers._initialize_services()

    assert seen["reset_db"] == "falkordb"
    assert seen["reset_path"] == str(project_db)
    assert seen["db_path"] == str(project_db)
    assert resolved.db_path == str(project_db)
