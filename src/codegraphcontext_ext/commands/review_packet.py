"""Phase 2 review-packet command: reviewer JSON with fallback chain.

Spec §4.2-4.3: kkg review-packet walks a fallback chain
(diff→staged→workdir→untracked→locked_files) to produce a JSON packet
with touched nodes, callers/callees not in the diff, cross-module
impact, and advisories.  50-node cap per bucket (§4.4).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from pathspec import PathSpec

from codegraphcontext.cli.config_manager import DEFAULT_CGCIGNORE_PATTERNS
from codegraphcontext.core.cgcignore import find_cgcignore, parse_cgcignore_lines
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection

COMMAND_NAME = "review-packet"
SCHEMA_FILE = "review-packet.json"
SUMMARY = "Generate a reviewer JSON packet with blast radius and advisories."

# Node tables to query for touched-file lookups.
# Excludes Repository, Directory, Module, Parameter (no meaningful path match).
_CODE_NODE_TABLES = (
    "Function", "Class", "Variable", "Trait", "Interface",
    "Macro", "Struct", "Enum", "Union", "Annotation", "Record", "Property",
)

_DEFAULT_MAX_NODES = 50


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run_git(*args: str, cwd: Path | None = None) -> str:
    """Run a git command and return stripped stdout."""
    cmd = ["git"]
    if cwd:
        cmd.extend(["-C", str(cwd)])
    cmd.extend(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return result.stdout.strip()


def _ref_resolves(ref: str, cwd: Path | None = None) -> bool:
    """Check whether a git ref resolves."""
    try:
        _run_git("rev-parse", "--verify", ref, cwd=cwd)
        return True
    except subprocess.CalledProcessError:
        return False


def _diff_files(base: str, head: str, cwd: Path | None = None) -> list[str]:
    """Return list of files changed between base..head."""
    try:
        output = _run_git("diff", "--name-only", f"{base}..{head}", cwd=cwd)
    except subprocess.CalledProcessError:
        return []
    return [f for f in output.splitlines() if f]


def _diff_stats(base: str, head: str, cwd: Path | None = None) -> dict[str, int]:
    """Return {files, additions, deletions} for base..head."""
    try:
        output = _run_git("diff", "--shortstat", f"{base}..{head}", cwd=cwd)
    except subprocess.CalledProcessError:
        return {"files": 0, "additions": 0, "deletions": 0}
    return _parse_shortstat(output)


def _staged_files(cwd: Path | None = None) -> list[str]:
    try:
        output = _run_git("diff", "--cached", "--name-only", cwd=cwd)
    except subprocess.CalledProcessError:
        return []
    return [f for f in output.splitlines() if f]


def _staged_stats(cwd: Path | None = None) -> dict[str, int]:
    try:
        output = _run_git("diff", "--cached", "--shortstat", cwd=cwd)
    except subprocess.CalledProcessError:
        return {"files": 0, "additions": 0, "deletions": 0}
    return _parse_shortstat(output)


def _workdir_files(cwd: Path | None = None) -> list[str]:
    try:
        output = _run_git("diff", "--name-only", cwd=cwd)
    except subprocess.CalledProcessError:
        return []
    return [f for f in output.splitlines() if f]


def _workdir_stats(cwd: Path | None = None) -> dict[str, int]:
    try:
        output = _run_git("diff", "--shortstat", cwd=cwd)
    except subprocess.CalledProcessError:
        return {"files": 0, "additions": 0, "deletions": 0}
    return _parse_shortstat(output)


def _untracked_files(
    filter_paths: list[str] | None = None, cwd: Path | None = None,
) -> list[str]:
    try:
        output = _run_git(
            "ls-files", "--others", "--exclude-standard", cwd=cwd,
        )
    except subprocess.CalledProcessError:
        return []
    files = [f for f in output.splitlines() if f]
    if filter_paths:
        path_set = set(filter_paths)
        # Also match by prefix for directory paths
        files = [
            f for f in files
            if f in path_set or any(f.startswith(p.rstrip("/") + "/") for p in filter_paths)
        ]
    return files


def _is_ancestor(base: str, head: str, cwd: Path | None = None) -> bool:
    """Check if base is an ancestor of head."""
    try:
        subprocess.run(
            ["git"] + (["-C", str(cwd)] if cwd else [])
            + ["merge-base", "--is-ancestor", base, head],
            capture_output=True, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _parse_shortstat(output: str) -> dict[str, int]:
    """Parse git diff --shortstat output into {files, additions, deletions}."""
    stats: dict[str, int] = {"files": 0, "additions": 0, "deletions": 0}
    if not output.strip():
        return stats
    for part in output.strip().split(","):
        part = part.strip()
        if "file" in part:
            stats["files"] = int(part.split()[0])
        elif "insertion" in part:
            stats["additions"] = int(part.split()[0])
        elif "deletion" in part:
            stats["deletions"] = int(part.split()[0])
    return stats


def _git_probe(*args: str, cwd: Path | None = None) -> str | None:
    """Run a git probe and return stdout, or None when git rejects it."""
    try:
        return _run_git(*args, cwd=cwd)
    except subprocess.CalledProcessError:
        return None


def _probe_repo_shape(cwd: Path | None = None) -> tuple[list[dict[str, str]], str | None]:
    """Detect repo shapes that need an advisory or forced fallback."""
    advisories: list[dict[str, str]] = []
    inside_work_tree = _git_probe("rev-parse", "--is-inside-work-tree", cwd=cwd)
    bare_repo = _git_probe("rev-parse", "--is-bare-repository", cwd=cwd)
    superproject_root = _git_probe("rev-parse", "--show-superproject-working-tree", cwd=cwd)

    if bare_repo == "true":
        advisories.append({
            "level": "warn",
            "kind": "unsupported_repo_shape",
            "detail": "bare_repo",
        })
        return advisories, "locked_files"

    if inside_work_tree == "false":
        advisories.append({
            "level": "warn",
            "kind": "unsupported_repo_shape",
            "detail": "outside_work_tree",
        })
        return advisories, "locked_files"

    if superproject_root:
        advisories.append({
            "level": "warn",
            "kind": "unsupported_repo_shape",
            "detail": f"submodule_boundary:{superproject_root}",
        })

    return advisories, None


def _repo_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def _resolve_packet_path(path: str, cwd: Path | None = None) -> Path:
    path_obj = Path(path)
    if path_obj.is_absolute():
        return path_obj.resolve()
    return (_repo_root(cwd) / path_obj).resolve()


def _display_path(path: str | None, cwd: Path | None = None) -> str | None:
    if not path:
        return path
    path_obj = Path(path)
    if not path_obj.is_absolute():
        return path_obj.as_posix()
    try:
        return path_obj.resolve().relative_to(_repo_root(cwd)).as_posix()
    except ValueError:
        return path_obj.as_posix()


def _path_variants(path: str, cwd: Path | None = None) -> set[str]:
    variants = {Path(path).as_posix()}
    variants.add(str(_resolve_packet_path(path, cwd)))
    return {variant for variant in variants if variant}


def _path_variant_map(file_paths: list[str], cwd: Path | None = None) -> dict[str, set[str]]:
    return {path: _path_variants(path, cwd) for path in file_paths}


def _query_path_list(file_paths: list[str], cwd: Path | None = None) -> list[str]:
    query_paths: set[str] = set()
    for path in file_paths:
        query_paths.update(_path_variants(path, cwd))
    return sorted(query_paths)


def _matchable_ignore_path(path: str, cwd: Path | None = None) -> str | None:
    path_obj = Path(path)
    if not path_obj.is_absolute():
        return path_obj.as_posix()
    try:
        return path_obj.resolve().relative_to(_repo_root(cwd)).as_posix()
    except ValueError:
        return None


def _load_cgcignore_spec(cwd: Path | None = None) -> PathSpec:
    ignore_root = _repo_root(cwd)
    default_patterns = parse_cgcignore_lines(DEFAULT_CGCIGNORE_PATTERNS.splitlines())
    local_cgcignore = find_cgcignore(ignore_root, explicit_path=None)
    patterns = list(default_patterns)
    if local_cgcignore and local_cgcignore.exists():
        user_patterns = parse_cgcignore_lines(
            local_cgcignore.read_text(encoding="utf-8").splitlines()
        )
        patterns = user_patterns + default_patterns
    return PathSpec.from_lines("gitwildmatch", patterns)


def _find_cgcignore_excluded_paths(
    file_paths: list[str], cwd: Path | None = None,
) -> list[str]:
    if not file_paths:
        return []

    spec = _load_cgcignore_spec(cwd)
    excluded: list[str] = []
    for path in file_paths:
        matchable_path = _matchable_ignore_path(path, cwd)
        if matchable_path and spec.match_file(matchable_path):
            excluded.append(path)
    return excluded


def _git_object_hash(path: str, cwd: Path | None = None) -> str | None:
    resolved_path = _resolve_packet_path(path, cwd)
    if not resolved_path.is_file():
        return None
    return _git_probe("hash-object", str(resolved_path), cwd=cwd)


def _head_object_hash(path: str, cwd: Path | None = None) -> str | None:
    git_path = _matchable_ignore_path(path, cwd)
    if not git_path:
        return None
    return _git_probe("rev-parse", f"HEAD:{git_path}", cwd=cwd)


def _find_stale_index_paths(
    *,
    source: str,
    file_paths: list[str],
    indexed_file_paths: set[str],
    cwd: Path | None = None,
) -> list[str]:
    """Best-effort stale-index detection for live worktree sources."""
    if source not in {"staged", "workdir"} or not indexed_file_paths:
        return []

    stale: list[str] = []
    for path, variants in _path_variant_map(file_paths, cwd).items():
        if not variants.intersection(indexed_file_paths):
            continue
        current_hash = _git_object_hash(path, cwd)
        head_hash = _head_object_hash(path, cwd)
        if current_hash and head_hash and current_hash != head_hash:
            stale.append(path)
    return sorted(stale)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _find_nodes_by_paths(
    conn: Any, file_paths: list[str], cwd: Path | None = None,
) -> list[dict[str, Any]]:
    """Find all code-entity nodes whose path matches the given files."""
    if not file_paths or conn is None:
        return []

    nodes: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    query_paths = _query_path_list(file_paths, cwd)
    path_list = ", ".join(f"'{p}'" for p in query_paths)

    for table in _CODE_NODE_TABLES:
        query = (
            f"MATCH (n:`{table}`) "
            f"WHERE n.path IN [{path_list}] "
            f"RETURN n.uid AS uid, n.name AS name, n.path AS path, "
            f"n.line_number AS line_number, label(n) AS kind"
        )
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                uid = row[0]
                if uid not in seen_uids:
                    seen_uids.add(uid)
                    nodes.append({
                        "uid": uid,
                        "name": row[1],
                        "file": (
                            f"{_display_path(row[2], cwd)}:{row[3]}"
                            if row[2] and row[3]
                            else _display_path(row[2], cwd)
                        ),
                        "kind": row[4],
                    })
        except Exception:
            continue

    return nodes


def _find_indexed_file_paths(
    conn: Any, file_paths: list[str], cwd: Path | None = None,
) -> set[str]:
    """Return file-path variants already covered by the graph.

    Some graphs may not populate explicit File nodes for every indexed path,
    but if any code-entity node already resolves to the file we should still
    treat that path as indexed for stale-index and omission checks.
    """
    if not file_paths or conn is None:
        return set()

    query_paths = _query_path_list(file_paths, cwd)
    path_list = ", ".join(f"'{p}'" for p in query_paths)
    query_path_set = set(query_paths)
    indexed: set[str] = set()

    queries = [
        f"MATCH (f:File) WHERE f.path IN [{path_list}] RETURN DISTINCT f.path AS path",
        *[
            (
                f"MATCH (n:`{table}`) "
                f"WHERE n.path IN [{path_list}] "
                f"RETURN DISTINCT n.path AS path"
            )
            for table in _CODE_NODE_TABLES
        ],
    ]

    for query in queries:
        try:
            result = conn.execute(query)
            while result.has_next():
                row = result.get_next()
                values = row.values() if isinstance(row, dict) else row
                for candidate in values:
                    if isinstance(candidate, str) and candidate in query_path_set:
                        indexed.add(candidate)
        except Exception:
            continue

    return indexed


def _is_test_path(path: str) -> bool:
    """Heuristic: does this file path look like a test file?"""
    if not path:
        return False
    parts = path.replace("\\", "/").split("/")
    basename = parts[-1]
    return (
        basename.startswith("test_")
        or basename.endswith("_test.py")
        or any(p in ("test", "tests") for p in parts[:-1])
    )


def _find_tested_uids(conn: Any, uids: list[str]) -> set[str]:
    """Return uids that have at least one caller from a test file (graph evidence)."""
    if not uids or conn is None:
        return set()

    uid_list = ", ".join(f"'{u}'" for u in uids)
    tested: set[str] = set()

    query = (
        f"MATCH (caller)-[:CALLS]->(target) "
        f"WHERE target.uid IN [{uid_list}] "
        f"RETURN DISTINCT target.uid AS uid, caller.path AS caller_path"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            if _is_test_path(row[1] or ""):
                tested.add(row[0])
    except Exception:
        pass

    return tested


def _find_callers_not_in_set(
    conn: Any,
    touched_uids: set[str],
) -> list[dict[str, Any]]:
    """Find nodes that CALL into the touched set but are not themselves touched."""
    if not touched_uids or conn is None:
        return []

    uid_list = ", ".join(f"'{uid}'" for uid in touched_uids)
    callers: list[dict[str, Any]] = []
    seen: set[str] = set()

    query = (
        f"MATCH (caller)-[r:CALLS]->(target) "
        f"WHERE target.uid IN [{uid_list}] AND NOT caller.uid IN [{uid_list}] "
        f"RETURN DISTINCT caller.uid AS uid, caller.name AS name, "
        f"caller.path AS path, caller.line_number AS line_number, "
        f"label(caller) AS kind"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            uid = row[0]
            if uid not in seen:
                seen.add(uid)
                callers.append({
                    "uid": uid,
                    "name": row[1],
                    "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                    "kind": row[4],
                })
    except Exception:
        pass

    return callers


def _find_callees_not_in_set(
    conn: Any,
    touched_uids: set[str],
) -> list[dict[str, Any]]:
    """Find nodes that the touched set CALLS but are not themselves touched."""
    if not touched_uids or conn is None:
        return []

    uid_list = ", ".join(f"'{uid}'" for uid in touched_uids)
    callees: list[dict[str, Any]] = []
    seen: set[str] = set()

    query = (
        f"MATCH (source)-[r:CALLS]->(callee) "
        f"WHERE source.uid IN [{uid_list}] AND NOT callee.uid IN [{uid_list}] "
        f"RETURN DISTINCT callee.uid AS uid, callee.name AS name, "
        f"callee.path AS path, callee.line_number AS line_number, "
        f"label(callee) AS kind"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            uid = row[0]
            if uid not in seen:
                seen.add(uid)
                callees.append({
                    "uid": uid,
                    "name": row[1],
                    "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                    "kind": row[4],
                })
    except Exception:
        pass

    return callees


def _find_cross_module_impact(
    conn: Any,
    touched_paths: set[str],
    cwd: Path | None = None,
) -> list[str]:
    """Find modules imported by touched files that are outside the touched set."""
    if not touched_paths or conn is None:
        return []

    query_paths = _query_path_list(sorted(touched_paths), cwd)
    path_list = ", ".join(f"'{p}'" for p in query_paths)
    modules: set[str] = set()

    query = (
        f"MATCH (f:File)-[r:IMPORTS]->(m:Module) "
        f"WHERE f.path IN [{path_list}] "
        f"RETURN DISTINCT m.name AS module_name"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            mod = row[0]
            if mod:
                # Extract top-level module name
                top = mod.split(".")[0]
                modules.add(top)
    except Exception:
        pass

    return sorted(modules)


def _synthesize_nodes_from_file(path: str) -> list[dict[str, Any]]:
    """Extract function/class nodes from a Python file via AST (worktree synthesis).

    Spec §4.3: for unindexed files, attempt worktree parse before omitting.
    Only supports .py files; other languages emit the omitted advisory.
    """
    if not path.endswith(".py"):
        return []
    try:
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path)
    except (SyntaxError, FileNotFoundError, UnicodeDecodeError, OSError):
        return []

    nodes: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append({
                "uid": f"{path}:{node.name}:{node.lineno}",
                "name": node.name,
                "file": f"{path}:{node.lineno}",
                "kind": "Function",
            })
        elif isinstance(node, ast.ClassDef):
            nodes.append({
                "uid": f"{path}:{node.name}:{node.lineno}",
                "name": node.name,
                "file": f"{path}:{node.lineno}",
                "kind": "Class",
            })
    return nodes


def _count_in_degree(conn: Any, uids: list[str]) -> dict[str, int]:
    """Count incoming CALLS edges for each uid (for truncation ranking)."""
    if not uids or conn is None:
        return {}

    uid_list = ", ".join(f"'{u}'" for u in uids)
    degrees: dict[str, int] = {}

    query = (
        f"MATCH (caller)-[r:CALLS]->(target) "
        f"WHERE target.uid IN [{uid_list}] "
        f"RETURN target.uid AS uid, count(caller) AS deg"
    )
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            degrees[row[0]] = row[1]
    except Exception:
        pass

    return degrees


# ---------------------------------------------------------------------------
# Truncation (§4.4)
# ---------------------------------------------------------------------------

def _truncate_bucket(
    nodes: list[dict[str, Any]],
    max_nodes: int,
    in_degrees: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Truncate a node bucket to max_nodes, preferring high in-degree.

    Returns (truncated_list, original_count).
    """
    total = len(nodes)
    if total <= max_nodes:
        return nodes, total

    if in_degrees:
        # Sort by in-degree descending, keep top max_nodes
        nodes_sorted = sorted(
            nodes,
            key=lambda n: in_degrees.get(n.get("uid", n.get("name", "")), 0),
            reverse=True,
        )
        return nodes_sorted[:max_nodes], total

    return nodes[:max_nodes], total


