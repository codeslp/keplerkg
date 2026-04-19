"""Tests for Wave 3 embedding-backed naming analysis rules (CGQ-F01–F04).

Covers: cosine_similarity, _humanize_name, all 4 rule functions,
embedding rule dispatch in the loader, and graceful degradation when
name_embedding data is missing.
"""

from __future__ import annotations

import math

import pytest

from codegraphcontext_ext.hybrid.ann import cosine_similarity
from codegraphcontext_ext.commands.embed import _humanize_name
from codegraphcontext_ext.standards.loader import (
    StandardRule,
    run_rule,
    load_rules,
    Exemptions,
)
from codegraphcontext_ext.standards.naming_rules import (
    EMBEDDING_RULES,
    _misleading_name,
    _inconsistent_naming,
    _suggest_better_name,
    _module_content_mismatch,
    set_provider,
)

from .conftest import FakeResult


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_known_value(self):
        a = [1.0, 2.0, 3.0]
        b = [4.0, 5.0, 6.0]
        dot = 1 * 4 + 2 * 5 + 3 * 6  # 32
        norm_a = math.sqrt(14)
        norm_b = math.sqrt(77)
        expected = dot / (norm_a * norm_b)
        assert cosine_similarity(a, b) == pytest.approx(expected)

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
        assert cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0

    def test_empty_vectors_return_zero(self):
        assert cosine_similarity([], []) == 0.0
        assert cosine_similarity([], [1.0]) == 0.0

    def test_mismatched_lengths_uses_min(self):
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0]
        # Uses min(3, 2) = 2 dims
        assert cosine_similarity(a, b) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _humanize_name
# ---------------------------------------------------------------------------


class TestHumanizeName:
    def test_snake_case(self):
        assert _humanize_name("calculate_total_price") == "calculate total price"

    def test_camel_case(self):
        assert _humanize_name("getUserById") == "get User By Id"

    def test_pascal_case(self):
        assert _humanize_name("DatabaseManager") == "Database Manager"

    def test_single_word(self):
        assert _humanize_name("main") == "main"

    def test_empty_string(self):
        assert _humanize_name("") == ""

    def test_mixed(self):
        assert _humanize_name("get_userByName") == "get user By Name"

    def test_underscores_only(self):
        assert _humanize_name("__init__") == "init"

    def test_acronym(self):
        assert _humanize_name("parseHTMLDocument") == "parse HTML Document"


# ---------------------------------------------------------------------------
# Mock connection for embedding rule tests
# ---------------------------------------------------------------------------


class _DualEmbConn:
    """Mock connection returning rows with both embedding columns."""

    def __init__(self, rows, *, count_result=None):
        self._rows = list(rows)
        self._count = count_result

    def execute(self, query, **_kw):
        if "count(f)" in query.lower():
            return FakeResult([[self._count if self._count is not None else len(self._rows)]])
        return FakeResult(self._rows)


# ---------------------------------------------------------------------------
# F01 — misleading_name
# ---------------------------------------------------------------------------


