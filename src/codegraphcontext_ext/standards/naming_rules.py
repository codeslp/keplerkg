"""Embedding-backed naming analysis rules (CGQ-F01 through CGQ-F04).

These rules require Python-side vector math because KuzuDB 0.4+ has no
native cosine similarity function.  Each rule fetches embeddings via
Cypher, then computes similarity in Python.

Spec §14.1 Category F.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Callable

from ..embeddings.schema import EMBEDDING_COLUMN, NAME_EMBEDDING_COLUMN
from ..hybrid.ann import cosine_similarity

# Type: (conn, thresholds) -> list of violation dicts
RuleFunc = Callable[[Any, dict[str, int | float]], list[dict[str, Any]]]

# Registry keyed by rule id.
EMBEDDING_RULES: dict[str, RuleFunc] = {}

# Optional embedding provider — set by audit.py before execution.
# Only needed by F03 (module_content_mismatch) to embed file names on the fly.
_provider: Any = None


def set_provider(provider: Any) -> None:
    """Set the embedding provider for rules that need live embedding (F03)."""
    global _provider
    _provider = provider


def _register(rule_id: str) -> Callable[[RuleFunc], RuleFunc]:
    def wrapper(fn: RuleFunc) -> RuleFunc:
        EMBEDDING_RULES[rule_id] = fn
        return fn
    return wrapper


# ---------------------------------------------------------------------------
# Shared fetch helpers
# ---------------------------------------------------------------------------

def _fetch_dual_embeddings(
    conn: Any,
    table: str = "Function",
    *,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Fetch nodes that have both behavior and name embeddings."""
    limit_clause = f"LIMIT {limit}" if limit else ""
    query = (
        f"MATCH (n:`{table}`) "
        f"WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL "
        f"  AND n.`{NAME_EMBEDDING_COLUMN}` IS NOT NULL "
        f"  AND NOT n.is_dependency "
        f"RETURN n.uid, n.name, n.path, n.line_number, "
        f"  n.`{EMBEDDING_COLUMN}`, n.`{NAME_EMBEDDING_COLUMN}` "
        f"{limit_clause}"
    )
    result = conn.execute(query)
    rows: list[dict[str, Any]] = []
    while result.has_next():
        row = result.get_next()
        rows.append({
            "uid": str(row[0]),
            "name": row[1],
            "path": row[2],
            "line_number": row[3],
            "behavior_vec": list(row[4]),
            "name_vec": list(row[5]),
        })
    return rows


# ---------------------------------------------------------------------------
# F01 — misleading_name
# ---------------------------------------------------------------------------

@_register("misleading_name")
def _misleading_name(
    conn: Any,
    thresholds: dict[str, int | float],
) -> list[dict[str, Any]]:
    """Flag functions whose name and behavior embeddings are far apart.

    cosine(name_embedding, behavior_embedding) < threshold on the same node.
    """
    threshold = float(thresholds.get("warn", 0.15))
    nodes = _fetch_dual_embeddings(conn)
    violations: list[dict[str, Any]] = []
    for n in nodes:
        sim = cosine_similarity(n["name_vec"], n["behavior_vec"])
        if sim < threshold:
            violations.append({
                "uid": n["uid"],
                "name": n["name"],
                "path": n["path"],
                "line_number": n["line_number"],
                "metric_value": round(sim, 4),
            })
    return violations


# ---------------------------------------------------------------------------
# F02 — inconsistent_naming
# ---------------------------------------------------------------------------

@_register("inconsistent_naming")
def _inconsistent_naming(
    conn: Any,
    thresholds: dict[str, int | float],
) -> list[dict[str, Any]]:
    """Flag function pairs that do similar work but have dissimilar names.

    cosine(behavior_A, behavior_B) > behavior_threshold
    AND cosine(name_A, name_B) < name_threshold
    """
    behavior_thresh = float(thresholds.get("behavior_similarity", 0.85))
    name_thresh = float(thresholds.get("name_dissimilarity", 0.5))
    max_nodes = int(thresholds.get("max_nodes", 2000))

    nodes = _fetch_dual_embeddings(conn, limit=max_nodes)
    violations: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            beh_sim = cosine_similarity(a["behavior_vec"], b["behavior_vec"])
            if beh_sim < behavior_thresh:
                continue
            name_sim = cosine_similarity(a["name_vec"], b["name_vec"])
            if name_sim >= name_thresh:
                continue
            pair_key = tuple(sorted((a["uid"], b["uid"])))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            # Report A as the violation, with B as the similar peer
            violations.append({
                "uid": a["uid"],
                "name": a["name"],
                "path": a["path"],
                "line_number": a["line_number"],
                "metric_value": f"{b['name']} (behavior={round(beh_sim, 2)}, name={round(name_sim, 2)})",
            })
    return violations


