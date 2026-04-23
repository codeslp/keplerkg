"""Project targeting helpers for per-repository local database stores.

Spec 004 routes each target codebase to its own local store under
``/Volumes/zombie/cgraph/db/<slug>/`` so ANN search, embeddings, and
re-indexes stay repo-scoped across embedded backends.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer

_DEFAULT_DB_ROOT = Path("/Volumes/zombie/cgraph/db")
_DB_ROOT_ENV = "CGRAPH_DB_ROOT"
_PROJECT_ENV = "CGRAPH_PROJECT"
_REJECTED_SLUGS = {"", "default", "global"}
_DEFAULT_LOCAL_BACKEND = "falkordb"
_LOCAL_BACKEND_PATH_ENVS = {
    "kuzudb": "KUZUDB_PATH",
    "falkordb": "FALKORDB_PATH",
}

PROJECT_OPTION_HELP = (
    "Target project slug. Routes local DB storage to "
    "/Volumes/zombie/cgraph/db/<slug>/<backend>. Use one project per CLI invocation."
)


@dataclass(frozen=True)
class ProjectTarget:
    """Resolved target project identity and active local backend path."""

    slug: str
    db_path: Path
    source: str
    database: str = _DEFAULT_LOCAL_BACKEND
    path_env: str = "FALKORDB_PATH"
    socket_path: Optional[Path] = None


def activate_project(
    project: Optional[str] = None,
    *,
    start_dir: Optional[Path] = None,
) -> ProjectTarget:
    """Resolve the active target and export project-scoped local DB paths."""
    try:
        target = resolve_project_target(project, start_dir=start_dir)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--project") from exc

    paths = _project_paths_for_slug(target.slug)
    os.environ["KUZUDB_PATH"] = str(paths["kuzudb"])
    os.environ["FALKORDB_PATH"] = str(paths["falkordb"])
    os.environ["FALKORDB_SOCKET_PATH"] = str(paths["falkordb_socket"])

    _reset_kuzu_manager_if_needed(paths["kuzudb"])
    _reset_falkordb_manager_if_needed(
        paths["falkordb"],
        paths["falkordb_socket"],
    )
    return target


def resolve_project_target(
    project: Optional[str] = None,
    *,
    start_dir: Optional[Path] = None,
) -> ProjectTarget:
    """Resolve the current target project from CLI/env/config/fallback."""
    base_dir = _normalize_start_dir(start_dir)

    if project:
        slug = _normalize_slug(project)
        return _build_project_target(slug, source="cli")

    env_project = os.environ.get(_PROJECT_ENV)
    if env_project:
        slug = _normalize_slug(env_project)
        return _build_project_target(slug, source="env")

    project_toml = find_project_toml(base_dir)
    if project_toml is not None:
        toml_project = _project_from_toml(project_toml)
        if toml_project:
            slug = _normalize_slug(toml_project)
            return _build_project_target(slug, source="toml")

    slug = _normalize_slug(base_dir.name)
    if _should_warn_on_fallback():
        print(
            f"Warning: inferred project slug '{slug}' from {base_dir}. "
            f"Prefer --project, $CGRAPH_PROJECT, or .cgraph/project.toml.",
            file=sys.stderr,
        )
    return _build_project_target(slug, source="basename")


def find_project_toml(start_dir: Optional[Path] = None) -> Optional[Path]:
    """Walk upward looking for ``.cgraph/project.toml``."""
    base_dir = _normalize_start_dir(start_dir)
    for candidate in (base_dir, *base_dir.parents):
        toml_path = candidate / ".cgraph" / "project.toml"
        if toml_path.is_file():
            return toml_path
    return None


def _normalize_start_dir(start_dir: Optional[Path]) -> Path:
    path = (start_dir or Path.cwd()).expanduser().resolve()
    if path.is_file():
        return path.parent
    return path


def _project_from_toml(project_toml: Path) -> Optional[str]:
    current_section: tuple[str, ...] = ()
    for raw_line in project_toml.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = tuple(part.strip() for part in line[1:-1].split("."))
            continue
        if current_section or not line.startswith("project"):
            continue

        key, _, value = line.partition("=")
        if key.strip() != "project":
            continue
        parsed = _parse_toml_string(value.strip())
        return parsed or None
    return None


def _parse_toml_string(value: str) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        return str(ast.literal_eval(value))
    return value


def _normalize_slug(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if slug in _REJECTED_SLUGS:
        raise ValueError(
            f"Invalid project slug {raw!r}. Choose a non-empty kebab-case slug that is not "
            "'default' or 'global'."
        )
    if not slug:
        raise ValueError(f"Invalid project slug {raw!r}.")
    return slug


def _db_root() -> Path:
    raw_root = os.environ.get(_DB_ROOT_ENV)
    if raw_root:
        return Path(raw_root).expanduser()
    return _DEFAULT_DB_ROOT


def _should_warn_on_fallback() -> bool:
    if os.environ.get("CGRAPH_WARN_ON_FALLBACK") == "1":
        return True
    stderr = getattr(sys, "stderr", None)
    return bool(stderr is not None and stderr.isatty())


def _selected_local_backend() -> str:
    raw_backend = (
        os.environ.get("CGC_RUNTIME_DB_TYPE")
        or os.environ.get("DEFAULT_DATABASE")
        or _DEFAULT_LOCAL_BACKEND
    ).lower()
    return raw_backend if raw_backend in _LOCAL_BACKEND_PATH_ENVS else _DEFAULT_LOCAL_BACKEND


def _build_project_target(slug: str, *, source: str) -> ProjectTarget:
    backend = _selected_local_backend()
    paths = _project_paths_for_slug(slug)
    if backend == "kuzudb":
        return ProjectTarget(
            slug=slug,
            db_path=paths["kuzudb"],
            source=source,
            database=backend,
            path_env=_LOCAL_BACKEND_PATH_ENVS[backend],
        )

    return ProjectTarget(
        slug=slug,
        db_path=paths["falkordb"],
        source=source,
        database=backend,
        path_env=_LOCAL_BACKEND_PATH_ENVS[backend],
        socket_path=paths["falkordb_socket"],
    )


def _project_paths_for_slug(slug: str) -> dict[str, Path]:
    root = _db_root()
    legacy_cgraph = root / "kuzudb"
    target_dir = root / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    kuzu_path = target_dir / "kuzudb"
    if slug == "cgraph" and legacy_cgraph.exists() and not kuzu_path.exists():
        root.mkdir(parents=True, exist_ok=True)
        kuzu_path = legacy_cgraph

    return {
        "kuzudb": kuzu_path,
        "falkordb": target_dir / "falkordb",
        "falkordb_socket": target_dir / "falkordb.sock",
    }


def reset_local_db_manager(database: Optional[str], db_path: str | Path) -> None:
    """Reset embedded backend singletons when callers retarget the active DB."""
    backend = (database or "").lower()
    target_path = Path(db_path)

    if backend == "kuzudb":
        _reset_kuzu_manager_if_needed(target_path)
    elif backend == "falkordb":
        _reset_falkordb_manager_if_needed(
            target_path,
            target_path.parent / "falkordb.sock",
        )


def _reset_kuzu_manager_if_needed(expected_db_path: Path) -> None:
    """Drop the cached upstream singleton if it points at another DB.

    The CLI is still documented as one-project-per-process, but tests and
    programmatic callers can otherwise keep a stale singleton alive after
    changing ``KUZUDB_PATH``.
    """
    try:
        from codegraphcontext.core.database_kuzu import KuzuDBManager
    except Exception:
        return

    instance = getattr(KuzuDBManager, "_instance", None)
    if instance is None:
        return

    current_path = getattr(instance, "db_path", None)
    if current_path == str(expected_db_path):
        return

    close_driver = getattr(instance, "close_driver", None)
    if callable(close_driver):
        close_driver()

    KuzuDBManager._instance = None
    KuzuDBManager._db = None
    KuzuDBManager._conn = None


def _reset_falkordb_manager_if_needed(
    expected_db_path: Path,
    expected_socket_path: Path,
) -> None:
    """Drop the cached FalkorDB singleton if it points at another project DB."""
    try:
        from codegraphcontext.core.database_falkordb import FalkorDBManager
    except Exception:
        return

    instance = getattr(FalkorDBManager, "_instance", None)
    if instance is None:
        return

    current_db_path = getattr(instance, "db_path", None)
    current_socket_path = getattr(instance, "socket_path", None)
    if (
        current_db_path == str(expected_db_path)
        and current_socket_path == str(expected_socket_path)
    ):
        return

    close_driver = getattr(instance, "close_driver", None)
    if callable(close_driver):
        close_driver()

    shutdown = getattr(instance, "shutdown", None)
    if callable(shutdown):
        shutdown()

    FalkorDBManager._instance = None
    FalkorDBManager._process = None
    FalkorDBManager._driver = None
    FalkorDBManager._graph = None
