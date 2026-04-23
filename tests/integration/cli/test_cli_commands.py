
import ast
import os
import sys
import types
from pathlib import Path
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

import codegraphcontext.cli.main as cli_main
from codegraphcontext.cli.main import app, _load_credentials

runner = CliRunner()


def _command_name_from_decorator(decorator: ast.Call, func_name: str) -> str:
    if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
        return decorator.args[0].value

    for kw in decorator.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value

    return func_name.replace("_", "-")


def _inventory_from_main_source() -> dict[str, set[str]]:
    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    group_alias_to_name: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != "add_typer":
            continue
        if not isinstance(call.func.value, ast.Name) or call.func.value.id != "app":
            continue
        if not call.args or not isinstance(call.args[0], ast.Name):
            continue

        group_alias = call.args[0].id
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                group_alias_to_name[group_alias] = kw.value.value

    inventory: dict[str, set[str]] = {
        "root": set(),
        "mcp": set(),
        "neo4j": set(),
        "config": set(),
        "bundle": set(),
        "registry": set(),
        "find": set(),
        "analyze": set(),
    }

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue

        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr != "command":
                continue
            if not isinstance(decorator.func.value, ast.Name):
                continue

            owner = decorator.func.value.id
            family = "root" if owner == "app" else group_alias_to_name.get(owner)
            if family is None:
                continue

            command_name = _command_name_from_decorator(decorator, node.name)
            inventory.setdefault(family, set()).add(command_name)

    return inventory


class _FakeSession:
    class _FakeResult:
        def __init__(self, rows):
            self._rows = list(rows)

        def single(self):
            return self._rows[0] if self._rows else None

        def data(self):
            return list(self._rows)

        def consume(self):
            return self

        def __iter__(self):
            return iter(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **kwargs):
        if "MATCH (n:File)" in query:
            return self._FakeResult(
                [{"name": "main.py", "path": "repo/main.py", "is_dependency": False}]
            )
        if "count(f) AS count" in query or "count(c) AS count" in query:
            return self._FakeResult([{"count": 1}])
        return self._FakeResult(
            [{"type": "Function", "name": "demo", "path": "repo/main.py", "line_number": 1, "is_dependency": False}]
        )


class _FakeDriver:
    def session(self):
        return _FakeSession()


class _FakeDBManager:
    def get_driver(self):
        return _FakeDriver()

    def close_driver(self):
        return None


class _FakeGraphBuilder:
    def delete_repository_from_graph(self, _):
        return None


