"""Tests for the Phase 5 kkg audit command."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
from typer.testing import CliRunner

from codegraphcontext_ext.commands.audit import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    build_audit_payload,
    build_calibration_payload,
    build_list_payload,
    build_explain_payload,
    _percentile,
    _compute_distribution,
    _detect_comparison_op,
    _count_violations,
)
from codegraphcontext_ext.io.schema_check import schema_path

runner = CliRunner()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_command_metadata():
    assert COMMAND_NAME == "audit"
    assert SCHEMA_FILE == "audit.json"
    assert isinstance(SUMMARY, str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_standards_dir(tmp_path):
    """Create a standards dir with one test rule."""
    std = tmp_path / "standards"
    std.mkdir()
    (std / "_exemptions.yaml").write_text("paths: []\n")
    (std / "test_rule.yaml").write_text(dedent("""\
        id: test_rule
        advisory_kind: test_kind
        severity: warn
        category: testing
        summary: Test rule
        query: "MATCH (f:Function) WHERE f.x > 5 RETURN f.uid, f.name, f.path, f.line_number, f.x AS metric"
        thresholds:
          warn: 5
        suggestion: "Fix {{name}}"
        evidence: "Test evidence"
    """))
    (std / "hard_rule.yaml").write_text(dedent("""\
        id: hard_rule
        advisory_kind: hard_kind
        severity: hard
        category: testing
        summary: Hard test rule
        query: "MATCH (f:Function) WHERE f.bad = true RETURN f.uid, f.name, f.path, f.line_number"
        evidence: "Hard evidence"
    """))
    return std


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
    def __init__(self, rows_by_query=None):
        self._rows_by_query = rows_by_query or {}

    def execute(self, query):
        for key, rows in self._rows_by_query.items():
            if key in query:
                return _FakeResult(rows)
        return _FakeResult([])


# ---------------------------------------------------------------------------
# build_audit_payload
# ---------------------------------------------------------------------------

def test_audit_no_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn()
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["ok"] is True
    assert result["kind"] == "audit"
    assert result["standards_evaluated"] == 2
    assert result["advisories"] == []
    assert result["hard_zero"] is True


def test_audit_with_warn_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [("uid1", "bad_func", "src/a.py", 10, 25)],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["ok"] is True
    assert result["counts"]["warn"] == 1
    assert result["counts"]["hard"] == 0
    assert result["hard_zero"] is True


def test_audit_with_hard_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.bad = true": [("uid2", "bad_class", "src/b.py", 5)],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["counts"]["hard"] == 1
    assert result["hard_zero"] is False


def test_audit_db_unavailable(tmp_path):
    std = _make_standards_dir(tmp_path)
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_audit_payload(standards_dir=std)
    assert result["ok"] is False
    assert "error" in result


def test_audit_category_filter(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn()
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std, category="nonexistent")
    assert result["standards_evaluated"] == 0


def test_audit_explicit_files_override_scope_resolution(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [
            ("uid1", "scoped_func", "src/a.py", 10, 25),
            ("uid2", "other_func", "src/b.py", 20, 30),
        ],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ), patch(
        "codegraphcontext_ext.commands.audit._resolve_scope_files",
        return_value={"src/b.py"},
    ):
        result = build_audit_payload(
            standards_dir=std,
            scope="lane",
            files=["src/a.py"],
        )

    assert result["scope"] == "lane"
    assert result["scope_source"] == "explicit_files"
    assert result["files_requested"] == 1
    assert result["counts"]["warn"] == 1
    assert [o["path"] for o in result["advisories"][0]["offenders"]] == ["src/a.py"]


def test_audit_explicit_directory_scope_filters_descendants(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [
            ("uid1", "scoped_func", "/repo/src/a.py", 10, 25),
            ("uid2", "other_func", "/repo/pkg/b.py", 20, 30),
        ],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(
            standards_dir=std,
            files=["src/"],
        )

    assert result["scope_source"] == "explicit_files"
    assert result["counts"]["warn"] == 1
    assert [o["path"] for o in result["advisories"][0]["offenders"]] == ["/repo/src/a.py"]


# ---------------------------------------------------------------------------
# build_list_payload
# ---------------------------------------------------------------------------

def test_list_payload(tmp_path):
    std = _make_standards_dir(tmp_path)
    result = build_list_payload(standards_dir=std)
    assert result["kind"] == "audit_list"
    assert len(result["standards"]) == 2
    ids = {s["id"] for s in result["standards"]}
    assert "test_rule" in ids
    assert "hard_rule" in ids


# ---------------------------------------------------------------------------
# build_explain_payload
# ---------------------------------------------------------------------------

def test_explain_found(tmp_path):
    std = _make_standards_dir(tmp_path)
    result = build_explain_payload("test_rule", standards_dir=std)
    assert result["kind"] == "audit_explain"
    assert result["id"] == "test_rule"
    assert result["severity"] == "warn"
    assert result["evidence"] == "Test evidence"


def test_explain_not_found(tmp_path):
    std = _make_standards_dir(tmp_path)
    result = build_explain_payload("nonexistent", standards_dir=std)
    assert "error" in result


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _load_schema():
    return json.loads(schema_path("audit.json").read_text())


def test_schema_validation_clean(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn()
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    jsonschema.validate(result, _load_schema())


def test_schema_validation_with_violations(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [("uid1", "fn", "a.py", 1, 20)],
        "f.bad = true": [("uid2", "cls", "b.py", 5)],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std)
    jsonschema.validate(result, _load_schema())


def test_schema_validation_with_explicit_files_scope(tmp_path):
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [
            ("uid1", "scoped_func", "src/a.py", 1, 20),
            ("uid2", "other_func", "src/b.py", 2, 25),
        ],
    })
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_audit_payload(standards_dir=std, files=["src/a.py"])
    jsonschema.validate(result, _load_schema())


# ---------------------------------------------------------------------------
# Calibration report — percentile math
# ---------------------------------------------------------------------------

def test_percentile_basic():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(vals, 0) == 1.0
    assert _percentile(vals, 50) == 3.0
    assert _percentile(vals, 100) == 5.0


def test_percentile_interpolation():
    vals = [10.0, 20.0, 30.0, 40.0]
    # p50: k = 0.5 * 3 = 1.5 → 20*(2-1.5) + 30*(1.5-1) = 10+15 = 25
    assert _percentile(vals, 50) == 25.0


def test_percentile_single_value():
    assert _percentile([42.0], 50) == 42.0
    assert _percentile([42.0], 99) == 42.0


def test_percentile_empty():
    assert _percentile([], 50) == 0.0


def test_compute_distribution():
    vals = [1.0, 5.0, 10.0, 20.0, 50.0, 80.0, 90.0, 95.0, 99.0, 100.0]
    dist = _compute_distribution(vals)
    assert dist["min"] == 1.0
    assert dist["max"] == 100.0
    assert "p50" in dist
    assert "p90" in dist
    assert "p99" in dist
    # p50 of 10 values: k = 0.5*9 = 4.5 → between 50 and 80
    assert 50.0 <= dist["p50"] <= 80.0


def test_compute_distribution_empty():
    assert _compute_distribution([]) == {}


# ---------------------------------------------------------------------------
# Calibration report — build_calibration_payload
# ---------------------------------------------------------------------------

def _make_calibration_standards(tmp_path):
    """Standards dir with threshold and non-threshold rules."""
    std = tmp_path / "standards"
    std.mkdir()
    (std / "_exemptions.yaml").write_text("paths: []\n")
    (std / "threshold_rule.yaml").write_text(dedent("""\
        id: threshold_rule
        advisory_kind: threshold_kind
        severity: warn
        category: complexity
        summary: Threshold rule
        query: |
          MATCH (f:Function)
          WHERE f.line_count > $warn
          RETURN f.uid, f.name, f.path, f.line_number,
                 f.line_count AS metric
        thresholds:
          warn: 50
          hard: 100
        suggestion: "Fix {{name}}"
        evidence: "Test evidence"
    """))
    # Hard rule without thresholds — should be excluded from calibration
    (std / "no_threshold.yaml").write_text(dedent("""\
        id: no_threshold
        advisory_kind: hard_kind
        severity: hard
        category: compliance
        summary: No threshold rule
        query: "MATCH (f:Function) WHERE f.bad = true RETURN f.uid, f.name, f.path, f.line_number"
        evidence: "Hard evidence"
    """))
    return std


class _CalibrationConn:
    """Fake connection that returns metric values for calibration queries."""
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query):
        # When threshold is zeroed ($warn=0), return all rows
        if "> 0" in query or ">= 0" in query:
            return _FakeResult(self._rows)
        return _FakeResult([])


def test_calibration_returns_distribution(tmp_path):
    std = _make_calibration_standards(tmp_path)
    rows = [
        ("uid1", "fn_a", "a.py", 1, 10),
        ("uid2", "fn_b", "b.py", 2, 30),
        ("uid3", "fn_c", "c.py", 3, 55),
        ("uid4", "fn_d", "d.py", 4, 80),
        ("uid5", "fn_e", "e.py", 5, 120),
    ]
    conn = _CalibrationConn(rows)
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_calibration_payload(standards_dir=std)

    assert result["ok"] is True
    assert result["kind"] == "audit_calibration"
    assert result["rules_analyzed"] == 1  # Only threshold_rule
    assert len(result["rules"]) == 1

    r = result["rules"][0]
    assert r["id"] == "threshold_rule"
    assert r["population"] == 5
    assert r["current_thresholds"] == {"warn": 50, "hard": 100}

    # Violations at current: metric > 50 → uid3(55), uid4(80), uid5(120) = 3
    assert r["violations_at_current"]["warn"] == 3
    # metric > 100 → uid5(120) = 1
    assert r["violations_at_current"]["hard"] == 1

    dist = r["distribution"]
    assert dist["min"] == 10.0
    assert dist["max"] == 120.0
    assert "p50" in dist
    assert "p90" in dist

    # Candidate thresholds should be present
    assert len(r["candidate_thresholds"]) > 0
    for c in r["candidate_thresholds"]:
        assert "percentile" in c
        assert "value" in c
        assert "violations_above" in c


def test_calibration_excludes_non_threshold_rules(tmp_path):
    std = _make_calibration_standards(tmp_path)
    conn = _CalibrationConn([("uid1", "fn", "a.py", 1, 25)])
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_calibration_payload(standards_dir=std)

    rule_ids = [r["id"] for r in result["rules"]]
    assert "threshold_rule" in rule_ids
    assert "no_threshold" not in rule_ids


def test_calibration_category_filter(tmp_path):
    std = _make_calibration_standards(tmp_path)
    conn = _CalibrationConn([])
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_calibration_payload(standards_dir=std, category="nonexistent")

    assert result["rules_analyzed"] == 0
    assert result["rules"] == []


def test_calibration_db_unavailable(tmp_path):
    std = _make_calibration_standards(tmp_path)
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        side_effect=Exception("no db"),
    ):
        result = build_calibration_payload(standards_dir=std)

    assert result["ok"] is False
    assert "error" in result


def test_calibration_empty_population(tmp_path):
    std = _make_calibration_standards(tmp_path)
    conn = _CalibrationConn([])  # No rows returned
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_calibration_payload(standards_dir=std)

    r = result["rules"][0]
    assert r["population"] == 0
    assert r["distribution"] == {}
    assert r["candidate_thresholds"] == []


def test_calibration_schema_validation(tmp_path):
    std = _make_calibration_standards(tmp_path)
    rows = [
        ("uid1", "fn_a", "a.py", 1, 10),
        ("uid2", "fn_b", "b.py", 2, 60),
    ]
    conn = _CalibrationConn(rows)
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_calibration_payload(standards_dir=std)

    jsonschema.validate(result, _load_schema())


# ---------------------------------------------------------------------------
# Calibration — comparison operator detection
# ---------------------------------------------------------------------------

def test_detect_comparison_op_gt():
    query = "WHERE fan_out > $warn RETURN f.uid"
    assert _detect_comparison_op(query, "warn") == ">"


def test_detect_comparison_op_gte():
    query = "WHERE max_depth >= $warn RETURN leaf.uid"
    assert _detect_comparison_op(query, "warn") == ">="


def test_detect_comparison_op_defaults_to_gt():
    query = "MATCH (f) RETURN f.uid"
    assert _detect_comparison_op(query, "warn") == ">"


def test_count_violations_gt():
    metrics = [3.0, 5.0, 5.0, 7.0]
    assert _count_violations(metrics, 5, ">") == 1   # only 7 > 5
    assert _count_violations(metrics, 5, ">=") == 3   # 5, 5, 7 >= 5


# ---------------------------------------------------------------------------
# Calibration — >= deep_inheritance regression test
# ---------------------------------------------------------------------------

def _make_gte_standards(tmp_path):
    """Standards dir with a >= threshold rule (deep_inheritance style)."""
    std = tmp_path / "standards"
    std.mkdir()
    (std / "_exemptions.yaml").write_text("paths: []\n")
    (std / "gte_rule.yaml").write_text(dedent("""\
        id: gte_rule
        advisory_kind: gte_kind
        severity: warn
        category: inheritance
        summary: GTE threshold rule
        query: |
          MATCH (leaf:Class)
          WITH leaf, leaf.depth AS max_depth
          WHERE max_depth >= $warn
          RETURN leaf.uid, leaf.name, leaf.path, leaf.line_number,
                 max_depth AS metric
        thresholds:
          warn: 4
          hard: 6
        evidence: "Test evidence"
    """))
    return std


def test_calibration_gte_rule_counts_correctly(tmp_path):
    """Regression: >= rules must count violations with >= not >."""
    std = _make_gte_standards(tmp_path)
    rows = [
        ("uid1", "cls_a", "a.py", 1, 3),
        ("uid2", "cls_b", "b.py", 2, 4),   # exactly at warn threshold
        ("uid3", "cls_c", "c.py", 3, 5),
        ("uid4", "cls_d", "d.py", 4, 6),   # exactly at hard threshold
        ("uid5", "cls_e", "e.py", 5, 8),
    ]
    conn = _CalibrationConn(rows)
    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = build_calibration_payload(standards_dir=std)

    r = result["rules"][0]
    assert r["id"] == "gte_rule"
    # >= 4: uid2(4), uid3(5), uid4(6), uid5(8) = 4
    assert r["violations_at_current"]["warn"] == 4
    # >= 6: uid4(6), uid5(8) = 2
    assert r["violations_at_current"]["hard"] == 2


# ---------------------------------------------------------------------------
# Calibration — severity overrides + hard_stop (strict profile)
# ---------------------------------------------------------------------------

def _make_severity_override_standards(tmp_path):
    """Standards dir with a warn rule that can be promoted to hard."""
    std = tmp_path / "standards"
    std.mkdir()
    (std / "_exemptions.yaml").write_text("paths: []\n")
    (std / "promotable_rule.yaml").write_text(dedent("""\
        id: promotable_rule
        advisory_kind: promotable_kind
        severity: warn
        category: complexity
        summary: Promotable rule
        query: |
          MATCH (f:Function)
          WHERE f.method_count > $warn
          RETURN f.uid, f.name, f.path, f.line_number,
                 f.method_count AS metric
        thresholds:
          warn: 20
          hard: 40
        evidence: "Test evidence"
    """))
    return std


def test_calibration_severity_override_hard(tmp_path):
    """Rules promoted to hard via overrides should show severity=hard in calibration."""
    std = _make_severity_override_standards(tmp_path)
    conn = _CalibrationConn([("uid1", "cls_a", "a.py", 1, 25)])

    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ), patch(
        "codegraphcontext_ext.commands.audit.resolve_cgraph_config",
    ) as mock_cfg:
        from codegraphcontext_ext.config import CgraphConfig, StandardsConfig
        scfg = StandardsConfig()
        scfg.overrides = {"promotable_rule": "hard"}
        mock_cfg.return_value = CgraphConfig(standards=scfg)

        result = build_calibration_payload(standards_dir=std)

    r = result["rules"][0]
    assert r["severity"] == "hard"


def test_calibration_hard_stop_promotes_severity(tmp_path):
    """Rules in hard_stop list should be promoted to hard in calibration."""
    std = _make_severity_override_standards(tmp_path)
    conn = _CalibrationConn([("uid1", "cls_a", "a.py", 1, 25)])

    with patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ), patch(
        "codegraphcontext_ext.commands.audit.resolve_cgraph_config",
    ) as mock_cfg:
        from codegraphcontext_ext.config import CgraphConfig, StandardsConfig
        scfg = StandardsConfig()
        scfg.hard_stop = ["promotable_rule"]
        mock_cfg.return_value = CgraphConfig(standards=scfg)

        result = build_calibration_payload(standards_dir=std)

    r = result["rules"][0]
    assert r["severity"] == "hard"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_list():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)

    with patch(
        "codegraphcontext_ext.commands.audit._find_standards_dir",
        return_value=Path(__file__).parent.parent.parent / "standards",
    ):
        result = runner.invoke(app, ["audit", "--list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["kind"] == "audit_list"


def test_audit_registered():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)
    names = [cmd.name for cmd in app.registered_commands]
    assert "audit" in names


def test_cli_audit_files_flag_filters_output(tmp_path):
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)
    std = _make_standards_dir(tmp_path)
    conn = _FakeConn(rows_by_query={
        "f.x > 5": [
            ("uid1", "scoped_func", "src/a.py", 10, 25),
            ("uid2", "other_func", "src/b.py", 20, 30),
        ],
    })

    with patch(
        "codegraphcontext_ext.commands.audit._find_standards_dir",
        return_value=std,
    ), patch(
        "codegraphcontext_ext.commands.audit.get_kuzu_connection",
        return_value=conn,
    ):
        result = runner.invoke(app, ["audit", "--files", "src/a.py"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["scope_source"] == "explicit_files"
    assert payload["files_requested"] == 1
    assert [o["path"] for o in payload["advisories"][0]["offenders"]] == ["src/a.py"]


def test_cli_audit_rejects_empty_files():
    from codegraphcontext_ext.cli import register_extensions
    import typer

    app = typer.Typer()
    register_extensions(app)
    result = runner.invoke(app, ["audit", "--files", "  "])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["kind"] == "no_files"
