"""Tests for the --code-only whitelist filter."""

from __future__ import annotations

from pathlib import Path

from codegraphcontext.core.code_only_filter import (
    is_code_or_structural,
    partition_by_code_only,
)


PARSEABLE = {".py", ".ts", ".tsx", ".js", ".go", ".rs"}


def test_parseable_code_is_kept():
    assert is_code_or_structural(Path("src/app.py"), PARSEABLE)
    assert is_code_or_structural(Path("src/frontend/App.tsx"), PARSEABLE)
    assert is_code_or_structural(Path("main.go"), PARSEABLE)


def test_structural_manifests_are_kept():
    assert is_code_or_structural(Path("pyproject.toml"), PARSEABLE)
    assert is_code_or_structural(Path("package.json"), PARSEABLE)
    assert is_code_or_structural(Path("tsconfig.json"), PARSEABLE)
    assert is_code_or_structural(Path("Cargo.toml"), PARSEABLE)
    assert is_code_or_structural(Path("go.mod"), PARSEABLE)


def test_dockerfile_and_build_scripts_are_kept():
    assert is_code_or_structural(Path("Dockerfile"), PARSEABLE)
    assert is_code_or_structural(Path("docker-compose.yml"), PARSEABLE)
    assert is_code_or_structural(Path("Makefile"), PARSEABLE)
    assert is_code_or_structural(Path("CMakeLists.txt"), PARSEABLE)
    assert is_code_or_structural(Path("Justfile"), PARSEABLE)


def test_schema_and_lint_configs_are_kept():
    assert is_code_or_structural(Path("api.proto"), PARSEABLE)
    assert is_code_or_structural(Path("schema.graphql"), PARSEABLE)
    assert is_code_or_structural(Path("migrations/001_init.sql"), PARSEABLE)
    assert is_code_or_structural(Path(".eslintrc.json"), PARSEABLE)
    assert is_code_or_structural(Path("ruff.toml"), PARSEABLE)
    assert is_code_or_structural(Path("mypy.ini"), PARSEABLE)


def test_prose_and_media_are_dropped():
    assert not is_code_or_structural(Path("README.md"), PARSEABLE)
    assert not is_code_or_structural(Path("docs/guide.md"), PARSEABLE)
    assert not is_code_or_structural(Path("research/notes.md"), PARSEABLE)
    assert not is_code_or_structural(Path("assets/logo.png"), PARSEABLE)
    assert not is_code_or_structural(Path("tutorial.pdf"), PARSEABLE)
    assert not is_code_or_structural(Path("demo.mp4"), PARSEABLE)


def test_data_and_binaries_are_dropped():
    assert not is_code_or_structural(Path("fixtures/users.json"), PARSEABLE)
    assert not is_code_or_structural(Path("data/records.csv"), PARSEABLE)
    assert not is_code_or_structural(Path("data/export.parquet"), PARSEABLE)
    assert not is_code_or_structural(Path("build/app.wasm"), PARSEABLE)


def test_lock_files_are_dropped():
    # Lockfiles describe deps but aren't code-quality signal; the manifests
    # (package.json, pyproject.toml) already capture structure.
    assert not is_code_or_structural(Path("package-lock.json"), PARSEABLE)
    assert not is_code_or_structural(Path("pnpm-lock.yaml"), PARSEABLE)
    assert not is_code_or_structural(Path("yarn.lock"), PARSEABLE)


def test_prefixed_manifests_are_kept():
    assert is_code_or_structural(Path("tsconfig.base.json"), PARSEABLE)
    assert is_code_or_structural(Path("tsconfig.build.json"), PARSEABLE)
    assert is_code_or_structural(Path("requirements-dev.txt"), PARSEABLE)
    assert is_code_or_structural(Path("requirements-test.txt"), PARSEABLE)
    assert is_code_or_structural(Path("Dockerfile.prod"), PARSEABLE)


def test_partition_splits_files():
    files = [
        Path("src/app.py"),
        Path("pyproject.toml"),
        Path("README.md"),
        Path("assets/logo.png"),
        Path("tests/test_app.py"),
    ]
    kept, skipped = partition_by_code_only(files, PARSEABLE)

    kept_names = {p.name for p in kept}
    skipped_names = {p.name for p in skipped}

    assert kept_names == {"app.py", "pyproject.toml", "test_app.py"}
    assert skipped_names == {"README.md", "logo.png"}


def test_partition_on_empty_input():
    kept, skipped = partition_by_code_only([], PARSEABLE)
    assert kept == []
    assert skipped == []
