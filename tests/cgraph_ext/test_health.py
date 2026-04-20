"""Tests for kkg health — A-F letter-grade health score."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from codegraphcontext_ext.commands.health import (
    _score_to_grade,
    _grade_description,
    build_health_payload,
    _HARD_PENALTY_PER_OFFENDER,
    _WARN_PENALTY_PER_OFFENDER,
    _HARD_PENALTY_CAP_PER_RULE,
    _WARN_PENALTY_CAP_PER_RULE,
    _MAX_SCORE,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "health.json"


# ── Grade mapping tests ──────────────────────────────────────────


def test_score_to_grade_boundaries():
    assert _score_to_grade(100) == "A"
    assert _score_to_grade(90) == "A"
    assert _score_to_grade(89) == "B"
    assert _score_to_grade(75) == "B"
    assert _score_to_grade(74) == "C"
    assert _score_to_grade(60) == "C"
    assert _score_to_grade(59) == "D"
    assert _score_to_grade(40) == "D"
    assert _score_to_grade(39) == "F"
    assert _score_to_grade(0) == "F"


def test_grade_description_all_grades():
    for grade in ("A", "B", "C", "D", "F"):
        desc = _grade_description(grade)
        assert isinstance(desc, str)
        assert len(desc) > 10


# ── Payload builder tests ────────────────────────────────────────


def _mock_audit_payload(advisories, standards_evaluated=20):
    """Build a fake audit payload for testing."""
    warn_count = sum(1 for a in advisories if a["severity"] == "warn")
    hard_count = sum(1 for a in advisories if a["severity"] == "hard")
    return {
        "ok": True,
        "kind": "audit",
        "scope": "all",
        "standards_evaluated": standards_evaluated,
        "advisories": advisories,
        "counts": {"warn": warn_count, "hard": hard_count},
        "hard_zero": hard_count == 0,
    }


def test_health_perfect_score_no_violations():
    audit = _mock_audit_payload([])
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    assert payload["ok"] is True
    assert payload["kind"] == "health"
    assert payload["score"] == 100
    assert payload["grade"] == "A"
    assert payload["audit_summary"]["hard"] == 0
    assert payload["audit_summary"]["warn"] == 0
    assert payload["breakdown"] == []


def test_health_warn_violations_deduct_correctly():
    advisories = [
        {
            "severity": "warn",
            "standard_id": "function_too_long",
            "offenders": [{"uid": f"u{i}"} for i in range(3)],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    # 3 offenders * 2 pts = 6 pts penalty → score 94 → A
    assert payload["score"] == 94
    assert payload["grade"] == "A"
    assert len(payload["breakdown"]) == 1
    assert payload["breakdown"][0]["penalty"] == 6


def test_health_hard_violations_deduct_heavily():
    advisories = [
        {
            "severity": "hard",
            "standard_id": "auth_bypass",
            "offenders": [{"uid": f"u{i}"} for i in range(2)],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    # 2 offenders * 8 pts = 16 pts penalty → score 84 → B
    assert payload["score"] == 84
    assert payload["grade"] == "B"
    assert payload["breakdown"][0]["penalty"] == 16


def test_health_hard_violation_never_produces_a():
    """Even a single hard offender (score 92) must be capped at B."""
    advisories = [
        {
            "severity": "hard",
            "standard_id": "auth_bypass",
            "offenders": [{"uid": "u1"}],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    # 1 offender * 8 pts = 8 pts penalty → score 92, but hard → capped at B
    assert payload["score"] == 92
    assert payload["grade"] == "B"


def test_health_hard_penalty_capped_per_rule():
    advisories = [
        {
            "severity": "hard",
            "standard_id": "auth_bypass",
            "offenders": [{"uid": f"u{i}"} for i in range(100)],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    # 100 offenders * 8 = 800, but capped at 40 → score 60 → C
    assert payload["breakdown"][0]["penalty"] == _HARD_PENALTY_CAP_PER_RULE
    assert payload["score"] == _MAX_SCORE - _HARD_PENALTY_CAP_PER_RULE


def test_health_warn_penalty_capped_per_rule():
    advisories = [
        {
            "severity": "warn",
            "standard_id": "function_too_long",
            "offenders": [{"uid": f"u{i}"} for i in range(100)],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    assert payload["breakdown"][0]["penalty"] == _WARN_PENALTY_CAP_PER_RULE
    assert payload["score"] == _MAX_SCORE - _WARN_PENALTY_CAP_PER_RULE


def test_health_multiple_rules_accumulate():
    advisories = [
        {
            "severity": "hard",
            "standard_id": "auth_bypass",
            "offenders": [{"uid": "u1"}, {"uid": "u2"}],
        },
        {
            "severity": "warn",
            "standard_id": "function_too_long",
            "offenders": [{"uid": "u3"}, {"uid": "u4"}, {"uid": "u5"}],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    # hard: 2 * 8 = 16, warn: 3 * 2 = 6, total = 22 → score 78 → B
    assert payload["score"] == 78
    assert payload["grade"] == "B"
    assert payload["audit_summary"]["hard"] == 1
    assert payload["audit_summary"]["warn"] == 1
    assert payload["audit_summary"]["total_offenders"] == 5


def test_health_score_floors_at_zero():
    advisories = [
        {
            "severity": "hard",
            "standard_id": f"rule_{i}",
            "offenders": [{"uid": f"u{j}"} for j in range(10)],
        }
        for i in range(5)
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    # 5 rules * 40 cap = 200, but score floors at 0
    assert payload["score"] == 0
    assert payload["grade"] == "F"


def test_health_db_unavailable_returns_f():
    audit = {
        "ok": False,
        "kind": "audit",
        "scope": "all",
        "standards_evaluated": 0,
        "advisories": [],
        "counts": {"warn": 0, "hard": 0},
        "hard_zero": False,
        "error": "Could not connect to KùzuDB",
    }
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    assert payload["ok"] is False
    assert payload["grade"] == "F"
    assert payload["score"] == 0
    assert "error" in payload


def test_health_breakdown_sorted_by_penalty_descending():
    advisories = [
        {
            "severity": "warn",
            "standard_id": "small_issue",
            "offenders": [{"uid": "u1"}],
        },
        {
            "severity": "hard",
            "standard_id": "big_issue",
            "offenders": [{"uid": "u2"}, {"uid": "u3"}, {"uid": "u4"}],
        },
    ]
    audit = _mock_audit_payload(advisories)
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    penalties = [b["penalty"] for b in payload["breakdown"]]
    assert penalties == sorted(penalties, reverse=True)


def test_health_schema_validates():
    """Health output conforms to schemas/health.json."""
    import jsonschema
    schema = json.loads(SCHEMA_PATH.read_text())

    audit = _mock_audit_payload([
        {
            "severity": "warn",
            "standard_id": "test_rule",
            "offenders": [{"uid": "u1"}],
        },
    ])
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    jsonschema.validate(payload, schema)


def test_health_envelope_fields():
    audit = _mock_audit_payload([])
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    assert payload["kind"] == "health"
    assert payload["schema_version"] == "1.0"
    assert "project" in payload


def test_health_project_slug_passed_through():
    """Resolved project slug must appear in the envelope."""
    audit = _mock_audit_payload([])
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload(project="my-app")

    assert payload["project"] == "my-app"


def test_health_project_none_when_omitted():
    """Without --project the envelope project field should be None."""
    audit = _mock_audit_payload([])
    with patch(
        "codegraphcontext_ext.commands.health.build_audit_payload",
        return_value=audit,
    ):
        payload = build_health_payload()

    assert payload["project"] is None