def _truncation_suggestion(source: str, advisories: list[dict[str, str]]) -> str:
    """Pick a context-aware truncation workaround suggestion per §4.4."""
    advisory_kinds = {a["kind"] for a in advisories}

    if source == "locked_files" and "untracked_only" in advisory_kinds:
        return (
            "Commit or stage your changes and re-run — "
            "the packet will narrow to just what you touched."
        )
    if source == "locked_files" and not advisory_kinds & {
        "empty_diff", "missing_base_ref", "untracked_only",
    }:
        return (
            "Once you start editing, re-run with --include-workdir "
            "to scope to changed files only."
        )
    if source == "locked_files":
        return (
            "Narrow with --files <subpath>, or query by intent: "
            "kkg context '<topic>'."
        )
    # workdir / staged / untracked with huge change set
    return "Commit in smaller logical chunks, or narrow with --files <subpath>."


# ---------------------------------------------------------------------------
# Fallback chain (§4.3)
# ---------------------------------------------------------------------------

def _detect_source(
    *,
    base: str | None,
    head: str | None,
    files: list[str] | None,
    include_staged: bool,
    include_workdir: bool,
    include_untracked: bool,
    cwd: Path | None,
) -> tuple[str, list[str], dict[str, int], list[dict[str, str]]]:
    """Walk the fallback chain. Returns (source, changed_files, diff_stats, advisories)."""
    advisories: list[dict[str, str]] = []

    # Forced source via --include-* flags
    if include_staged:
        files_list = _staged_files(cwd=cwd)
        stats = _staged_stats(cwd=cwd)
        return "staged", files_list, stats, advisories

    if include_workdir:
        files_list = _workdir_files(cwd=cwd)
        stats = _workdir_stats(cwd=cwd)
        return "workdir", files_list, stats, advisories

    if include_untracked:
        files_list = _untracked_files(filter_paths=files, cwd=cwd)
        stats = {"files": len(files_list), "additions": 0, "deletions": 0}
        advisories.append({
            "level": "warn", "kind": "untracked_only",
            "detail": "All paths are untracked — this lane's work is not committed yet.",
        })
        return "untracked", files_list, stats, advisories

    # Auto-fallback chain
    # 1. diff (base..head)
    if base and head:
        if not _ref_resolves(base, cwd=cwd):
            advisories.append({
                "level": "warn", "kind": "missing_base_ref",
                "detail": f"--base '{base}' does not resolve.",
            })
        elif not _ref_resolves(head, cwd=cwd):
            advisories.append({
                "level": "warn", "kind": "missing_base_ref",
                "detail": f"--head '{head}' does not resolve.",
            })
        else:
            # Check divergence
            if not _is_ancestor(base, head, cwd=cwd):
                advisories.append({
                    "level": "warn", "kind": "refs_diverged_from_main",
                    "detail": f"{base} is not an ancestor of {head}; diff spans a merge.",
                })
            files_list = _diff_files(base, head, cwd=cwd)
            stats = _diff_stats(base, head, cwd=cwd)
            if files_list:
                return "diff", files_list, stats, advisories
            advisories.append({
                "level": "warn", "kind": "empty_diff",
                "detail": f"Refs resolved but {base}..{head} produced an empty diff.",
            })

    # 2. staged
    files_list = _staged_files(cwd=cwd)
    if files_list:
        stats = _staged_stats(cwd=cwd)
        return "staged", files_list, stats, advisories

    # 3. workdir
    files_list = _workdir_files(cwd=cwd)
    if files_list:
        stats = _workdir_stats(cwd=cwd)
        return "workdir", files_list, stats, advisories

    # 4. untracked
    files_list = _untracked_files(filter_paths=files, cwd=cwd)
    if files_list:
        stats = {"files": len(files_list), "additions": 0, "deletions": 0}
        advisories.append({
            "level": "warn", "kind": "untracked_only",
            "detail": "All changed paths are untracked.",
        })
        return "untracked", files_list, stats, advisories

    # 5. locked_files (last resort)
    if files:
        advisories.append({
            "level": "warn", "kind": "no_diff_available",
            "detail": "No diff, staged, workdir, or untracked changes found. "
                      "Falling back to --files lock list.",
        })
        stats = {"files": len(files), "additions": 0, "deletions": 0}
        return "locked_files", files, stats, advisories

    # Nothing at all
    advisories.append({
        "level": "warn", "kind": "no_diff_available",
        "detail": "No changes detected and no --files provided.",
    })
    return "locked_files", [], {"files": 0, "additions": 0, "deletions": 0}, advisories


