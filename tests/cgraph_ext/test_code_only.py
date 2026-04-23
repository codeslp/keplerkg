"""Integration tests for `kkg index --code-only` discovery + CLI flag."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codegraphcontext.cli.main import app
from codegraphcontext.tools.graph_builder import PARSER_EXTENSIONS
from codegraphcontext.tools.indexing.discovery import discover_files_to_index


def _build_sample_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "src" / "util.ts").write_text("export const x = 1;\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "Dockerfile").write_text("FROM python:3.12\n")
    (root / "schema.sql").write_text("CREATE TABLE t(id INT);\n")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# guide\n")
    (root / "assets").mkdir()
    (root / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "fixtures").mkdir()
    (root / "fixtures" / "seed.json").write_text("{}")


def test_discover_code_only_keeps_code_and_structural(tmp_path: Path):
    repo = tmp_path / "repo"
    _build_sample_tree(repo)

    files, _ = discover_files_to_index(
        repo,
        cgcignore_path=None,
        code_only=True,
        parseable_extensions=set(PARSER_EXTENSIONS.keys()),
    )

    names = {f.name for f in files}
    assert "app.py" in names
    assert "util.ts" in names
    assert "pyproject.toml" in names
    assert "Dockerfile" in names
    assert "schema.sql" in names
    # Dropped:
    assert "guide.md" not in names
    assert "logo.png" not in names
    assert "seed.json" not in names


def test_discover_code_only_requires_parseable_extensions(tmp_path: Path):
    repo = tmp_path / "repo"
    _build_sample_tree(repo)

    with pytest.raises(ValueError):
        discover_files_to_index(repo, cgcignore_path=None, code_only=True)


def test_discover_without_code_only_keeps_prose_and_json(tmp_path: Path):
    repo = tmp_path / "repo"
    _build_sample_tree(repo)

    files, _ = discover_files_to_index(repo, cgcignore_path=None, code_only=False)
    names = {f.name for f in files}
    # Prose and JSON survive the default .cgcignore (default drops images,
    # archives, and a handful of known non-code types). --code-only is the
    # only path that drops these.
    assert {"guide.md", "seed.json"}.issubset(names)
    # Without --code-only, these *are* discovered.
    assert "guide.md" in names


def test_index_cli_aborts_on_n_response(tmp_path: Path, monkeypatch):
    """--code-only with 'n' at the prompt aborts before touching the DB."""
    repo = tmp_path / "repo"
    _build_sample_tree(repo)

    # Guard: if the helper is ever reached, fail the test. A 'n' answer must
    # short-circuit before index_helper runs.
    def _should_not_run(*args, **kwargs):
        raise AssertionError("index_helper should not run when user declines")

    monkeypatch.setattr("codegraphcontext.cli.main.index_helper", _should_not_run)
    monkeypatch.setattr("codegraphcontext.cli.main.reindex_helper", _should_not_run)
    # Avoid credential side-effects.
    monkeypatch.setattr("codegraphcontext.cli.main._load_credentials", lambda: None)

    runner = CliRunner()
    result = runner.invoke(app, ["index", str(repo), "--code-only"], input="n\n")

    # Exit code 1 is produced by typer.Exit(code=1) when the user declines.
    # The guard above ensures the indexer was never invoked.
    assert result.exit_code == 1


def test_index_cli_proceeds_with_yes_flag(tmp_path: Path, monkeypatch):
    """--code-only --yes skips the prompt and calls the indexer."""
    repo = tmp_path / "repo"
    _build_sample_tree(repo)

    calls = {"code_only": None}

    def _fake_index(path, context=None, code_only=False):
        calls["code_only"] = code_only

    monkeypatch.setattr("codegraphcontext.cli.main.index_helper", _fake_index)
    monkeypatch.setattr("codegraphcontext.cli.main._load_credentials", lambda: None)

    runner = CliRunner()
    result = runner.invoke(app, ["index", str(repo), "--code-only", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert calls["code_only"] is True