class _FakeCodeFinder:
    def find_by_function_name(self, *_args, **_kwargs):
        return [{"name": "foo", "path": "repo/main.py", "line_number": 2, "is_dependency": False}]

    def find_by_class_name(self, *_args, **_kwargs):
        return [{"name": "Foo", "path": "repo/main.py", "line_number": 3, "is_dependency": False}]

    def find_by_variable_name(self, *_args, **_kwargs):
        return [{"name": "value", "path": "repo/main.py", "line_number": 4, "context": "module", "is_dependency": False}]

    def find_by_module_name(self, *_args, **_kwargs):
        return [{"name": "repo.module", "path": "repo/module.py", "line_number": 1, "is_dependency": False}]

    def find_imports(self, *_args, **_kwargs):
        return [{"alias": "json", "imported_name": "json", "path": "repo/main.py", "line_number": 1, "is_dependency": False}]

    def find_by_type(self, *_args, **_kwargs):
        return [{"name": "foo", "path": "repo/main.py", "line_number": 2, "is_dependency": False}]

    def find_by_content(self, *_args, **_kwargs):
        return [{"name": "foo", "type": "Function", "path": "repo/main.py", "line_number": 2}]

    def find_functions_by_decorator(self, *_args, **_kwargs):
        return [{"function_name": "foo", "path": "repo/main.py", "line_number": 2, "decorators": ["route"]}]

    def find_functions_by_argument(self, *_args, **_kwargs):
        return [{"function_name": "foo", "path": "repo/main.py", "line_number": 2}]

    def what_does_function_call(self, *_args, **_kwargs):
        return [{"called_function": "bar", "called_file_path": "repo/main.py", "called_line_number": 10, "called_is_dependency": False}]

    def who_calls_function(self, *_args, **_kwargs):
        return [{"caller_function": "main", "caller_file_path": "repo/main.py", "caller_line_number": 1, "caller_is_dependency": False}]

    def find_function_call_chain(self, *_args, **_kwargs):
        return [{
            "chain_length": 2,
            "function_chain": [
                {"name": "main", "path": "repo/main.py", "line_number": 1},
                {"name": "foo", "path": "repo/main.py", "line_number": 2},
            ],
            "call_details": [{"call_line": 1, "args": ["x"]}],
        }]

    def find_module_dependencies(self, *_args, **_kwargs):
        return {
            "importers": [{"importer_file_path": "repo/main.py", "import_line_number": 1}],
            "imports": [{"imported_module": "json", "import_line_number": 1}],
        }

    def find_class_hierarchy(self, *_args, **_kwargs):
        return {
            "parent_classes": [{"parent_class": "Base", "parent_file_path": "repo/base.py", "parent_line_number": 1}],
            "child_classes": [{"child_class": "Derived", "child_file_path": "repo/main.py", "child_line_number": 2}],
            "methods": [{"method_name": "run", "method_args": "self"}],
        }

    def get_cyclomatic_complexity(self, *_args, **_kwargs):
        return {"complexity": 3, "path": "repo/main.py", "line_number": 2}

    def find_most_complex_functions(self, *_args, **_kwargs):
        return [{"function_name": "complex", "complexity": 12, "path": "repo/main.py", "line_number": 20}]

    def find_dead_code(self, *_args, **_kwargs):
        return {
            "potentially_unused_functions": [{"function_name": "unused", "path": "repo/main.py", "line_number": 30}],
            "note": "Static approximation",
        }

    def find_function_overrides(self, *_args, **_kwargs):
        return [{"class_name": "Derived", "function_name": "run", "class_file_path": "repo/main.py", "function_line_number": 20}]

    def find_variable_usage_scope(self, *_args, **_kwargs):
        return {
            "instances": [{
                "scope_type": "function",
                "scope_name": "foo",
                "path": "repo/main.py",
                "line_number": 2,
                "variable_value": "1",
            }]
        }

    def list_indexed_repositories(self):
        return [{"name": "repo", "path": "repo"}]


@pytest.fixture
def kuzudb_env():
    env = {
        "DEFAULT_DATABASE": "kuzudb",
        "CGC_RUNTIME_DB_TYPE": "kuzudb",
    }
    with patch.dict(os.environ, env, clear=False):
        yield