# ---------------------------------------------------------------------------
# Payload builder (pure function, testable)
# ---------------------------------------------------------------------------

def build_review_packet_payload(
    *,
    base: str | None = None,
    head: str | None = None,
    files: list[str] | None = None,
    include_staged: bool = False,
    include_workdir: bool = False,
    include_untracked: bool = False,
    max_nodes: int = _DEFAULT_MAX_NODES,
    conn: Any = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Build the spec-defined review-packet JSON payload."""

    repo_shape_advisories, forced_source = _probe_repo_shape(cwd)

    if forced_source == "locked_files":
        source = "locked_files"
        changed_files = list(files or [])
        stats = {"files": len(changed_files), "additions": 0, "deletions": 0}
        advisories = list(repo_shape_advisories)
    else:
        source, changed_files, stats, advisories = _detect_source(
            base=base, head=head, files=files,
            include_staged=include_staged,
            include_workdir=include_workdir,
            include_untracked=include_untracked,
            cwd=cwd,
        )
        advisories = repo_shape_advisories + advisories

    excluded_paths = _find_cgcignore_excluded_paths(changed_files, cwd)
    if excluded_paths:
        advisories.append({
            "level": "warn",
            "kind": "excluded_by_cgcignore",
            "detail": "File(s) excluded by .cgcignore: " + ", ".join(excluded_paths[:5])
            + ("..." if len(excluded_paths) > 5 else ""),
        })

    graph_changed_files = [path for path in changed_files if path not in set(excluded_paths)]

    # Graph lookups — graceful degradation if no connection
    touched_nodes = _find_nodes_by_paths(conn, graph_changed_files, cwd=cwd)
    indexed_file_paths = _find_indexed_file_paths(conn, graph_changed_files, cwd=cwd)
    touched_uids = {n["uid"] for n in touched_nodes}
    touched_paths = set(graph_changed_files)

    callers = _find_callers_not_in_set(conn, touched_uids)
    callees = _find_callees_not_in_set(conn, touched_uids)

    # Graph-based test evidence: which callers have a test-file caller?
    caller_uids = [c["uid"] for c in callers if c.get("uid")]
    tested_uids = _find_tested_uids(conn, caller_uids)
    for c in callers:
        c["untested"] = c.get("uid", "") not in tested_uids
    cross_module = _find_cross_module_impact(conn, touched_paths, cwd=cwd)

    # Detect unindexed files → worktree synthesis → explicit omission (§4.3)
    variant_map = _path_variant_map(graph_changed_files, cwd)
    unindexed_files = [
        file_path
        for file_path, variants in variant_map.items()
        if not variants.intersection(indexed_file_paths)
    ]

    # Try worktree synthesis for unindexed files (Python AST parse)
    synthesized: list[dict[str, Any]] = []
    truly_omitted: list[str] = []
    for uf in unindexed_files:
        synth = _synthesize_nodes_from_file(uf)
        if synth:
            synthesized.extend(synth)
        else:
            truly_omitted.append(uf)

    if synthesized:
        touched_nodes.extend(synthesized)
        touched_uids.update(n["uid"] for n in synthesized)
        synth_paths = sorted({s["file"].rsplit(":", 1)[0] for s in synthesized})
        advisories.append({
            "level": "info",
            "kind": "worktree_synthesized",
            "detail": (
                f"{len(synthesized)} node(s) synthesized from {len(synth_paths)} "
                "unindexed file(s) via AST parse: "
                + ", ".join(synth_paths[:5])
                + ("..." if len(synth_paths) > 5 else "")
            ),
        })

    if truly_omitted:
        names = ", ".join(truly_omitted[:5])
        advisories.append({
            "level": "info",
            "kind": "untracked_unindexed_omitted",
            "detail": (
                f"{len(truly_omitted)} file(s) not in graph and could not be "
                "synthesized (non-Python, parse error, or missing): "
                + names + ("..." if len(truly_omitted) > 5 else "")
            ),
        })

    stale_index_paths = _find_stale_index_paths(
        source=source,
        file_paths=graph_changed_files,
        indexed_file_paths=indexed_file_paths,
        cwd=cwd,
    )
    if stale_index_paths:
        advisories.append({
            "level": "warn",
            "kind": "stale_index",
            "detail": (
                "Working-tree content differs from the indexed graph for: "
                + ", ".join(stale_index_paths[:5])
                + ("..." if len(stale_index_paths) > 5 else "")
                + ". Run kkg index <path> or restart kkg watch."
            ),
        })

    # In-degree ranking for truncation
    all_uids = (
        [n.get("uid", "") for n in touched_nodes]
        + [n.get("uid", "") for n in callers]
        + [n.get("uid", "") for n in callees]
    )
    in_degrees = _count_in_degree(conn, [u for u in all_uids if u]) if conn else {}

    # Truncate per §4.4
    touched_trunc, touched_total = _truncate_bucket(touched_nodes, max_nodes, in_degrees)
    callers_trunc, callers_total = _truncate_bucket(callers, max_nodes, in_degrees)
    callees_trunc, callees_total = _truncate_bucket(callees, max_nodes, in_degrees)

    truncated = (
        touched_total > max_nodes
        or callers_total > max_nodes
        or callees_total > max_nodes
    )

    if truncated:
        advisories.append({
            "level": "warn",
            "kind": "packet_truncated",
            "detail": _truncation_suggestion(source, advisories),
        })

    # Untested-caller advisories (graph evidence: no CALLS edge from a test file)
    untested_callers = [c for c in callers_trunc if c.get("untested")]
    if untested_callers:
        names = ", ".join(c["name"] for c in untested_callers[:5])
        advisories.append({
            "level": "warn",
            "kind": "untested_caller",
            "detail": (
                f"{len(untested_callers)} caller(s) outside the diff with no "
                "test-file caller in the graph: "
                + names + ("..." if len(untested_callers) > 5 else "")
            ),
        })

    payload: dict[str, Any] = {
        "source": source,
        "base": base if source == "diff" else None,
        "head": head if source == "diff" else None,
        "diff_stats": stats,
        "touched_nodes": touched_trunc,
        "callers_not_in_diff": callers_trunc,
        "callees_not_in_diff": callees_trunc,
        "cross_module_impact": cross_module,
        "advisories": advisories,
    }

    if truncated:
        payload["truncated"] = True
        payload["total_nodes"] = {
            "touched": touched_total,
            "callers": callers_total,
            "callees": callees_total,
        }
        payload["returned_nodes"] = {
            "touched": len(touched_trunc),
            "callers": len(callers_trunc),
            "callees": len(callees_trunc),
        }

    return payload


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def review_packet_command(
    base: Optional[str] = typer.Option(
        None, "--base",
        help="Git ref for the base of the diff (e.g. main, abc123).",
    ),
    head: Optional[str] = typer.Option(
        None, "--head",
        help="Git ref for the head of the diff.",
    ),
    files: Optional[str] = typer.Option(
        None, "--files",
        help="Comma-separated file paths (lock list from btrain).",
    ),
    include_staged: bool = typer.Option(
        False, "--include-staged",
        help="Force source to 'staged' (skip diff attempt).",
    ),
    include_workdir: bool = typer.Option(
        False, "--include-workdir",
        help="Force source to 'workdir'.",
    ),
    include_untracked: bool = typer.Option(
        False, "--include-untracked",
        help="Force source to 'untracked'.",
    ),
    max_nodes: int = typer.Option(
        _DEFAULT_MAX_NODES, "--max-nodes",
        min=1,
        help="Per-bucket node cap (default 50).",
    ),
) -> None:
    """Generate a reviewer JSON packet with blast radius and advisories."""

    # Validate conflicting include flags
    forced = sum([include_staged, include_workdir, include_untracked])
    if forced > 1:
        error_payload = {
            "error": True,
            "kind": "conflicting_include_flags",
            "detail": "Only one --include-* flag may be set at a time.",
        }
        typer.echo(emit_json(error_payload), err=True)
        raise typer.Exit(code=1)

    file_list = [f.strip() for f in files.split(",") if f.strip()] if files else None

    # Try to get a graph connection; proceed without if unavailable
    conn = None
    try:
        conn = get_kuzu_connection()
    except Exception as exc:
        print(f"Warning: KùzuDB unavailable ({exc}); graph data will be empty.", file=sys.stderr)

    payload = build_review_packet_payload(
        base=base,
        head=head,
        files=file_list,
        include_staged=include_staged,
        include_workdir=include_workdir,
        include_untracked=include_untracked,
        max_nodes=max_nodes,
        conn=conn,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
