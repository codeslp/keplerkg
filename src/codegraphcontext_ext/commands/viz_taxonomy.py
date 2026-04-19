"""Taxonomy data fetcher for the KeplerKG dashboard.

Fetches structural containment (CONTAINS edges), inheritance forests
(INHERITS + IMPLEMENTS edges), and (future) community-detected subsystems
from the KuzuDB graph.  Returns flat node lists with parent references
that Cytoscape compound-node rendering consumes directly.

Phase 5.5a — explicit hierarchy.  Phase 5.5b adds Leiden communities.
"""

from __future__ import annotations

import json
import sys
from typing import Any


# ── Containment queries ────────────────────────────────────────────────
# Each query returns (parent_id, id, label, type, path, line).

_STRUCTURE_QUERIES: list[str] = [
    # Repository → Directory
    "MATCH (r:Repository)-[:CONTAINS]->(d:Directory) "
    "RETURN r.path, d.path, d.name, 'Directory', d.path, 0",
    # Directory → Directory
    "MATCH (p:Directory)-[:CONTAINS]->(d:Directory) "
    "RETURN p.path, d.path, d.name, 'Directory', d.path, 0",
    # Directory → File
    "MATCH (d:Directory)-[:CONTAINS]->(f:File) "
    "RETURN d.path, f.path, f.name, 'File', f.path, 0",
    # Repository → File (top-level)
    "MATCH (r:Repository)-[:CONTAINS]->(f:File) "
    "RETURN r.path, f.path, f.name, 'File', f.path, 0",
    # File → Class
    "MATCH (f:File)-[:CONTAINS]->(c:Class) "
    "RETURN f.path, c.uid, c.name, 'Class', c.path, c.line_number",
    # File → Function
    "MATCH (f:File)-[:CONTAINS]->(fn:Function) "
    "RETURN f.path, fn.uid, fn.name, 'Function', fn.path, fn.line_number",
    # Class → Function (methods)
    "MATCH (c:Class)-[:CONTAINS]->(fn:Function) "
    "RETURN c.uid, fn.uid, fn.name, 'Function', fn.path, fn.line_number",
    # Function → Function (nested)
    "MATCH (outer:Function)-[:CONTAINS]->(inner:Function) "
    "RETURN outer.uid, inner.uid, inner.name, 'Function', inner.path, inner.line_number",
]

# Additional entity types that can appear under File via CONTAINS.
_EXTRA_CONTAINED = (
    "Variable", "Trait", "Interface", "Struct", "Enum",
)
# Types that need backtick-escaping in Cypher because they're reserved words.
_ESCAPED_TYPES = {"Macro", "Union", "Property"}


def _build_all_structure_queries(limit: int) -> list[str]:
    queries = [q + f" LIMIT {limit}" for q in _STRUCTURE_QUERIES]
    for t in _EXTRA_CONTAINED:
        queries.append(
            f"MATCH (f:File)-[:CONTAINS]->(n:{t}) "
            f"RETURN f.path, n.uid, n.name, '{t}', n.path, n.line_number "
            f"LIMIT {limit}"
        )
    for t in _ESCAPED_TYPES:
        queries.append(
            f"MATCH (f:File)-[:CONTAINS]->(n:`{t}`) "
            f"RETURN f.path, n.uid, n.name, '{t}', n.path, n.line_number "
            f"LIMIT {limit}"
        )
    return queries


# ── Public API ─────────────────────────────────────────────────────────

def fetch_structure(conn: Any, *, limit: int = 500) -> dict[str, Any]:
    """Fetch the containment hierarchy as flat nodes with parent refs.

    Returns ``{"nodes": [...], "stats": {...}}``.
    """
    nodes: dict[str, dict[str, Any]] = {}

    # Root: Repository nodes (no parent)
    try:
        result = conn.execute(
            "MATCH (r:Repository) "
            "RETURN r.path, r.name, 'Repository', r.path, 0 "
            "LIMIT 10"
        )
        while result.has_next():
            row = result.get_next()
            nodes[row[0]] = {
                "id": row[0], "label": row[1] or row[0],
                "type": row[2], "parent": None,
                "path": row[3] or "", "line": row[4] or 0,
            }
    except Exception:
        pass

    for query in _build_all_structure_queries(limit):
        try:
            result = conn.execute(query)
        except Exception:
            continue
        while result.has_next():
            row = result.get_next()
            parent_id, node_id, label, node_type, path, line = row
            if not node_id or node_id in nodes:
                continue
            nodes[node_id] = {
                "id": node_id,
                "label": label or "(anonymous)",
                "type": node_type,
                "parent": parent_id,
                "path": path or "",
                "line": line or 0,
            }
            # Ensure the referenced parent exists (synthetic placeholder).
            if parent_id and parent_id not in nodes:
                basename = parent_id.rsplit("/", 1)[-1] if "/" in str(parent_id) else str(parent_id)
                nodes[parent_id] = {
                    "id": parent_id, "label": basename,
                    "type": "Directory", "parent": None,
                    "path": str(parent_id), "line": 0,
                }

    type_counts: dict[str, int] = {}
    for n in nodes.values():
        type_counts[n["type"]] = type_counts.get(n["type"], 0) + 1

    return {"nodes": list(nodes.values()), "stats": type_counts}