@pytest.fixture
def cli_test_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_FILE", tmp_path / "config.json")

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "configure_mcp_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "run_neo4j_setup_wizard", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(cli_main, "index_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "add_package_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "list_repos_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "delete_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "cypher_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "cypher_helper_visual", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "visualize_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "reindex_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "clean_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "stats_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "watch_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "unwatch_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "list_watching_helper", lambda *_args, **_kwargs: None)

    fake_db = _FakeDBManager()
    monkeypatch.setattr(cli_main, "_initialize_services", lambda *_args, **_kwargs: (fake_db, _FakeGraphBuilder(), _FakeCodeFinder()))
    monkeypatch.setattr("codegraphcontext.core.get_database_manager", lambda *_args, **_kwargs: fake_db)
    monkeypatch.setattr(cli_main.DatabaseManager, "test_connection", staticmethod(lambda *_args, **_kwargs: (True, None)))
    monkeypatch.setattr(cli_main.typer, "confirm", lambda *_args, **_kwargs: True)

    class _FakeMCPServer:
        def __init__(self, *_args, **_kwargs):
            self.tools = {
                "demo": {"name": "demo.tool", "description": "demo"},
            }

        async def run(self):
            return None

        def shutdown(self):
            return None

    monkeypatch.setattr(cli_main, "MCPServer", _FakeMCPServer)

    downloaded_bundle = tmp_path / "downloaded.cgc"
    downloaded_bundle.write_text("bundle", encoding="utf-8")

    registry_module = types.ModuleType("codegraphcontext.cli.registry_commands")
    registry_module.list_bundles = lambda *_args, **_kwargs: None
    registry_module.search_bundles = lambda *_args, **_kwargs: None
    registry_module.download_bundle = lambda *_args, **_kwargs: str(downloaded_bundle)
    registry_module.request_bundle = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "codegraphcontext.cli.registry_commands", registry_module)

    bundle_module = types.ModuleType("codegraphcontext.core.cgc_bundle")

    class _FakeCGCBundle:
        def __init__(self, _db_manager):
            pass

        def export_to_bundle(self, *_args, **_kwargs):
            return True, "Bundle exported"

        def import_from_bundle(self, *_args, **_kwargs):
            return True, "Bundle imported"

    bundle_module.CGCBundle = _FakeCGCBundle
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.cgc_bundle", bundle_module)

    return {
        "bundle_file": downloaded_bundle,
        "bundle_export": tmp_path / "exported.cgc",
    }


def _matrix_command_set(entries: list[list[str]]) -> set[tuple[str, str]]:
    families = set(_inventory_from_main_source().keys()) - {"root"}
    covered: set[tuple[str, str]] = set()
    for args in entries:
        if args[0] in families:
            covered.add((args[0], args[1]))
        else:
            covered.add(("root", args[0]))
    return covered


def test_cli_inventory_grouped_from_source():
    inventory = _inventory_from_main_source()

    assert {"root", "mcp", "neo4j", "config", "bundle", "registry", "find", "analyze"}.issubset(set(inventory.keys()))
    assert inventory["mcp"] == {"setup", "start", "tools"}
    assert inventory["neo4j"] == {"setup"}
    assert inventory["config"] == {"show", "set", "reset", "db"}
    assert inventory["bundle"] == {"export", "import", "load"}
    assert inventory["registry"] == {"list", "search", "download", "request"}
    assert inventory["find"] == {"name", "pattern", "type", "variable", "content", "decorator", "argument"}
    assert inventory["analyze"] == {"calls", "callers", "chain", "deps", "tree", "complexity", "dead-code", "overrides", "variable"}
    if "context" in inventory:
        assert inventory["context"] == {"list", "create", "delete", "mode", "default"}


