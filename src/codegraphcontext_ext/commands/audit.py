"""kkg audit: code-quality standards runner.

Spec §6.6 — loads YAML rules from ``standards/``, runs their Cypher
queries against the graph, and reports violations.  Distinct from
``kkg advise`` (§4.2 tip-lookup): audit is the heavier analysis
command used by PostToolUse/Stop hooks, pre-handoff, and CI.

Output: JSON with violations, counts, and hard_zero status.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..config import resolve_cgraph_config, StandardsConfig, STANDARDS_PRESETS
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..standards.loader import (
    RuleResult,
    load_exemptions,
    load_rules,
    run_all_rules,
    run_rule,
)

COMMAND_NAME = "audit"
SCHEMA_FILE = "audit.json"
SUMMARY = "Run code-quality standards against the graph and report violations."

_DEFAULT_STANDARDS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "standards"


def _find_standards_dir() -> Path:
    """Locate the standards/ directory — check repo root first, then package."""
    # Walk up from cwd looking for standards/
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        d = candidate / "standards"
        if d.is_dir() and (d / "_exemptions.yaml").is_file():
            return d
    return _DEFAULT_STANDARDS_DIR


# ---------------------------------------------------------------------------
# Payload builder (pure, testable)
# ---------------------------------------------------------------------------

def build_audit_payload(
    standards_dir: Path | None = None,
    scope: str = "all",
    category: str | None = None,
    require_hard_zero: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build the audit response payload.

    Respects config from ``[cgraph.standards]``: profile presets,
    category filtering, per-rule severity overrides, and disabled rules.
    """
    std_dir = standards_dir or _find_standards_dir()
    advisories: list[dict[str, Any]] = []
    results: list[RuleResult] = []
    rules_evaluated = 0
    error_msg: str | None = None

    # Load config (profile, overrides, categories)
    cfg = resolve_cgraph_config()
    std_cfg = cfg.standards
    if profile:
        std_cfg.profile = profile
        from ..config import _apply_preset
        _apply_preset(std_cfg)

    rules = load_rules(std_dir)

    # Apply category filter (from config or CLI)
    active_categories = [category] if category else std_cfg.categories
    if "all" not in active_categories:
        rules = [r for r in rules if r.category in active_categories]

    # Apply per-rule overrides (severity changes + "off" disables)
    filtered_rules = []
    for rule in rules:
        override = std_cfg.overrides.get(rule.id)
        if override == "off":
            continue  # Rule disabled
        if override and override != "off":
            rule.severity = "hard" if override in ("blocker", "critical", "hard") else "warn"
        # Check hard_stop list — promote to hard if listed
        if rule.id in std_cfg.hard_stop and rule.severity != "hard":
            rule.severity = "hard"
        filtered_rules.append(rule)

    rules = filtered_rules
    rules_evaluated = len(rules)

    try:
        conn = get_kuzu_connection()
    except (SystemExit, Exception) as exc:
        error_msg = f"Could not connect to KùzuDB: {exc}"
        return {
            "ok": False,
            "kind": "audit",
            "scope": scope,
            "standards_evaluated": rules_evaluated,
            "advisories": [],
            "counts": {"warn": 0, "hard": 0},
            "hard_zero": False,  # Fail closed — can't verify, so assume not clean
            "error": error_msg,
        }

    exemptions = load_exemptions(std_dir)
    for rule in rules:
        result = run_rule(conn, rule, exemptions)
        results.append(result)
        if result.fired:
            advisories.append(result.to_advisory())

    warn_count = sum(1 for a in advisories if a["severity"] == "warn")
    hard_count = sum(1 for a in advisories if a["severity"] == "hard")
    hard_zero = hard_count == 0

    return {
        "ok": True,
        "kind": "audit",
        "scope": scope,
        "standards_evaluated": rules_evaluated,
        "advisories": advisories,
        "counts": {"warn": warn_count, "hard": hard_count},
        "hard_zero": hard_zero,
    }


def build_list_payload(standards_dir: Path | None = None) -> dict[str, Any]:
    """List all registered standards and their severity."""
    std_dir = standards_dir or _find_standards_dir()
    rules = load_rules(std_dir)
    return {
        "kind": "audit_list",
        "standards": [
            {
                "id": r.id,
                "severity": r.severity,
                "category": r.category,
                "summary": r.summary,
            }
            for r in rules
        ],
    }


def build_explain_payload(
    standard_id: str,
    standards_dir: Path | None = None,
) -> dict[str, Any]:
    """Show rule definition, thresholds, and exemptions."""
    std_dir = standards_dir or _find_standards_dir()
    rules = load_rules(std_dir)
    exemptions = load_exemptions(std_dir)

    rule = next((r for r in rules if r.id == standard_id), None)
    if rule is None:
        return {
            "kind": "audit_explain",
            "error": f"Standard '{standard_id}' not found.",
        }

    return {
        "kind": "audit_explain",
        "id": rule.id,
        "severity": rule.severity,
        "category": rule.category,
        "summary": rule.summary,
        "query": rule.query,
        "thresholds": rule.thresholds,
        "suggestion": rule.suggestion,
        "evidence": rule.evidence,
        "exemptions_file": rule.exemptions,
        "exemption_paths": exemptions.paths if rule.exemptions else [],
    }


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def audit_command(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Audit scope: diff, session, lane, all, or function:<uid>.",
    ),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Filter to a specific rule category (e.g. coupling, complexity, compliance).",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Standards preset: default, strict, soc2, minimal.",
    ),
    list_standards: bool = typer.Option(
        False,
        "--list",
        help="List all registered standards and their severity.",
    ),
    explain: Optional[str] = typer.Option(
        None,
        "--explain",
        help="Show definition for a specific standard ID.",
    ),
    require_hard_zero: bool = typer.Option(
        False,
        "--require-hard-zero",
        help="Exit 2 if any hard violation fires.",
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json, summary.",
    ),
) -> None:
    """Run code-quality standards against the graph."""
    if list_standards:
        payload = build_list_payload()
        typer.echo(emit_json(payload))
        raise typer.Exit(code=0)

    if explain:
        payload = build_explain_payload(explain)
        typer.echo(emit_json(payload))
        raise typer.Exit(code=0)

    payload = build_audit_payload(
        scope=scope,
        category=category,
        require_hard_zero=require_hard_zero,
        profile=profile,
    )

    if fmt == "summary":
        _print_summary(payload)
    else:
        typer.echo(emit_json(payload))

    if require_hard_zero and not payload.get("hard_zero", True):
        raise typer.Exit(code=2)

    raise typer.Exit(code=0)


def _print_summary(payload: dict[str, Any]) -> None:
    """Print a human-readable summary table to stderr."""
    advisories = payload.get("advisories", [])
    counts = payload.get("counts", {})

    print(f"\nkkg audit: {payload['standards_evaluated']} standards evaluated", file=sys.stderr)
    print(f"  warn: {counts.get('warn', 0)}  hard: {counts.get('hard', 0)}", file=sys.stderr)

    for adv in advisories:
        severity = adv["severity"].upper()
        kind = adv["standard_id"]
        count = len(adv.get("offenders", []))
        print(f"  [{severity}] {kind}: {count} offender(s)", file=sys.stderr)
        if adv.get("suggestion"):
            print(f"         → {adv['suggestion'][:120]}", file=sys.stderr)

    if payload.get("hard_zero"):
        print("  ✓ hard_zero: no hard violations", file=sys.stderr)
    else:
        print("  ✗ HARD VIOLATIONS FOUND", file=sys.stderr)
    print("", file=sys.stderr)

    # Still emit JSON on stdout for piping
    print(emit_json(payload))
