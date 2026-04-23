"""Code-only filter: restrict indexing to AST-parseable code + structural configs.

Used by ``kkg index --code-only`` to walk a tree keeping only files that inform
code structure and quality. Everything else (prose, media, data, generated
artifacts) is dropped before the graph pipeline sees it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Set, Tuple


# Filenames whose presence defines how code is built/deployed. Kept even when
# the extension is not in the parseable set, since they encode structure.
STRUCTURAL_FILENAMES: frozenset[str] = frozenset({
    # Python
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "Pipfile.lock",
    # JS/TS
    "package.json",
    "tsconfig.json",
    # Rust
    "Cargo.toml",
    # Go
    "go.mod",
    "go.sum",
    # JVM
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    # Ruby
    "Gemfile",
    "Gemfile.lock",
    # PHP
    "composer.json",
    # Build / container
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Makefile",
    "GNUmakefile",
    "CMakeLists.txt",
    "Taskfile.yml",
    "Taskfile.yaml",
    "justfile",
    "Justfile",
})

# Filename prefixes to keep (e.g. tsconfig.base.json, requirements-test.txt).
STRUCTURAL_FILENAME_PREFIXES: tuple[str, ...] = (
    "tsconfig.",
    "requirements-",
    "Dockerfile.",
    "docker-compose.",
)

# Extensions that define schema/IDL or code-quality rules.
STRUCTURAL_EXTENSIONS: frozenset[str] = frozenset({
    ".proto",
    ".graphql",
    ".gql",
    ".prisma",
    ".sql",
})

# Lint/format configs that express code-quality rules. Matched by basename
# since most are dotfiles with no extension.
STRUCTURAL_DOTFILES: frozenset[str] = frozenset({
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "pyrightconfig.json",
    ".flake8",
    ".rubocop.yml",
    ".golangci.yml",
    ".golangci.yaml",
})


def is_code_or_structural(path: Path, parseable_extensions: Set[str]) -> bool:
    """Return True if *path* should be kept under ``--code-only``."""
    suffix = path.suffix
    if suffix in parseable_extensions:
        return True
    if suffix in STRUCTURAL_EXTENSIONS:
        return True

    name = path.name
    if name in STRUCTURAL_FILENAMES:
        return True
    if name in STRUCTURAL_DOTFILES:
        return True
    for prefix in STRUCTURAL_FILENAME_PREFIXES:
        if name.startswith(prefix):
            return True

    return False


def partition_by_code_only(
    files: Iterable[Path],
    parseable_extensions: Set[str],
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
