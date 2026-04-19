"""Standards loader: parse YAML rules, inject exemptions, run Cypher queries.

Each rule file under ``standards/`` defines:
  - ``id``, ``advisory_kind``, ``severity`` (warn | hard)
  - ``query`` — parameterised Cypher with ``$var`` placeholders
  - ``thresholds`` — dict of named threshold values
  - ``suggestion`` — Mustache-style template over RETURN columns
  - ``exemptions`` — reference to shared exemptions file
  - ``evidence`` — documents the graph/embedding proof

The loader resolves thresholds into the query, injects exemption
WHERE clauses, executes against KùzuDB, and collects violations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class StandardRule:
    """A single parsed quality rule."""
    id: str
    advisory_kind: str
    severity: str  # "warn" | "hard"
    summary: str
    query: str
    thresholds: dict[str, int | float] = field(default_factory=dict)
    suggestion: str = ""
    exemptions: str = ""
    evidence: str = ""
    category: str = ""
    detection_method: str = "cypher"  # "cypher" | "embedding"

    @property
    def is_hard(self) -> bool:
        return self.severity == "hard"


@dataclass
class Violation:
    """A single offender found by a rule."""
    uid: str
    name: Optional[str]
    path: Optional[str]
    line_number: Optional[int]
    metric_value: Any = None


@dataclass
class RuleResult:
    """Result of running one rule: the rule definition + violations found."""
    rule: StandardRule
    offenders: list[Violation] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def fired(self) -> bool:
        return len(self.offenders) > 0

    def to_advisory(self) -> dict[str, Any]:
        """Convert to the §6.5 advisory output shape."""
        offender_dicts = []
        for o in self.offenders:
            d: dict[str, Any] = {"uid": o.uid, "name": o.name, "path": o.path}
            if o.line_number is not None:
                d["line_number"] = o.line_number
            if o.metric_value is not None:
                d["metric_value"] = o.metric_value
            offender_dicts.append(d)

        suggestion = self.rule.suggestion
        if offender_dicts and suggestion:
            # Render Mustache-style {{key}} from first offender
            for key, val in offender_dicts[0].items():
                suggestion = suggestion.replace(f"{{{{{key}}}}}", str(val or ""))

        return {
            "kind": self.rule.advisory_kind,
            "severity": self.rule.severity,
            "standard_id": self.rule.id,
            "threshold_applied": self.rule.thresholds,
            "offenders": offender_dicts,
            "suggestion": suggestion,
            "evidence": self.rule.evidence,
        }


# ---------------------------------------------------------------------------
# Exemption loading
# ---------------------------------------------------------------------------

@dataclass
class Exemptions:
    """Parsed _exemptions.yaml."""
    decorators: dict[str, list[str]] = field(default_factory=dict)
    function_name_patterns: dict[str, list[str]] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)


def load_exemptions(standards_dir: Path) -> Exemptions:
    """Load _exemptions.yaml from the standards directory."""
    exemptions_path = standards_dir / "_exemptions.yaml"
    if not exemptions_path.is_file():
        return Exemptions()
    with open(exemptions_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Exemptions(
        decorators=data.get("decorators", {}),
        function_name_patterns=data.get("function_name_patterns", {}),
        paths=data.get("paths", []),
    )


def build_exemption_where(exemptions: Exemptions, node_var: str = "f") -> str:
    """Build a Cypher WHERE fragment that excludes exempted nodes.

    Returns empty string if no exemptions apply.
    """
    clauses: list[str] = []
    for path_pattern in exemptions.paths:
        # Convert glob patterns to Cypher CONTAINS checks.
        # "**/tests/**"    → CONTAINS '/tests/'
        # "**/*_test.*"    → CONTAINS '_test.'
        # "**/__pycache__/**" → CONTAINS '/__pycache__/'
        clean = path_pattern.replace("**/", "").replace("/**", "").replace("*", "")
        if clean:
            clauses.append(f"NOT {node_var}.path CONTAINS '{clean}'")

    # Python decorators — check if node has any exempt decorator
    python_decorators = exemptions.decorators.get("python", [])
    for dec in python_decorators:
        if "*" not in dec:
            clauses.append(
                f"NOT ('{dec}' IN {node_var}.decorators)"
            )

    if not clauses:
        return ""
    return " AND ".join(clauses)


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def load_rules(standards_dir: Path) -> list[StandardRule]:
    """Load all YAML rule files from the standards directory."""
    rules: list[StandardRule] = []
    if not standards_dir.is_dir():
        return rules

    for yaml_path in sorted(standards_dir.glob("*.yaml")):
        if yaml_path.name.startswith("_"):
            continue  # Skip _exemptions.yaml etc.
        try:
            rule = _parse_rule_file(yaml_path)
            rules.append(rule)
        except Exception:
            continue  # Skip malformed rules
    return rules


def _parse_rule_file(path: Path) -> StandardRule:
    """Parse a single YAML rule file into a StandardRule."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return StandardRule(
        id=data["id"],
        advisory_kind=data.get("advisory_kind", data["id"]),
        severity=data.get("severity", "warn"),
        summary=data.get("summary", ""),
        query=data.get("query", ""),
        thresholds=data.get("thresholds", {}),
        suggestion=data.get("suggestion", ""),
        exemptions=data.get("exemptions", ""),
        evidence=data.get("evidence", ""),
        category=data.get("category", ""),
        detection_method=data.get("detection_method", "cypher"),
    )


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def resolve_query(rule: StandardRule, exemptions: Exemptions) -> str:
    """Resolve $var placeholders and inject exemption WHERE clauses."""
    query = rule.query

    # Replace $threshold placeholders
    for key, val in rule.thresholds.items():
        query = query.replace(f"${key}", str(val))

    # Inject exemption clauses if the rule references exemptions
    if rule.exemptions:
        exemption_where = build_exemption_where(exemptions)
        if exemption_where:
            # Insert before RETURN — find last WHERE or AND before RETURN
            query = _inject_exemption_clauses(query, exemption_where)

    return query


