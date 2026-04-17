"""Graph traversal for cgraph hybrid retrieval.

Given seed node UIDs, walks CALLS / IMPORTS / DEFINED_IN edges for
N hops and returns the neighborhood (callers, callees, imports).
"""

from __future__ import annotations

from typing import Any


def _query_edges(
    conn: Any,
    seed_uids: list[str],
    *,
    direction: str,
    rel_type: str,
    depth: int,
) -> list[dict[str, Any]]:
    """Query edges from/to seed nodes.

    direction: "out" (seed CALLS target) or "in" (caller CALLS seed).
    """
    if not seed_uids:
        return []

    uid_list = ", ".join(f"'{uid}'" for uid in seed_uids)

    if direction == "out":
        query = (
            f"MATCH (seed)-[r:`{rel_type}`*1..{depth}]->(target) "
            f"WHERE seed.uid IN [{uid_list}] AND seed.uid <> target.uid "
            f"RETURN DISTINCT target.uid AS uid, target.name AS name, "
            f"target.path AS file, target.line_number AS line_number, "
            f"label(target) AS kind"
        )
    else:
        query = (
            f"MATCH (source)-[r:`{rel_type}`*1..{depth}]->(seed) "
            f"WHERE seed.uid IN [{uid_list}] AND source.uid <> seed.uid "
            f"RETURN DISTINCT source.uid AS uid, source.name AS name, "
            f"source.path AS file, source.line_number AS line_number, "
            f"label(source) AS kind"
        )

    results: list[dict[str, Any]] = []
    try:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            results.append({
                "uid": row[0],
                "name": row[1],
                "file": f"{row[2]}:{row[3]}" if row[2] and row[3] else row[2],
                "kind": row[4],
            })
    except Exception:
        # Edge type may not exist for all node combinations — skip
        pass

    return results


def traverse(
    conn: Any,
    seed_uids: list[str],
    *,
    depth: int = 1,
) -> dict[str, list[dict[str, Any]]]:
    """Traverse the graph from seed nodes, returning the neighborhood.

    Returns {callers: [...], callees: [...], imports: [...]}.
    """
    callers = _query_edges(
        conn, seed_uids, direction="in", rel_type="CALLS", depth=depth
    )
    callees = _query_edges(
        conn, seed_uids, direction="out", rel_type="CALLS", depth=depth
    )
    imports = _query_edges(
        conn, seed_uids, direction="out", rel_type="IMPORTS", depth=depth
    )

    # Deduplicate across categories by uid
    seen: set[str] = set(seed_uids)

    def _dedup(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        for n in nodes:
            if n["uid"] not in seen:
                seen.add(n["uid"])
                unique.append(n)
        return unique

    return {
        "callers": _dedup(callers),
        "callees": _dedup(callees),
        "imports": _dedup(imports),
    }
