"""Shared framework resolver for decorator-based entry-point classification.

Phase 5.8: centralizes decorator → framework mapping so ``kkg entrypoints``,
``kkg impact``, audit rules (H01/H02), and topology discovery all use the
same classification logic.

Each framework definition includes:
- ``name``: canonical identifier (e.g. "flask", "fastapi")
- ``category``: entry-point type ("http", "cli", "worker", "test", "graphql")
- ``leaf_patterns``: decorator leaf names that identify the framework
- ``contains_patterns``: substring patterns for Cypher WHERE clauses
- ``base_score``: heuristic weight for entry-point ranking
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FrameworkDef:
    """One framework's entry-point detection definition."""

    name: str
    category: str
    leaf_patterns: frozenset[str]
    contains_patterns: frozenset[str]
    base_score: float


# ── Framework registry ────────────────────────────────────────────────

_FRAMEWORKS: list[FrameworkDef] = [
    # HTTP frameworks
    FrameworkDef(
        name="flask",
        category="http",
        leaf_patterns=frozenset({"route", "before_request", "after_request", "errorhandler"}),
        contains_patterns=frozenset({"route", "before_request", "after_request", "errorhandler"}),
        base_score=5.0,
    ),
    FrameworkDef(
        name="fastapi",
        category="http",
        leaf_patterns=frozenset({
            "api_route", "delete", "get", "head", "options",
            "patch", "post", "put", "websocket",
        }),
        contains_patterns=frozenset({
            "api_route", "delete", "get", "head", "options",
            "patch", "post", "put", "websocket",
        }),
        base_score=5.0,
    ),
    FrameworkDef(
        name="django",
        category="http",
        leaf_patterns=frozenset({
            "api_view", "action", "permission_classes",
            "authentication_classes", "renderer_classes",
        }),
        contains_patterns=frozenset({"api_view", "action"}),
        base_score=5.0,
    ),
    # GraphQL
    FrameworkDef(
        name="graphql",
        category="graphql",
        leaf_patterns=frozenset({
            "query", "mutation", "subscription", "resolver", "type",
        }),
        contains_patterns=frozenset({"query", "mutation", "subscription", "resolver"}),
        base_score=4.5,
    ),
    # CLI frameworks
    FrameworkDef(
        name="click",
        category="cli",
        leaf_patterns=frozenset({"command", "group", "callback"}),
        contains_patterns=frozenset({"command", "group", "callback"}),
        base_score=4.0,
    ),
    FrameworkDef(
        name="typer",
        category="cli",
        leaf_patterns=frozenset({"command", "callback"}),
        contains_patterns=frozenset({"command", "callback"}),
        base_score=4.0,
    ),
    # Worker / async job frameworks
    FrameworkDef(
        name="celery",
        category="worker",
        leaf_patterns=frozenset({"task", "shared_task"}),
        contains_patterns=frozenset({"task", "shared_task"}),
        base_score=4.0,
    ),
    # Test frameworks
    FrameworkDef(
        name="pytest",
        category="test",
        leaf_patterns=frozenset({"fixture", "parametrize"}),
        contains_patterns=frozenset({"fixture"}),
        base_score=3.0,
    ),
]

# Leaf → framework index for fast lookup
_LEAF_INDEX: dict[str, FrameworkDef] = {}
for _fw in _FRAMEWORKS:
    for _leaf in _fw.leaf_patterns:
        # First match wins — order matters in _FRAMEWORKS
        if _leaf not in _LEAF_INDEX:
            _LEAF_INDEX[_leaf] = _fw


# ── Public API ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrameworkMatch:
    """Result of classifying a set of decorators."""

    framework: str
    category: str
    base_score: float
    matched_decorators: tuple[str, ...]


def normalize_decorator(raw: Any) -> str:
    """Strip ``@`` prefix and parenthesized arguments from a raw decorator string."""
    text = str(raw or "").strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    return text


def classify_decorator(normalized: str) -> FrameworkDef | None:
    """Classify a single normalized decorator by its leaf name."""
    if not normalized:
        return None
    leaf = normalized.rsplit(".", 1)[-1]
    return _LEAF_INDEX.get(leaf)


def classify_decorators(decorators: list[str] | None) -> FrameworkMatch | None:
    """Classify a list of raw decorator strings, returning the best match.

    Returns None if no decorator matches any known framework.
    """
    if not decorators:
        return None

    matched: list[str] = []
    seen: set[str] = set()
    best_fw: FrameworkDef | None = None

    for raw in decorators:
        normalized = normalize_decorator(raw)
        fw = classify_decorator(normalized)
        if fw is None:
            continue
        if best_fw is None or fw.base_score > best_fw.base_score:
            best_fw = fw
        if normalized not in seen:
            seen.add(normalized)
            matched.append(normalized)

    if best_fw is None:
        return None

    return FrameworkMatch(
        framework=best_fw.name,
        category=best_fw.category,
        base_score=best_fw.base_score,
        matched_decorators=tuple(matched),
    )


def get_frameworks() -> list[FrameworkDef]:
    """Return a copy of the framework registry."""
    return list(_FRAMEWORKS)


def get_http_frameworks() -> list[FrameworkDef]:
    """Return frameworks in the ``http`` category (for audit rules)."""
    return [fw for fw in _FRAMEWORKS if fw.category == "http"]


def build_handler_decorator_clause(
    node_var: str = "handler",
    *,
    categories: tuple[str, ...] | None = None,
) -> str:
    """Build a Cypher WHERE clause that matches entry-point decorators.

    Returns a string like::

        ANY(d IN handler.decorators WHERE d CONTAINS 'route' OR d CONTAINS 'get' ...)

    If *categories* is given, only includes patterns from frameworks in those
    categories (e.g. ``("http",)`` for audit rules H01/H02).  Otherwise
    includes all frameworks.
    """
    patterns: set[str] = set()
    for fw in _FRAMEWORKS:
        if categories is not None and fw.category not in categories:
            continue
        patterns.update(fw.contains_patterns)

    if not patterns:
        return "true"

    parts = " OR ".join(
        f"d CONTAINS '{p}'" for p in sorted(patterns)
    )
    return f"ANY(d IN {node_var}.decorators WHERE {parts})"
