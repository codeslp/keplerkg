"""kkg repl: interactive session with sticky project and query history.

Phase 2.5: provides a conversational interface to cgraph commands with
session state (project, audit profile) that persists across invocations.
Bare text is interpreted as a semantic search query; prefixed text
dispatches to the named command's payload builder.

Built-in dot-commands:
    .project <slug>   Switch the active project
    .profile <name>   Set the audit profile (default/strict/soc2/minimal)
    .history          Show recent queries
    .commands         List available commands
    .help             Show help
    .quit / .exit     Exit the REPL
"""

from __future__ import annotations

import json
import os
import readline
import shlex
import sys
from collections import deque
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "repl"
SCHEMA_FILE = None
SUMMARY = "Interactive session with sticky project, profile, and query history."

_HISTORY_SIZE = 50
_PROMPT = "\033[36mkkg\033[0m> "
_PROMPT_WITH_PROJECT = "\033[36mkkg\033[0m[\033[33m{project}\033[0m]> "


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class ReplSession:
    """Mutable session state for the REPL loop."""

    def __init__(
        self,
        project: str | None = None,
        profile: str = "default",
    ) -> None:
        self.project = project
        self.profile = profile
        self.history: deque[str] = deque(maxlen=_HISTORY_SIZE)
        self.conn: Any = None

    def prompt(self) -> str:
        if self.project:
            return _PROMPT_WITH_PROJECT.format(project=self.project)
        return _PROMPT

    def ensure_conn(self) -> Any:
        if self.conn is None:
            self.conn = get_kuzu_connection()
        return self.conn

    def switch_project(self, slug: str) -> str:
        target = activate_project(slug)
        self.project = target.slug
        self.conn = None  # reset connection for new project
        return f"Switched to project '{target.slug}' (db: {target.db_path})"

    def set_profile(self, profile: str) -> str:
        valid = {"default", "strict", "soc2", "minimal"}
        if profile not in valid:
            return f"Unknown profile '{profile}'. Choose from: {', '.join(sorted(valid))}"
        self.profile = profile
        return f"Audit profile set to '{profile}'"


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def _dispatch_search(session: ReplSession, query: str) -> dict[str, Any]:
    from ..embeddings.runtime import resolve_embedding_config, create_provider
    from ..hybrid.ann import ann_search
    from ..hybrid.traverse import traverse
    from .context import _build_context_payload

    try:
        conn = session.ensure_conn()
    except Exception:
        return {"kind": "context", "seeds": [], "error": "KùzuDB unavailable"}

    config = resolve_embedding_config()
    provider = create_provider(config)
    query_vectors = provider.embed_texts([query])
    seeds = ann_search(conn, query_vectors[0], k=8)
    if not seeds:
        return _build_context_payload(query, [], {"callers": [], "callees": [], "imports": []})
    neighborhood = traverse(conn, [s["uid"] for s in seeds], depth=1)
    return _build_context_payload(query, seeds, neighborhood)


def _dispatch_impact(session: ReplSession, args: list[str]) -> dict[str, Any]:
    from .impact import build_impact_payload
    symbol = args[0] if args else ""
    kind = None
    if "--kind" in args:
        idx = args.index("--kind")
        kind = args[idx + 1] if idx + 1 < len(args) else None
    try:
        conn = session.ensure_conn()
    except Exception:
        conn = None
    return build_impact_payload(symbol=symbol, kind=kind, conn=conn)


def _dispatch_execution_flow(session: ReplSession, args: list[str]) -> dict[str, Any]:
    from .execution_flow import build_execution_flow_payload
    symbol = args[0] if args else ""
    kind = None
    if "--kind" in args:
        idx = args.index("--kind")
        kind = args[idx + 1] if idx + 1 < len(args) else None
    depth = 4
    if "--depth" in args:
        idx = args.index("--depth")
        try:
            depth = int(args[idx + 1]) if idx + 1 < len(args) else 4
        except ValueError:
            depth = 4
    try:
        conn = session.ensure_conn()
    except Exception:
        conn = None
    return build_execution_flow_payload(symbol=symbol, kind=kind, depth=depth, conn=conn)


