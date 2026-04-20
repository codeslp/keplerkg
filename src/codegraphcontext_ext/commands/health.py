"""kkg health: A-F letter-grade health score from audit data.

Phase 7 — Aggregates audit rule violations into a single composite
score (0-100) mapped to a letter grade (A-F).  Useful for dashboards,
CI gates, and quick "how are we doing?" checks.

Scoring model:
  - Start at 100 points
  - Each hard violation:  -8 points per offender (capped at -40 per rule)
  - Each warn violation:  -2 points per offender (capped at -15 per rule)
  - Floor at 0

Letter grade mapping:
  A  90-100    Excellent — no hard violations, few warnings
  B  75-89     Good — minor issues only
  C  60-74     Fair — some quality concerns
  D  40-59     Poor — significant issues
  F   0-39     Failing — critical violations present

Usage:
    kkg health
    kkg health --project flask --profile strict
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json, make_envelope
from ..project import PROJECT_OPTION_HELP, activate_project
from .audit import build_audit_payload

COMMAND_NAME = "health"
SCHEMA_FILE = "health.json"
SUMMARY = "A-F letter-grade health score computed from audit violations."

# Scoring constants
_HARD_PENALTY_PER_OFFENDER = 8
_HARD_PENALTY_CAP_PER_RULE = 40
_WARN_PENALTY_PER_OFFENDER = 2
_WARN_PENALTY_CAP_PER_RULE = 15
_MAX_SCORE = 100

_GRADE_THRESHOLDS = [
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (40, "D"),
    (0,  "F"),
]


def _score_to_grade(score: int) -> str:
    """Map a 0-100 score to a letter grade."""
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def _grade_description(grade: str) -> str:
    """Human-readable description of what the grade means."""
    return {
        "A": "Excellent — no hard violations, few warnings",
        "B": "Good — minor issues only",
        "C": "Fair — some quality concerns",
        "D": "Poor — significant issues",
        "F": "Failing — critical violations present",
    }.get(grade, "Unknown")


# ---------------------------------------------------------------------------
# Payload builder (pure, testable)
# ---------------------------------------------------------------------------

def build_health_payload(
    *,
    scope: str = "all",
    profile: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Build the health score payload from audit data.

    Calls build_audit_payload internally, then computes the composite
    score and letter grade.
    """
    audit = build_audit_payload(scope=scope, profile=profile)

    if not audit.get("ok"):
        return make_envelope("health", {
            "score": 0,
            "grade": "F",
            "description": "Failing — could not connect to database",
            "audit_summary": {
                "standards_evaluated": audit.get("standards_evaluated", 0),
                "hard": 0,
                "warn": 0,
                "total_offenders": 0,
            },
            "breakdown": [],
        }, ok=False, error=audit.get("error"), project=project)

    # Compute penalty per rule
    breakdown: list[dict[str, Any]] = []
    total_penalty = 0

    for advisory in audit.get("advisories", []):
        severity = advisory.get("severity", "warn")
        offender_count = len(advisory.get("offenders", []))
        rule_id = advisory.get("standard_id", advisory.get("kind", "unknown"))

        if severity == "hard":
            penalty = min(
                offender_count * _HARD_PENALTY_PER_OFFENDER,
                _HARD_PENALTY_CAP_PER_RULE,
            )
        else:
            penalty = min(
                offender_count * _WARN_PENALTY_PER_OFFENDER,
                _WARN_PENALTY_CAP_PER_RULE,
            )

        total_penalty += penalty
        breakdown.append({
            "rule": rule_id,
            "severity": severity,
            "offenders": offender_count,
            "penalty": penalty,
        })

    score = max(0, _MAX_SCORE - total_penalty)
    has_hard = any(b["severity"] == "hard" for b in breakdown)
    grade = _score_to_grade(score)
    # Any hard violation disqualifies grade A
    if has_hard and grade == "A":
        grade = "B"

    counts = audit.get("counts", {"warn": 0, "hard": 0})
    total_offenders = sum(
        len(a.get("offenders", []))
        for a in audit.get("advisories", [])
    )

    return make_envelope("health", {
        "score": score,
        "grade": grade,
        "description": _grade_description(grade),
        "audit_summary": {
            "standards_evaluated": audit.get("standards_evaluated", 0),
            "hard": counts.get("hard", 0),
            "warn": counts.get("warn", 0),
            "total_offenders": total_offenders,
        },
        "breakdown": sorted(breakdown, key=lambda b: b["penalty"], reverse=True),
    }, project=project)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def health_command(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Audit scope: all, diff, session, lane.",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Standards profile: default, strict, soc2, minimal.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Compute an A-F health score from audit violations."""
    target = activate_project(project)

    payload = build_health_payload(scope=scope, profile=profile, project=target.slug)

    # Human-readable summary to stderr
    grade = payload.get("grade", "?")
    score = payload.get("score", 0)
    summary = payload.get("audit_summary", {})
    print(
        f"Health: {grade} ({score}/100) — "
        f"{summary.get('hard', 0)} hard, {summary.get('warn', 0)} warn, "
        f"{summary.get('standards_evaluated', 0)} rules evaluated",
        file=sys.stderr,
    )

    typer.echo(emit_json(payload))
    raise typer.Exit(code=0 if payload.get("ok") else 1)