def _inject_exemption_clauses(query: str, clauses: str) -> str:
    """Insert exemption AND clauses before the RETURN keyword."""
    # Find RETURN keyword position
    return_match = re.search(r'\bRETURN\b', query, re.IGNORECASE)
    if not return_match:
        return query

    insert_pos = return_match.start()
    # Add AND clauses before RETURN
    return query[:insert_pos] + f"  AND {clauses}\n" + query[insert_pos:]


def _run_embedding_rule(conn: Any, rule: StandardRule) -> RuleResult:
    """Execute a Python-backed embedding rule (detection_method='embedding')."""
    from .naming_rules import EMBEDDING_RULES
    from ..embeddings.schema import NAME_EMBEDDING_COLUMN

    func = EMBEDDING_RULES.get(rule.id)
    if func is None:
        return RuleResult(
            rule=rule,
            error=f"No embedding rule implementation for '{rule.id}'",
        )

    # Probe: are there any name_embedding vectors?
    try:
        probe = conn.execute(
            f"MATCH (f:Function) WHERE f.`{NAME_EMBEDDING_COLUMN}` IS NOT NULL "
            f"RETURN count(f)"
        )
        count = probe.get_next()[0] if probe.has_next() else 0
    except Exception:
        count = 0

    if count == 0:
        return RuleResult(
            rule=rule,
            error="No name_embedding data found. Run 'kkg embed' to generate name embeddings.",
        )

    try:
        raw = func(conn, rule.thresholds)
    except Exception as exc:
        return RuleResult(rule=rule, error=f"{type(exc).__name__}: {exc}")

    offenders = [
        Violation(
            uid=v["uid"],
            name=v.get("name"),
            path=v.get("path"),
            line_number=v.get("line_number"),
            metric_value=v.get("metric_value"),
        )
        for v in raw
    ]
    return RuleResult(rule=rule, offenders=offenders)


def run_rule(
    conn: Any,
    rule: StandardRule,
    exemptions: Exemptions,
) -> RuleResult:
    """Execute a single rule against the DB and collect violations."""
    if rule.detection_method == "embedding":
        return _run_embedding_rule(conn, rule)

    resolved = resolve_query(rule, exemptions)

    try:
        result = conn.execute(resolved)
    except Exception as exc:
        return RuleResult(rule=rule, error=f"{type(exc).__name__}: {exc}")

    offenders: list[Violation] = []
    while result.has_next():
        row = result.get_next()
        # Expected columns: uid, name, path, line_number, [metric]
        offenders.append(Violation(
            uid=str(row[0]) if row[0] else "",
            name=str(row[1]) if len(row) > 1 and row[1] else None,
            path=str(row[2]) if len(row) > 2 and row[2] else None,
            line_number=int(row[3]) if len(row) > 3 and row[3] else None,
            metric_value=row[4] if len(row) > 4 else None,
        ))

    return RuleResult(rule=rule, offenders=offenders)


def run_all_rules(
    conn: Any,
    standards_dir: Path,
    category_filter: Optional[str] = None,
) -> list[RuleResult]:
    """Load and run all rules, returning results."""
    rules = load_rules(standards_dir)
    exemptions = load_exemptions(standards_dir)

    if category_filter:
        rules = [r for r in rules if r.category == category_filter]

    results: list[RuleResult] = []
    for rule in rules:
        result = run_rule(conn, rule, exemptions)
        results.append(result)

    return results