def _dispatch_clusters(session: ReplSession, _args: list[str]) -> dict[str, Any]:
    from .clusters import build_clusters_payload
    try:
        conn = session.ensure_conn()
    except Exception:
        conn = None
    return build_clusters_payload(conn=conn, project=session.project)


def _dispatch_entrypoints(session: ReplSession, args: list[str]) -> dict[str, Any]:
    from .entrypoints import build_entrypoints_payload
    framework = None
    if "--framework" in args:
        idx = args.index("--framework")
        framework = args[idx + 1] if idx + 1 < len(args) else None
    limit = 20
    if "--limit" in args:
        idx = args.index("--limit")
        try:
            limit = int(args[idx + 1]) if idx + 1 < len(args) else 20
        except ValueError:
            limit = 20
    try:
        conn = session.ensure_conn()
    except Exception:
        conn = None
    return build_entrypoints_payload(
        conn=conn, framework_filter=framework, limit=limit, project=session.project,
    )


def _dispatch_audit(session: ReplSession, args: list[str]) -> dict[str, Any]:
    from .audit import build_audit_payload, build_list_payload
    if "--list" in args:
        return build_list_payload()
    scope = "all"
    if "--scope" in args:
        idx = args.index("--scope")
        scope = args[idx + 1] if idx + 1 < len(args) else "all"
    return build_audit_payload(scope=scope, profile=session.profile)


def _dispatch_manifest(_session: ReplSession, _args: list[str]) -> dict[str, Any]:
    from ..io.json_stdout import make_envelope
    from ..io.registry import get_command_registry
    registry = get_command_registry()
    return make_envelope("manifest", {
        "commands": registry,
        "total_commands": len(registry),
    })


_DISPATCH_TABLE: dict[str, Any] = {
    "search": lambda s, a: _dispatch_search(s, " ".join(a)),
    "impact": _dispatch_impact,
    "execution-flow": _dispatch_execution_flow,
    "clusters": _dispatch_clusters,
    "entrypoints": _dispatch_entrypoints,
    "audit": _dispatch_audit,
    "manifest": _dispatch_manifest,
}

_AVAILABLE_COMMANDS = sorted(_DISPATCH_TABLE.keys())


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_output(payload: dict[str, Any]) -> str:
    """Format a JSON payload for human-readable REPL output."""
    kind = payload.get("kind", "")

    # Search results — show ranked matches
    if kind == "context":
        seeds = payload.get("seeds", [])
        if not seeds:
            return "  No results."
        lines = []
        for i, s in enumerate(seeds, 1):
            score = f" ({s['score']:.3f})" if "score" in s else ""
            lines.append(f"  {i}. {s.get('name', '?')}{score}  {s.get('file', '')}")
        return "\n".join(lines)

    # Impact — compact summary
    if kind == "impact":
        m = payload.get("summary", {})
        parts = [
            f"{m.get('matches', 0)} match(es)",
            f"{m.get('callers', 0)} callers",
            f"{m.get('callees', 0)} callees",
        ]
        ep = m.get("entrypoint_callers", 0) + m.get("entrypoint_callees", 0)
        if ep:
            parts.append(f"{ep} entry points")
        return "  " + " · ".join(parts)

    # Execution flow — tree summary
    if kind == "execution_flow":
        m = payload.get("summary", {})
        return f"  {m.get('total_nodes', 0)} nodes, {m.get('total_edges', 0)} edges, max depth {m.get('max_depth_reached', 0)}"

    # Clusters
    if kind == "clusters":
        s = payload.get("stats", {})
        return f"  {s.get('communities', 0)} communities, {s.get('total_nodes', 0)} nodes, {s.get('cross_community_edges', 0)} cross-boundary"

    # Entrypoints — top entries
    if kind == "entrypoints":
        entries = payload.get("entrypoints", [])[:5]
        if not entries:
            return "  No entry points found."
        lines = [f"  {e.get('name', '?')} ({e.get('framework', '?')}) score={e.get('score', 0)}  {e.get('file', '')}" for e in entries]
        total = payload.get("summary", {}).get("total", len(entries))
        if total > 5:
            lines.append(f"  ... and {total - 5} more")
        return "\n".join(lines)

    # Audit
    if kind == "audit":
        violations = payload.get("violations", [])
        total = sum(len(v.get("offenders", [])) for v in violations)
        if not total:
            return "  \033[32mNo violations.\033[0m"
        return f"  {total} violation(s) across {len(violations)} rule(s)"

    # Default — compact JSON
    return "  " + json.dumps(payload, sort_keys=True)[:200]