def test_all_canonical_cli_commands_run_with_kuzudb(kuzudb_env, cli_test_stubs):
    bundle_file = str(cli_test_stubs["bundle_file"])
    bundle_export = str(cli_test_stubs["bundle_export"])

    command_matrix = [
        ["mcp", "setup"],
        ["mcp", "start"],
        ["mcp", "tools"],
        ["neo4j", "setup"],
        ["config", "show"],
        ["config", "set", "MAX_FILE_SIZE_MB", "11"],
        ["config", "reset"],
        ["config", "db", "kuzudb"],
        ["bundle", "export", bundle_export],
        ["bundle", "import", bundle_file],
        ["bundle", "load", bundle_file],
        ["registry", "list"],
        ["registry", "search", "numpy"],
        ["registry", "download", "numpy"],
        ["registry", "request", "https://github.com/example/repo"],
        ["doctor"],
        ["start"],
        ["index", "."],
        ["clean"],
        ["stats"],
        ["delete", "."],
        ["visualize"],
        ["list"],
        ["add-package", "requests", "python"],
        ["watch", "."],
        ["unwatch", "."],
        ["watching"],
        ["find", "name", "foo"],
        ["find", "pattern", "foo"],
        ["find", "type", "function"],
        ["find", "variable", "value"],
        ["find", "content", "foo"],
        ["find", "decorator", "route"],
        ["find", "argument", "user_id"],
        ["analyze", "calls", "foo"],
        ["analyze", "callers", "foo"],
        ["analyze", "chain", "main", "foo"],
        ["analyze", "deps", "json"],
        ["analyze", "tree", "Foo"],
        ["analyze", "complexity"],
        ["analyze", "dead-code"],
        ["analyze", "overrides", "run"],
        ["analyze", "variable", "value"],
        ["query", "MATCH (n) RETURN n LIMIT 1"],
        ["cypher", "MATCH (n) RETURN n LIMIT 1"],
        ["i", "."],
        ["ls"],
        ["rm", "."],
        ["v", "."],
        ["w", "."],
        ["help"],
        ["version"],
        ["m"],
        ["n"],
        ["export", bundle_export],
        ["load", bundle_file],
    ]

    source_inventory = _inventory_from_main_source()
    if "context" in source_inventory:
        command_matrix.extend(
            [
                ["context", "list"],
                ["context", "create", "ci-context"],
                ["context", "delete", "ci-context"],
                ["context", "mode", "single"],
                ["context", "default", "ci-context"],
            ]
        )

    expected_inventory = source_inventory
    expected_set = {(family, name) for family, names in expected_inventory.items() for name in names}
    assert _matrix_command_set(command_matrix) == expected_set

    for args in command_matrix:
        result = runner.invoke(app, ["--database", "kuzudb", *args])
        assert result.exit_code == 0, f"command failed: {' '.join(args)}\n{result.output}"
        assert result.exception is None, f"exception raised for {' '.join(args)}"
        assert "Traceback" not in result.output


def test_config_db_rejects_invalid_backend_with_clear_error(kuzudb_env):
    result = runner.invoke(app, ["config", "db", "invalid-backend"])

    assert result.exit_code == 1
    assert "Invalid backend" in result.output
    assert "kuzudb" in result.output


def test_config_show_with_empty_config_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_FILE", tmp_path / "config.json")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert "Configuration Settings" in result.output


def test_find_content_falkordb_known_limitation_message(monkeypatch):
    class _FakeFalkorDBManager:
        def close_driver(self):
            return None

    class _FailingFinder:
        def find_by_content(self, _query):
            raise Exception("CALL db.index.fulltext.queryNodes is unsupported")

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeFalkorDBManager(), _FakeGraphBuilder(), _FailingFinder()),
    )

    result = runner.invoke(app, ["--database", "falkordb", "find", "content", "foo"])

    assert result.exit_code == 0
    assert "Full-text search is not supported on FalkorDB" in result.output
    assert "kkg find pattern" in result.output