class TestMisleadingName:
    def test_flags_low_similarity(self):
        # name_vec and behavior_vec are nearly orthogonal
        rows = [
            ("uid1", "foo", "a.py", 1, [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
        ]
        conn = _DualEmbConn(rows)
        violations = _misleading_name(conn, {"warn": 0.15})
        assert len(violations) == 1
        assert violations[0]["uid"] == "uid1"
        assert violations[0]["metric_value"] == pytest.approx(0.0, abs=0.01)

    def test_passes_high_similarity(self):
        rows = [
            ("uid1", "foo", "a.py", 1, [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]),
        ]
        conn = _DualEmbConn(rows)
        violations = _misleading_name(conn, {"warn": 0.15})
        assert len(violations) == 0

    def test_threshold_configurable(self):
        # similarity ~0.5 — below 0.6 threshold but above default 0.15
        rows = [
            ("uid1", "foo", "a.py", 1, [1.0, 1.0, 0.0], [1.0, 0.0, 0.0]),
        ]
        conn = _DualEmbConn(rows)
        assert len(_misleading_name(conn, {"warn": 0.15})) == 0
        assert len(_misleading_name(conn, {"warn": 0.8})) == 1


# ---------------------------------------------------------------------------
# F02 — inconsistent_naming
# ---------------------------------------------------------------------------


class TestInconsistentNaming:
    def test_flags_similar_behavior_dissimilar_names(self):
        # A and B have identical behavior vectors but orthogonal name vectors
        rows = [
            ("uid1", "foo", "a.py", 1, [1.0, 0.0], [1.0, 0.0]),
            ("uid2", "bar", "b.py", 2, [1.0, 0.0], [0.0, 1.0]),
        ]
        conn = _DualEmbConn(rows)
        violations = _inconsistent_naming(conn, {
            "behavior_similarity": 0.9,
            "name_dissimilarity": 0.5,
            "max_nodes": 100,
        })
        assert len(violations) == 1
        assert "bar" in violations[0]["metric_value"]

    def test_no_flag_when_names_also_similar(self):
        rows = [
            ("uid1", "foo", "a.py", 1, [1.0, 0.0], [1.0, 0.0]),
            ("uid2", "bar", "b.py", 2, [1.0, 0.0], [1.0, 0.0]),
        ]
        conn = _DualEmbConn(rows)
        violations = _inconsistent_naming(conn, {
            "behavior_similarity": 0.9,
            "name_dissimilarity": 0.5,
            "max_nodes": 100,
        })
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# F04 — suggest_better_name
# ---------------------------------------------------------------------------


class TestSuggestBetterName:
    def test_suggests_from_well_named_neighbors(self):
        # uid1: poorly named (name orthogonal to behavior)
        # uid2: well named (name == behavior) and similar behavior to uid1
        rows = [
            ("uid1", "bad_name", "a.py", 1, [1.0, 0.0], [0.0, 1.0]),
            ("uid2", "good_name", "b.py", 2, [1.0, 0.0], [1.0, 0.0]),
        ]
        conn = _DualEmbConn(rows)
        violations = _suggest_better_name(conn, {
            "self_low": 0.4,
            "exemplar_high": 0.7,
            "max_exemplars": 3,
        })
        assert len(violations) == 1
        assert violations[0]["uid"] == "uid1"
        assert "good_name" in violations[0]["metric_value"]

    def test_no_suggestions_when_all_well_named(self):
        rows = [
            ("uid1", "good", "a.py", 1, [1.0, 0.0], [1.0, 0.0]),
            ("uid2", "also_good", "b.py", 2, [0.0, 1.0], [0.0, 1.0]),
        ]
        conn = _DualEmbConn(rows)
        violations = _suggest_better_name(conn, {
            "self_low": 0.4,
            "exemplar_high": 0.7,
        })
        assert len(violations) == 0


# ---------------------------------------------------------------------------
# F03 — module_content_mismatch
# ---------------------------------------------------------------------------


class _F03Conn:
    """Mock connection for module_content_mismatch test."""

    def __init__(self, rows, count=1):
        self._rows = rows
        self._count = count

    def execute(self, query, **_kw):
        if "count(f)" in query.lower():
            return FakeResult([[self._count]])
        return FakeResult(self._rows)


class _FakeProvider:
    def __init__(self, return_vecs):
        self._vecs = return_vecs

    def embed_texts(self, texts):
        return self._vecs[:len(texts)]


class TestModuleContentMismatch:
    def test_flags_mismatched_file(self):
        # Two functions in same file with behavior pointing in one direction
        rows = [
            ("src/math_utils.py", [0.0, 1.0, 0.0]),
            ("src/math_utils.py", [0.0, 1.0, 0.0]),
        ]
        conn = _F03Conn(rows)
        # Provider returns a file-name embedding orthogonal to function behavior
        provider = _FakeProvider([[1.0, 0.0, 0.0]])
        set_provider(provider)
        try:
            violations = _module_content_mismatch(conn, {"warn": 0.3})
            assert len(violations) == 1
            assert violations[0]["path"] == "src/math_utils.py"
        finally:
            set_provider(None)

    def test_no_flag_when_aligned(self):
        rows = [
            ("src/math_utils.py", [1.0, 0.0, 0.0]),
        ]
        conn = _F03Conn(rows)
        provider = _FakeProvider([[1.0, 0.0, 0.0]])
        set_provider(provider)
        try:
            violations = _module_content_mismatch(conn, {"warn": 0.3})
            assert len(violations) == 0
        finally:
            set_provider(None)

    def test_no_provider_returns_empty(self):
        set_provider(None)
        conn = _F03Conn([("a.py", [1.0, 0.0])])
        violations = _module_content_mismatch(conn, {"warn": 0.3})
        assert violations == []


# ---------------------------------------------------------------------------
# Loader dispatch: detection_method=embedding
# ---------------------------------------------------------------------------


class TestEmbeddingRuleDispatch:
    def test_loader_parses_detection_method(self, tmp_path):
        rule_yaml = tmp_path / "test_rule.yaml"
        rule_yaml.write_text(
            "id: misleading_name\n"
            "advisory_kind: misleading_name\n"
            "severity: warn\n"
            "summary: test\n"
            "detection_method: embedding\n"
            "category: naming\n"
            "query: ''\n"
        )
        exemptions_yaml = tmp_path / "_exemptions.yaml"
        exemptions_yaml.write_text("paths: []\n")
        rules = load_rules(tmp_path)
        assert len(rules) == 1
        assert rules[0].detection_method == "embedding"

    def test_cypher_rule_defaults_detection_method(self, tmp_path):
        rule_yaml = tmp_path / "test_rule.yaml"
        rule_yaml.write_text(
            "id: test\n"
            "advisory_kind: test\n"
            "severity: warn\n"
            "summary: test\n"
            "query: 'RETURN 1'\n"
        )
        exemptions_yaml = tmp_path / "_exemptions.yaml"
        exemptions_yaml.write_text("paths: []\n")
        rules = load_rules(tmp_path)
        assert rules[0].detection_method == "cypher"

    def test_run_rule_dispatches_to_embedding(self):
        """run_rule delegates to _run_embedding_rule when detection_method='embedding'."""
        rule = StandardRule(
            id="misleading_name",
            advisory_kind="misleading_name",
            severity="warn",
            summary="test",
            query="",
            detection_method="embedding",
            thresholds={"warn": 0.15},
        )
        # Connection with one node and name_embedding data
        rows = [
            ("uid1", "foo", "a.py", 1, [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
        ]
        conn = _DualEmbConn(rows, count_result=1)
        result = run_rule(conn, rule, Exemptions())
        assert result.error is None
        assert result.fired
        assert result.offenders[0].uid == "uid1"

    def test_embedding_rule_no_data_returns_error(self):
        """When name_embedding column has no data, return informative error."""
        rule = StandardRule(
            id="misleading_name",
            advisory_kind="misleading_name",
            severity="warn",
            summary="test",
            query="",
            detection_method="embedding",
        )
        conn = _DualEmbConn([], count_result=0)
        result = run_rule(conn, rule, Exemptions())
        assert result.error is not None
        assert "kkg embed" in result.error
        assert not result.fired

    def test_embedding_rule_unknown_id_returns_error(self):
        """Unknown rule id in the registry returns error, not crash."""
        rule = StandardRule(
            id="nonexistent_rule",
            advisory_kind="nonexistent",
            severity="warn",
            summary="test",
            query="",
            detection_method="embedding",
        )
        conn = _DualEmbConn([], count_result=5)
        result = run_rule(conn, rule, Exemptions())
        assert result.error is not None
        assert "nonexistent_rule" in result.error


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


def test_all_four_rules_registered():
    expected = {"misleading_name", "inconsistent_naming", "module_content_mismatch", "suggest_better_name"}
    assert expected == set(EMBEDDING_RULES.keys())
