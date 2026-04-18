"""Tests for the Phase 5 standards loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from codegraphcontext_ext.standards.loader import (
    Exemptions,
    StandardRule,
    RuleResult,
    Violation,
    build_exemption_where,
    load_exemptions,
    load_rules,
    resolve_query,
    run_rule,
    _inject_exemption_clauses,
)


# ---------------------------------------------------------------------------
# StandardRule
# ---------------------------------------------------------------------------

def test_rule_is_hard():
    rule = StandardRule(id="test", advisory_kind="test", severity="hard", summary="", query="")
    assert rule.is_hard is True

    rule2 = StandardRule(id="test2", advisory_kind="test2", severity="warn", summary="", query="")
    assert rule2.is_hard is False


# ---------------------------------------------------------------------------
# RuleResult.to_advisory
# ---------------------------------------------------------------------------

def test_to_advisory_with_offenders():
    rule = StandardRule(
        id="func_complexity",
        advisory_kind="high_complexity",
        severity="warn",
        summary="Too complex",
        query="",
        thresholds={"warn": 10, "hard": 15},
        suggestion="Split {{name}} ({{metric_value}} branches)",
        evidence="Function.cyclomatic_complexity",
    )
    result = RuleResult(
        rule=rule,
        offenders=[
            Violation(uid="uid1", name="parse_query", path="src/a.py", line_number=42, metric_value=14),
        ],
    )
    adv = result.to_advisory()
    assert adv["kind"] == "high_complexity"
    assert adv["severity"] == "warn"
    assert adv["standard_id"] == "func_complexity"
    assert adv["threshold_applied"] == {"warn": 10, "hard": 15}
    assert len(adv["offenders"]) == 1
    assert adv["offenders"][0]["name"] == "parse_query"
    assert "parse_query" in adv["suggestion"]
    assert "14" in adv["suggestion"]


def test_to_advisory_empty():
    rule = StandardRule(id="test", advisory_kind="test", severity="warn", summary="", query="")
    result = RuleResult(rule=rule)
    assert result.fired is False
    adv = result.to_advisory()
    assert adv["offenders"] == []


# ---------------------------------------------------------------------------
# Exemptions
# ---------------------------------------------------------------------------

def test_load_exemptions(tmp_path):
    exemptions_yaml = tmp_path / "_exemptions.yaml"
    exemptions_yaml.write_text(dedent("""\
        decorators:
          python: [pytest.fixture, dataclass]
        paths:
          - "**/tests/**"
          - "**/vendor/**"
    """))
    ex = load_exemptions(tmp_path)
    assert "python" in ex.decorators
    assert len(ex.paths) == 2


def test_load_exemptions_missing(tmp_path):
    ex = load_exemptions(tmp_path)
    assert ex.paths == []
    assert ex.decorators == {}


def test_build_exemption_where():
    ex = Exemptions(
        decorators={"python": ["pytest.fixture", "dataclass"]},
        paths=["**/tests/**", "**/vendor/**"],
    )
    where = build_exemption_where(ex, "f")
    assert "tests" in where
    assert "vendor" in where
    assert "pytest.fixture" in where


def test_build_exemption_where_empty():
    ex = Exemptions()
    assert build_exemption_where(ex) == ""


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def test_load_rules(tmp_path):
    rule_file = tmp_path / "test_rule.yaml"
    rule_file.write_text(dedent("""\
        id: test_rule
        advisory_kind: test_kind
        severity: warn
        summary: A test rule
        category: testing
        query: "MATCH (f:Function) RETURN f.uid, f.name, f.path, f.line_number"
        thresholds:
          warn: 5
        suggestion: "Fix {{name}}"
        evidence: "Test evidence"
    """))
    # Also add an exemptions file (should be skipped)
    (tmp_path / "_exemptions.yaml").write_text("paths: []")

    rules = load_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0].id == "test_rule"
    assert rules[0].severity == "warn"
    assert rules[0].thresholds == {"warn": 5}


def test_load_rules_empty_dir(tmp_path):
    rules = load_rules(tmp_path)
    assert rules == []


def test_load_rules_nonexistent_dir():
    rules = load_rules(Path("/nonexistent"))
    assert rules == []


# ---------------------------------------------------------------------------
# Query resolution
# ---------------------------------------------------------------------------

def test_resolve_query_replaces_thresholds():
    rule = StandardRule(
        id="test", advisory_kind="test", severity="warn", summary="",
        query="MATCH (f:Function) WHERE f.complexity > $warn RETURN f.uid",
        thresholds={"warn": 10},
    )
    resolved = resolve_query(rule, Exemptions())
    assert "$warn" not in resolved
    assert "10" in resolved


def test_resolve_query_injects_exemptions():
    rule = StandardRule(
        id="test", advisory_kind="test", severity="warn", summary="",
        query="MATCH (f:Function)\nWHERE f.x > 5\nRETURN f.uid",
        exemptions="_exemptions.yaml",
    )
    ex = Exemptions(paths=["**/tests/**"])
    resolved = resolve_query(rule, ex)
    assert "tests" in resolved


def test_inject_exemption_clauses():
    query = "MATCH (f:Function)\nWHERE f.x > 5\nRETURN f.uid"
    result = _inject_exemption_clauses(query, "NOT f.path CONTAINS '/tests/'")
    assert "NOT f.path CONTAINS '/tests/'" in result
    assert "RETURN" in result


# ---------------------------------------------------------------------------
# run_rule with fake DB
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def has_next(self):
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


class _FakeConn:
    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    def execute(self, query):
        if self._error:
            raise self._error
        return _FakeResult(self._rows)


def test_run_rule_finds_violations():
    rule = StandardRule(
        id="test", advisory_kind="test_kind", severity="warn", summary="",
        query="MATCH (f:Function) RETURN f.uid, f.name, f.path, f.line_number, f.metric",
    )
    conn = _FakeConn(rows=[
        ("uid1", "bad_func", "src/a.py", 10, 25),
        ("uid2", "worse_func", "src/b.py", 20, 30),
    ])
    result = run_rule(conn, rule, Exemptions())
    assert result.fired is True
    assert len(result.offenders) == 2
    assert result.offenders[0].uid == "uid1"
    assert result.offenders[0].metric_value == 25


def test_run_rule_no_violations():
    rule = StandardRule(
        id="test", advisory_kind="test_kind", severity="warn", summary="",
        query="MATCH (f:Function) RETURN f.uid, f.name, f.path, f.line_number",
    )
    conn = _FakeConn(rows=[])
    result = run_rule(conn, rule, Exemptions())
    assert result.fired is False
    assert result.error is None


def test_run_rule_db_error():
    rule = StandardRule(
        id="test", advisory_kind="test_kind", severity="warn", summary="",
        query="INVALID QUERY",
    )
    conn = _FakeConn(error=RuntimeError("query failed"))
    result = run_rule(conn, rule, Exemptions())
    assert result.fired is False
    assert result.error is not None
    assert "query failed" in result.error