class TestNeo4jDatabaseNameCLI:
    """Integration tests for NEO4J_DATABASE display in CLI commands."""

    def test_doctor_passes_database_to_test_connection(self):
        """Test that the doctor command passes NEO4J_DATABASE to test_connection."""
        with patch("codegraphcontext.cli.main.config_manager") as mock_config_mgr, patch.object(
            cli_main.DatabaseManager,
            "test_connection",
            MagicMock(return_value=(True, None)),
        ) as mock_test_conn:
            mock_config_mgr.load_config.return_value = {"DEFAULT_DATABASE": "neo4j"}
            mock_config_mgr.CONFIG_FILE = MagicMock()
            mock_config_mgr.CONFIG_FILE.exists.return_value = True
            mock_config_mgr.CONTEXT_CONFIG_FILE = MagicMock()
            mock_config_mgr.CONTEXT_CONFIG_FILE.exists.return_value = False
            mock_config_mgr.CONFIG_DIR = MagicMock()
            mock_config_mgr.CONFIG_DIR.exists.return_value = True
            mock_config_mgr.validate_config_value.return_value = (True, None)

            env = {
                "NEO4J_URI": "bolt://localhost:7687",
                "NEO4J_USERNAME": "neo4j",
                "NEO4J_PASSWORD": "password",
                "NEO4J_DATABASE": "mydb",
                "CGC_RUNTIME_DB_TYPE": "neo4j",
                "DEFAULT_DATABASE": "neo4j",
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("codegraphcontext.cli.main._load_credentials"):
                    cli_main.doctor()

            mock_test_conn.assert_called_once_with(
                "bolt://localhost:7687", "neo4j", "password", database="mydb"
            )

    @patch("codegraphcontext.cli.main.config_manager")
    def test_load_credentials_displays_database_name(self, mock_config_mgr, monkeypatch, tmp_path):
        """Test _load_credentials prints database name when NEO4J_DATABASE is set."""
        mock_config_mgr.ensure_config_dir.return_value = None

        env = {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "password",
            "NEO4J_DATABASE": "mydb",
            "DEFAULT_DATABASE": "neo4j",
        }
        monkeypatch.chdir(tmp_path)
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "DEFAULT_DATABASE",
                "CGC_RUNTIME_DB_TYPE",
                "NEO4J_URI",
                "NEO4J_USERNAME",
                "NEO4J_PASSWORD",
                "NEO4J_DATABASE",
            }
        }
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            output = StringIO()
            with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
                _load_credentials()

            printed = output.getvalue()
            assert "Using database: Neo4j (database: mydb)" in printed

    @patch("codegraphcontext.cli.main.config_manager")
    def test_load_credentials_no_database_name(self, mock_config_mgr, monkeypatch, tmp_path):
        """Test _load_credentials prints Neo4j without database when NEO4J_DATABASE is not set."""
        mock_config_mgr.ensure_config_dir.return_value = None

        env = {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "password",
            "DEFAULT_DATABASE": "neo4j",
        }
        monkeypatch.chdir(tmp_path)
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "DEFAULT_DATABASE",
                "CGC_RUNTIME_DB_TYPE",
                "NEO4J_URI",
                "NEO4J_USERNAME",
                "NEO4J_PASSWORD",
                "NEO4J_DATABASE",
            }
        }
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            output = StringIO()
            with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
                _load_credentials()

            printed = output.getvalue()
            assert "Using database: Neo4j" in printed
            assert "(database:" not in printed


def test_load_credentials_displays_kuzudb_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main.config_manager, "ensure_config_dir", lambda *_args, **_kwargs: None)

    monkeypatch.chdir(tmp_path)
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "DEFAULT_DATABASE",
            "CGC_RUNTIME_DB_TYPE",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "NEO4J_DATABASE",
        }
    }
    clean_env["DEFAULT_DATABASE"] = "kuzudb"
    with patch.dict(os.environ, clean_env, clear=True):
        output = StringIO()
        with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
            _load_credentials()

        assert "Using database: KùzuDB" in output.getvalue()


def test_load_credentials_defaults_to_falkordb_when_unconfigured(monkeypatch, tmp_path):
    class _FakeManager:
        def get_backend_type(self):
            return "falkordb"

    monkeypatch.setattr(cli_main.config_manager, "ensure_config_dir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)

    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "DEFAULT_DATABASE",
            "CGC_RUNTIME_DB_TYPE",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "NEO4J_DATABASE",
        }
    }
    with patch.dict(os.environ, clean_env, clear=True):
        output = StringIO()
        with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)), patch(
            "codegraphcontext.core.get_database_manager",
            return_value=_FakeManager(),
        ):
            _load_credentials()

        assert "Using database: FalkorDB Lite" in output.getvalue()


def test_context_create_defaults_to_falkordb(monkeypatch):
    seen: dict[str, str | None] = {}

    monkeypatch.setattr(
        cli_main.config_manager,
        "create_context",
        lambda name, database, db_path: seen.update(
            name=name,
            database=database,
            db_path=db_path,
        ),
    )
    monkeypatch.setattr(cli_main.config_manager, "get_config_value", lambda key: None)

    result = runner.invoke(app, ["context", "create", "demo"])

    assert result.exit_code == 0
    assert seen["name"] == "demo"
    assert seen["database"] == "falkordb"
    assert seen["db_path"] is None


