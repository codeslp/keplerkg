# Visualizing the Graph

Sometimes a table of text is not enough-you need to see the map. KeplerKG ships a local visualization server and includes repo-managed frontend surfaces for broader exploration.

## Local server: `kkg visualize`

Run:

```bash
kkg visualize
```

This starts a **local FastAPI server** that serves a **React** visualization of your current graph. Open the URL printed in the terminal (typically `http://127.0.0.1` with a chosen port).

### Modes

The UI supports several views of the same graph data:

- **2D force-directed graph** — classic node–link layout for navigation and clustering.
- **3D force-directed graph** — spatial exploration of larger graphs.
- **3D city view** — an alternative structural layout for hierarchy and density.
- **Mermaid flowchart** — diagram-style export and inspection of selected subgraphs.

Use the in-app controls to switch modes and focus on the neighborhood you care about.

## Hosted explorer

The repository includes standalone frontends under `site/` and `website/`, but the current GitHub Actions setup does **not** auto-deploy a hosted explorer. The reliable workflow today is local `kkg visualize`.

## Neo4j users: Neo4j Browser and Bloom

If your **`DEFAULT_DATABASE`** (or config) points at **Neo4j**, you can still use **Neo4j Browser** (and **Neo4j Bloom** on Desktop) for Cypher-centric exploration. The local `kkg visualize` experience is backend-agnostic where supported; Neo4j-specific URLs and tools remain available when Neo4j is your active backend.

---

For CLI details and options, see the CLI reference for the `visualize` command.
