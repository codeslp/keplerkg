"""kkg audit: code-quality standards runner.

Spec §6.6 — loads YAML rules from ``standards/``, runs their Cypher
queries against the graph, and reports violations.  Distinct from
``kkg advise`` (§4.2 tip-lookup): audit is the heavier analysis
command used by PostToolUse/Stop hooks, pre-handoff, and CI.

Output: JSON with violations, counts, and hard_zero status.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..config import resolve_cgraph_config, StandardsConfig, STANDARDS_PRESETS
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project
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


# ---------------------------------------------------------------------------
# Scope resolution — which files are in scope for this audit run?
# ---------------------------------------------------------------------------

def _resolve_scope_files(scope: str) -> set[str] | None:
    """Return the set of files in scope, or None for 'all' (no filtering).

    Scopes:
      - 'all' → None (run against everything)
      - 'diff' → git diff --name-only (unstaged + staged changes)
      - 'session' → git diff HEAD --name-only (all changes since last commit)
      - 'lane' → same as session (lane isolation is via btrain locks)
    """
    if scope == "all":
        return None

    if scope in ("diff", "session", "lane"):
        try:
            # Staged + unstaged
            out = subprocess.check_output(
                ["git", "diff", "HEAD", "--name-only"],
                text=True,
                timeout=5,
            )
            files = {f.strip() for f in out.splitlines() if f.strip()}
            # Also include untracked
            out2 = subprocess.check_output(
                ["git", "ls-files", "--others", "--exclude-standard"],
                text=True,
                timeout=5,
            )
            files |= {f.strip() for f in out2.splitlines() if f.strip()}
            return files
        except (subprocess.SubprocessError, FileNotFoundError):
            return None  # Fall back to all

    # function:<uid> — not filtered at file level
    return None


def _filter_violations_by_scope(
    advisories: list[dict[str, Any]],
    scope_files: set[str] | None,
) -> list[dict[str, Any]]:
    """Remove offenders whose file is not in scope. Drop advisories with zero offenders."""
    if scope_files is None:
        return advisories

    filtered = []
    for adv in advisories:
        scoped_offenders = [
            o for o in adv.get("offenders", [])
            if o.get("path") and any(
                o["path"].endswith(f) or f in o["path"]
                for f in scope_files
            )
        ]
        if scoped_offenders:
            adv = {**adv, "offenders": scoped_offenders}
            filtered.append(adv)
    return filtered


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

    # Apply per-rule overrides (severity, thresholds, "off" disables)
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
        # Apply threshold overrides from config
        threshold_overrides = std_cfg.thresholds.get(rule.id)
        if threshold_overrides:
            rule.thresholds.update(threshold_overrides)
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

    # Wire up embedding provider for naming rules (F03 needs live embedding)
    if any(r.detection_method == "embedding" for r in rules):
        try:
            from ..embeddings.runtime import resolve_embedding_config
            from ..embeddings.providers import create_provider
            from ..standards.naming_rules import set_provider
            emb_config = resolve_embedding_config(
                provider=None, model=None, dimensions=None,
            )
            set_provider(create_provider(emb_config))
        except Exception:
            pass  # Embedding rules degrade gracefully

    exemptions = load_exemptions(std_dir)
    for rule in rules:
        result = run_rule(conn, rule, exemptions)
        results.append(result)
        if result.fired:
            advisories.append(result.to_advisory())

    # Scope filtering — only report violations on files that are in scope
    scope_files = _resolve_scope_files(scope)
    if scope_files is not None:
        advisories = _filter_violations_by_scope(advisories, scope_files)

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
# Calibration report
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list[float], p: int) -> float:
    """Compute the p-th percentile using linear interpolation."""
    if not sorted_vals:
        return 0.0
    k = (p / 100) * (len(sorted_vals) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _compute_distribution(values: list[float]) -> dict[str, float]:
    """Compute distribution percentiles from metric values."""
    if not values:
        return {}
    sv = sorted(values)
    return {
        "min": sv[0],
        "p25": _percentile(sv, 25),
        "p50": _percentile(sv, 50),
        "p75": _percentile(sv, 75),
        "p90": _percentile(sv, 90),
        "p95": _percentile(sv, 95),
        "p99": _percentile(sv, 99),
        "max": sv[-1],
    }


def _detect_comparison_op(query: str, placeholder: str) -> str:
    """Detect whether a threshold placeholder uses > or >= in the query.

    Returns '>=' if the query contains '>= $placeholder', otherwise '>'.
    """
    import re
    if re.search(rf'>= *\${re.escape(placeholder)}\b', query):
        return ">="
    return ">"


def _count_violations(
    metrics: list[float],
    threshold: float,
    op: str,
) -> int:
    """Count metric values that violate a threshold with the given operator."""
    if op == ">=":
        return sum(1 for m in metrics if m >= threshold)
    return sum(1 for m in metrics if m > threshold)


def _calibrate_rule(
    conn: Any,
    rule: "StandardRule",
    exemptions: Any,
) -> dict[str, Any]:
    """Run a single rule with thresholds zeroed to capture full population."""
    from ..standards.loader import StandardRule as _SR, resolve_query

    # Detect comparison operators before zeroing thresholds.
    # Keys not found in the query (e.g. $hard when only $warn appears)
    # inherit the operator from the "warn" key.
    ops: dict[str, str] = {}
    for key in rule.thresholds:
        ops[key] = _detect_comparison_op(rule.query, key)
    warn_op = ops.get("warn", ">")
    for key in ops:
        if f"${key}" not in rule.query:
            ops[key] = warn_op

    # Copy the rule with all thresholds set to 0
    cal_rule = _SR(
        id=rule.id,
        advisory_kind=rule.advisory_kind,
        severity=rule.severity,
        summary=rule.summary,
        query=rule.query,
        thresholds={k: 0 for k in rule.thresholds},
        suggestion=rule.suggestion,
        exemptions=rule.exemptions,
        evidence=rule.evidence,
        category=rule.category,
        detection_method=rule.detection_method,
    )

    resolved = resolve_query(cal_rule, exemptions)

    try:
        result = conn.execute(resolved)
    except Exception as exc:
        return {
            "id": rule.id,
            "category": rule.category,
            "severity": rule.severity,
            "current_thresholds": rule.thresholds,
            "error": f"{type(exc).__name__}: {exc}",
            "population": 0,
            "distribution": {},
            "violations_at_current": {},
            "candidate_thresholds": [],
        }

    # Collect metric values from last column (index 4)
    metrics: list[float] = []
    while result.has_next():
        row = result.get_next()
        if len(row) > 4 and row[4] is not None:
            try:
                metrics.append(float(row[4]))
            except (ValueError, TypeError):
                pass

    distribution = _compute_distribution(metrics)

    # Count violations at current thresholds using the correct operator
    violations_at_current: dict[str, int] = {}
    for key, val in rule.thresholds.items():
        violations_at_current[key] = _count_violations(metrics, val, ops.get(key, ">"))

    # Generate candidate thresholds at each percentile
    # Use the warn operator for candidates since that's the primary tuning target
    warn_op = ops.get("warn", ">")
    candidates: list[dict[str, Any]] = []
    if metrics:
        for label, pval in distribution.items():
            if label in ("min", "max"):
                continue
            threshold_val = math.ceil(pval)
            candidates.append({
                "percentile": label,
                "value": threshold_val,
                "violations_above": _count_violations(metrics, threshold_val, warn_op),
            })

    return {
        "id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "current_thresholds": rule.thresholds,
        "population": len(metrics),
        "distribution": distribution,
        "violations_at_current": violations_at_current,
        "candidate_thresholds": candidates,
    }


def build_calibration_payload(
    standards_dir: Path | None = None,
    category: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build a calibration report showing metric distributions for threshold tuning.

    For each threshold-bearing Cypher rule, runs the query with thresholds
    zeroed to capture the full metric population, then computes percentile
    distributions and violation counts at current and candidate thresholds.
    """
    std_dir = standards_dir or _find_standards_dir()

    cfg = resolve_cgraph_config()
    std_cfg = cfg.standards
    if profile:
        std_cfg.profile = profile
        from ..config import _apply_preset
        _apply_preset(std_cfg)

    rules = load_rules(std_dir)

    # Only threshold-bearing Cypher rules are calibratable
    threshold_rules = [
        r for r in rules
        if r.thresholds and r.detection_method == "cypher"
    ]

    active_categories = [category] if category else std_cfg.categories
    if "all" not in active_categories:
        threshold_rules = [r for r in threshold_rules if r.category in active_categories]

    # Apply per-rule overrides (disabled, severity, thresholds, hard_stop)
    # Mirrors the same logic in build_audit_payload.
    filtered: list[Any] = []
    for rule in threshold_rules:
        override = std_cfg.overrides.get(rule.id)
        if override == "off":
            continue
        if override and override != "off":
            rule.severity = "hard" if override in ("blocker", "critical", "hard") else "warn"
        if rule.id in std_cfg.hard_stop and rule.severity != "hard":
            rule.severity = "hard"
        threshold_overrides = std_cfg.thresholds.get(rule.id)
        if threshold_overrides:
            rule.thresholds.update(threshold_overrides)
        filtered.append(rule)
    threshold_rules = filtered

    try:
        conn = get_kuzu_connection()
    except (SystemExit, Exception) as exc:
        return {
            "ok": False,
            "kind": "audit_calibration",
            "error": f"Could not connect to KùzuDB: {exc}",
            "rules_analyzed": 0,
            "rules": [],
        }

    exemptions = load_exemptions(std_dir)
    rule_reports = [
        _calibrate_rule(conn, rule, exemptions)
        for rule in threshold_rules
    ]

    return {
        "ok": True,
        "kind": "audit_calibration",
        "rules_analyzed": len(rule_reports),
        "rules": rule_reports,
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
    calibration_report: bool = typer.Option(
        False,
        "--calibration-report",
        help="Show metric distributions and candidate thresholds for tuning.",
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json, summary.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Run code-quality standards against the graph."""
    activate_project(project)

    if list_standards:
        payload = build_list_payload()
        typer.echo(emit_json(payload))
        raise typer.Exit(code=0)

    if explain:
        payload = build_explain_payload(explain)
        typer.echo(emit_json(payload))
        raise typer.Exit(code=0)

    if calibration_report:
        payload = build_calibration_payload(
            category=category,
            profile=profile,
        )
        if fmt == "summary":
            _print_calibration_summary(payload)
        else:
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


def _print_calibration_summary(payload: dict[str, Any]) -> None:
    """Print a human-readable calibration report to stderr."""
    rules = payload.get("rules", [])
    print(f"\nkkg audit --calibration-report: {len(rules)} rules analyzed\n", file=sys.stderr)

    for r in rules:
        if r.get("error"):
            print(f"  {r['id']}: ERROR — {r['error']}", file=sys.stderr)
            continue

        dist = r.get("distribution", {})
        pop = r.get("population", 0)
        current = r.get("current_thresholds", {})
        viol = r.get("violations_at_current", {})

        print(f"  {r['id']} ({r['category']}, {r['severity']})", file=sys.stderr)
        print(f"    population: {pop}", file=sys.stderr)
        if dist:
            print(
                f"    distribution: min={dist.get('min','-')}"
                f"  p50={dist.get('p50','-')}"
                f"  p75={dist.get('p75','-')}"
                f"  p90={dist.get('p90','-')}"
                f"  p95={dist.get('p95','-')}"
                f"  max={dist.get('max','-')}",
                file=sys.stderr,
            )
        for key, val in current.items():
            print(f"    {key} threshold: {val} → {viol.get(key, '?')} violations", file=sys.stderr)
        print("", file=sys.stderr)

    # Still emit JSON on stdout for piping
    print(emit_json(payload))