def test_known_macos_malloc_warning_filter_suppresses_only_the_noisy_line(tmp_path):
    stderr_path = tmp_path / "stderr.log"
    warning = (
        b"Python(56432) MallocStackLogging: can't turn off malloc stack logging "
        b"because it was not enabled.\n"
    )
    normal = b"real stderr line\n"
    stderr_fd = sys.stderr.fileno()
    saved_stderr = os.dup(stderr_fd)

    try:
        with open(stderr_path, "wb", buffering=0) as sink:
            os.dup2(sink.fileno(), stderr_fd)
            with cli_main._suppress_known_macos_stderr():
                os.write(stderr_fd, warning)
                os.write(stderr_fd, normal)
    finally:
        os.dup2(saved_stderr, stderr_fd)
        os.close(saved_stderr)

    output = stderr_path.read_bytes()
    assert warning not in output
    assert normal in output


def test_root_callback_installs_malloc_stderr_filter_on_macos(monkeypatch):
    installed: list[object] = []

    class _FakeContext:
        invoked_subcommand = "embed"

        def ensure_object(self, _type):
            return {}

        def with_resource(self, resource):
            installed.append(resource)
            return resource

    monkeypatch.setattr(cli_main.sys, "platform", "darwin")

    cli_main.main(_FakeContext(), database=None, visual=False, version_=None, help_=None)

    assert len(installed) == 1


def test_wrap_non_force_embed_fetch_resets_offset_for_resumes():
    calls: list[dict[str, int | bool]] = []

    def _fake_fetch(_conn, _table, *, force, batch_size, offset):
        calls.append({
            "force": force,
            "batch_size": batch_size,
            "offset": offset,
        })
        return []

    wrapped = cli_main._wrap_non_force_embed_fetch(_fake_fetch)

    wrapped(None, "Function", force=False, batch_size=64, offset=128)
    wrapped(None, "Function", force=True, batch_size=64, offset=128)

    assert calls == [
        {"force": False, "batch_size": 64, "offset": 0},
        {"force": True, "batch_size": 64, "offset": 128},
    ]


def test_embed_runtime_defaults_to_cpu_and_smaller_batches_on_macos(monkeypatch):
    monkeypatch.setattr(cli_main.sys, "platform", "darwin")
    monkeypatch.delenv("CGRAPH_EMBED_DEVICE", raising=False)
    monkeypatch.delenv("CGRAPH_EMBED_BATCH_SIZE", raising=False)

    assert cli_main._embed_device_override() == "cpu"
    assert cli_main._embed_batch_size_override() == 8
    assert cli_main._sentence_transformer_model_kwargs() == {
        "trust_remote_code": True,
        "device": "cpu",
    }
    assert cli_main._embed_encode_kwargs() == {
        "show_progress_bar": False,
        "batch_size": 8,
    }


def test_embed_runtime_respects_explicit_env_overrides(monkeypatch):
    monkeypatch.setattr(cli_main.sys, "platform", "darwin")
    monkeypatch.setenv("CGRAPH_EMBED_DEVICE", "mps")
    monkeypatch.setenv("CGRAPH_EMBED_BATCH_SIZE", "4")

    assert cli_main._embed_device_override() == "mps"
    assert cli_main._embed_batch_size_override() == 4


def test_root_callback_installs_embed_runtime_guards_for_embed(monkeypatch):
    installed: list[str] = []

    class _FakeContext:
        invoked_subcommand = "embed"

        def ensure_object(self, _type):
            return {}

        def with_resource(self, resource):
            return resource

    monkeypatch.setattr(cli_main, "_apply_embed_runtime_guards", lambda: installed.append("embed"))

    cli_main.main(_FakeContext(), database=None, visual=False, version_=None, help_=None)

    assert installed == ["embed"]
