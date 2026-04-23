"""Code-only filter: restrict indexing to AST-parseable code + structural configs.

Used by ``kkg index --code-only`` to walk a tree keeping only files that inform
code structure and quality. Everything else (prose, media, data, generated
artifacts) is dropped before the graph pipeline sees it.
"""

from __future__ import annotations

from pathlib import Path
from typing import AbstractSet, Iterable, List, Tuple


# Exact-basename matches: manifests, build files, and lint/format configs
# whose names (not extensions) define project structure or quality rules.
STRUCTURAL_NAMES: frozenset[str] = frozenset({
    # Python
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements-dev.txt",
    "Pipfile", "Pipfile.lock",
    # JS / TS
    "package.json", "tsconfig.json",
    # Rust
    "Cargo.toml",
    # Go
    "go.mod", "go.sum",
    # JVM
    "pom.xml",
    "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts",
    # Ruby
    "Gemfile", "Gemfile.lock",
    # PHP
    "composer.json",
    # Build / container
    "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
    "Makefile", "GNUmakefile",
    "CMakeLists.txt",
    "Taskfile.yml", "Taskfile.yaml",
    "justfile", "Justfile",
    # Lint / format / type-check configs
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs",
    ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml",
    ".prettierrc", ".prettierrc.js", ".prettierrc.json",
    ".prettierrc.yml", ".prettierrc.yaml",
    "ruff.toml", ".ruff.toml",
    "mypy.ini", ".mypy.ini",
    "pyrightconfig.json",
    ".flake8",
    ".rubocop.yml",
    ".golangci.yml", ".golangci.yaml",
})

STRUCTURAL_FILENAME_PREFIXES: tuple[str, ...] = (
    "tsconfig.",
    "requirements-",
    "Dockerfile.",
    "docker-compose.",
)

STRUCTURAL_EXTENSIONS: frozenset[str] = frozenset({
    ".proto", ".graphql", ".gql", ".prisma", ".sql",
})


def is_code_or_structural(path: Path, parseable_extensions: AbstractSet[str]) -> bool:
    """Return True if *path* should be kept under ``--code-only``."""
    if path.suffix in parseable_extensions or path.suffix in STRUCTURAL_EXTENSIONS:
        return True
    name = path.name
    return name in STRUCTURAL_NAMES or name.startswith(STRUCTURAL_FILENAME_PREFIXES)


def partition_by_code_only(
    files: Iterable[Path],
    parseable_extensions: AbstractSet[str],
) -> Tuple[List[Path], List[Path]]:
    """Split *files* into (kept, skipped) based on the whitelist."""
    kept: List[Path] = []
    skipped: List[Path] = []
    for f in files:
        if is_code_or_structural(f, parseable_extensions):
            kept.append(f)
        else:
            skipped.append(f)
    return kept, skipped