# ---------------------------------------------------------------------------
# Dot-command handlers
# ---------------------------------------------------------------------------

def _handle_dot_command(session: ReplSession, line: str) -> str | None:
    """Handle dot-commands. Returns output string, or None to quit."""
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in (".quit", ".exit", ".q"):
        return None

    if cmd == ".project":
        if not arg:
            return f"  Current project: {session.project or '(none)'}"
        return "  " + session.switch_project(arg)

    if cmd == ".profile":
        if not arg:
            return f"  Current profile: {session.profile}"
        return "  " + session.set_profile(arg)

    if cmd == ".history":
        if not session.history:
            return "  (empty)"
        return "\n".join(f"  {i+1}. {h}" for i, h in enumerate(session.history))

    if cmd == ".commands":
        return "  Commands: " + ", ".join(_AVAILABLE_COMMANDS) + "\n  Dot-commands: .project, .profile, .history, .commands, .help, .quit"

    if cmd == ".help":
        return (
            "  Type a query to search, or use a command:\n"
            "    search <query>       Semantic search\n"
            "    impact <symbol>      Symbol impact analysis\n"
            "    execution-flow <sym> Call chain trace\n"
            "    clusters             Community detection\n"
            "    entrypoints          Entry-point ranking\n"
            "    audit [--list]       Run standards audit\n"
            "    manifest             List all commands\n"
            "  Dot-commands:\n"
            "    .project [slug]      Get/set active project\n"
            "    .profile [name]      Get/set audit profile\n"
            "    .history             Recent queries\n"
            "    .quit                Exit"
        )

    return f"  Unknown command '{cmd}'. Type .help for usage."


# ---------------------------------------------------------------------------
# REPL loop (pure, testable)
# ---------------------------------------------------------------------------

def run_repl_loop(
    session: ReplSession,
    *,
    input_fn: Any = None,
    output_fn: Any = None,
) -> None:
    """Run the interactive REPL loop.

    *input_fn* and *output_fn* are injectable for testing.
    Defaults to ``input()`` and ``print()``.
    """
    _input = input_fn or input
    _output = output_fn or print

    _output("kkg interactive session. Type .help for commands, .quit to exit.")
    if session.project:
        _output(f"  Project: {session.project}")
    _output(f"  Profile: {session.profile}")
    _output("")

    while True:
        try:
            line = _input(session.prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            _output("")
            break

        if not line:
            continue

        session.history.append(line)

        # Dot-commands
        if line.startswith("."):
            result = _handle_dot_command(session, line)
            if result is None:
                break
            _output(result)
            continue

        # Parse command vs bare query
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()

        cmd_name = tokens[0] if tokens else ""
        cmd_args = tokens[1:] if len(tokens) > 1 else []

        if cmd_name in _DISPATCH_TABLE:
            try:
                payload = _DISPATCH_TABLE[cmd_name](session, cmd_args)
                _output(_format_output(payload))
            except Exception as exc:
                _output(f"  \033[31mError: {exc}\033[0m")
        else:
            # Bare text → search
            try:
                payload = _dispatch_search(session, line)
                _output(_format_output(payload))
            except Exception as exc:
                _output(f"  \033[31mSearch error: {exc}\033[0m")


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def repl_command(
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
    profile: str = typer.Option(
        "default",
        "--profile",
        help="Initial audit profile (default/strict/soc2/minimal).",
    ),
) -> None:
    """Start an interactive kkg session.

    Provides a REPL with sticky project context, audit profile,
    and query history.  Bare text is treated as a semantic search
    query; command names dispatch to their payload builders directly.
    """
    target = activate_project(project)

    session = ReplSession(project=target.slug, profile=profile)

    # Pre-warm the DB connection
    try:
        session.conn = get_kuzu_connection()
    except Exception as exc:
        print(
            f"Warning: KùzuDB unavailable ({exc}); commands will run without graph data.",
            file=sys.stderr,
        )

    # Set up readline history
    try:
        readline.set_history_length(_HISTORY_SIZE)
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass

    run_repl_loop(session)
    raise typer.Exit(code=0)