def fetch_inheritance(conn: Any, *, limit: int = 500) -> dict[str, Any]:
    """Fetch INHERITS + IMPLEMENTS edges and the participating nodes.

    Returns ``{"nodes": [...], "edges": [...], "roots": [...], "stats": {...}}``.
    """
    edges: list[dict[str, str]] = []
    nodes: dict[str, dict[str, Any]] = {}

    def _add_node(nid: str, label: str, ntype: str, path: str, line: int | None) -> None:
        if nid and nid not in nodes:
            nodes[nid] = {
                "id": nid, "label": label or "(anonymous)",
                "type": ntype, "path": path or "", "line": line or 0,
            }

    # INHERITS
    try:
        result = conn.execute(
            "MATCH (child)-[r:INHERITS]->(parent) "
            "RETURN child.uid, child.name, label(child), child.path, child.line_number, "
            "       parent.uid, parent.name, label(parent), parent.path, parent.line_number "
            f"LIMIT {limit}"
        )
        while result.has_next():
            row = result.get_next()
            c_id, c_name, c_type, c_path, c_line = row[0:5]
            p_id, p_name, p_type, p_path, p_line = row[5:10]
            if c_id and p_id:
                edges.append({"source": str(c_id), "target": str(p_id), "type": "INHERITS"})
                _add_node(str(c_id), c_name, c_type, c_path, c_line)
                _add_node(str(p_id), p_name, p_type, p_path, p_line)
    except Exception:
        pass

    # IMPLEMENTS
    try:
        result = conn.execute(
            "MATCH (impl)-[r:IMPLEMENTS]->(iface:Interface) "
            "RETURN impl.uid, impl.name, label(impl), impl.path, impl.line_number, "
            "       iface.uid, iface.name, iface.path, iface.line_number "
            f"LIMIT {limit}"
        )
        while result.has_next():
            row = result.get_next()
            i_id, i_name, i_type, i_path, i_line = row[0:5]
            f_id, f_name, f_path, f_line = row[5:9]
            if i_id and f_id:
                edges.append({"source": str(i_id), "target": str(f_id), "type": "IMPLEMENTS"})
                _add_node(str(i_id), i_name, i_type, i_path, i_line)
                _add_node(str(f_id), f_name, "Interface", f_path, f_line)
    except Exception:
        pass

    children = {e["source"] for e in edges}
    parents = {e["target"] for e in edges}
    roots = list(parents - children)

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "roots": roots,
        "stats": {
            "trees": len(roots),
            "total_nodes": len(nodes),
            "inherits_edges": sum(1 for e in edges if e["type"] == "INHERITS"),
            "implements_edges": sum(1 for e in edges if e["type"] == "IMPLEMENTS"),
        },
    }


def _fetch_communities(conn: Any) -> dict[str, Any] | None:
    """Run community detection and return results, or None on failure."""
    try:
        from ..topology.communities import fetch_community_data
        return fetch_community_data(conn)
    except Exception as exc:
        print(f"  community detection skipped: {exc}", file=sys.stderr)
        return None


def fetch_taxonomy_data(conn: Any, *, limit: int = 500) -> dict[str, Any]:
    """Fetch all taxonomy data: structure + inheritance + communities."""
    return {
        "structure": fetch_structure(conn, limit=limit),
        "inheritance": fetch_inheritance(conn, limit=limit),
        "communities": _fetch_communities(conn),
    }


def taxonomy_json(conn: Any, *, limit: int = 500) -> str:
    """Fetch taxonomy data and serialize as a JSON string for dashboard injection."""
    print("Fetching taxonomy data...", file=sys.stderr)
    data = fetch_taxonomy_data(conn, limit=limit)
    struct_count = len(data["structure"]["nodes"])
    inh_count = data["inheritance"]["stats"]["total_nodes"]
    comm_count = data["communities"]["stats"]["communities"] if data["communities"] else 0
    print(
        f"  taxonomy: {struct_count} structure nodes, "
        f"{inh_count} inheritance nodes, "
        f"{comm_count} communities",
        file=sys.stderr,
    )
    return json.dumps(data)