# ---------------------------------------------------------------------------
# F03 — module_content_mismatch
# ---------------------------------------------------------------------------

@_register("module_content_mismatch")
def _module_content_mismatch(
    conn: Any,
    thresholds: dict[str, int | float],
) -> list[dict[str, Any]]:
    """Flag files whose name is distant from the centroid of their functions.

    cosine(file_name_embedding, centroid(contained function behavior embeddings))
    < threshold.
    """
    threshold = float(thresholds.get("warn", 0.3))

    if _provider is None:
        return []  # Cannot embed file names without a provider

    # Fetch all functions with behavior embeddings
    query = (
        f"MATCH (n:Function) "
        f"WHERE n.`{EMBEDDING_COLUMN}` IS NOT NULL "
        f"  AND NOT n.is_dependency "
        f"RETURN n.path, n.`{EMBEDDING_COLUMN}`"
    )
    result = conn.execute(query)

    # Group behavior embeddings by file path
    by_file: dict[str, list[list[float]]] = defaultdict(list)
    while result.has_next():
        row = result.get_next()
        path = row[0]
        if path:
            by_file[path].append(list(row[1]))

    if not by_file:
        return []

    # Compute centroid per file
    centroids: dict[str, list[float]] = {}
    for path, vecs in by_file.items():
        dims = len(vecs[0])
        centroid = [0.0] * dims
        for v in vecs:
            for d in range(dims):
                centroid[d] += v[d]
        n = len(vecs)
        centroids[path] = [c / n for c in centroid]

    # Extract file names and embed them
    paths = list(centroids.keys())
    file_names = [_path_to_name(p) for p in paths]
    try:
        file_name_vecs = _provider.embed_texts(file_names)
    except Exception:
        return []

    violations: list[dict[str, Any]] = []
    for path, name_vec in zip(paths, file_name_vecs):
        sim = cosine_similarity(list(name_vec), centroids[path])
        if sim < threshold:
            violations.append({
                "uid": path,
                "name": os.path.basename(path),
                "path": path,
                "line_number": None,
                "metric_value": round(sim, 4),
            })
    return violations


def _path_to_name(path: str) -> str:
    """Extract a human-readable name from a file path for embedding.

    'src/utils/calculate_prices.py' -> 'calculate prices'
    """
    basename = os.path.basename(path)
    stem = os.path.splitext(basename)[0]
    # Reuse the same humanization as name embeddings
    from ..commands.embed import _humanize_name
    return _humanize_name(stem)


# ---------------------------------------------------------------------------
# F04 — suggest_better_name
# ---------------------------------------------------------------------------

@_register("suggest_better_name")
def _suggest_better_name(
    conn: Any,
    thresholds: dict[str, int | float],
) -> list[dict[str, Any]]:
    """For poorly-named functions, find well-named neighbors as exemplars.

    A function is "poorly named" if its name-behavior cosine < self_low.
    An exemplar is a behavior-similar function with name-behavior cosine > exemplar_high.
    """
    self_low = float(thresholds.get("self_low", 0.4))
    exemplar_high = float(thresholds.get("exemplar_high", 0.7))
    max_exemplars = int(thresholds.get("max_exemplars", 3))

    nodes = _fetch_dual_embeddings(conn)
    if not nodes:
        return []

    # Split into poorly-named and well-named pools
    poor: list[dict[str, Any]] = []
    well: list[dict[str, Any]] = []
    for n in nodes:
        sim = cosine_similarity(n["name_vec"], n["behavior_vec"])
        n["self_sim"] = sim
        if sim < self_low:
            poor.append(n)
        elif sim > exemplar_high:
            well.append(n)

    if not poor or not well:
        return []

    violations: list[dict[str, Any]] = []
    for p in poor:
        exemplars: list[tuple[str, float]] = []
        for w in well:
            beh_sim = cosine_similarity(p["behavior_vec"], w["behavior_vec"])
            if beh_sim > 0.5:  # only suggest from behavior-similar neighbors
                exemplars.append((w["name"], round(beh_sim, 2)))
        if not exemplars:
            continue
        exemplars.sort(key=lambda x: x[1], reverse=True)
        top = exemplars[:max_exemplars]
        suggestion = ", ".join(f"{name} ({sim})" for name, sim in top)
        violations.append({
            "uid": p["uid"],
            "name": p["name"],
            "path": p["path"],
            "line_number": p["line_number"],
            "metric_value": f"similar to: {suggestion}",
        })
    return violations
