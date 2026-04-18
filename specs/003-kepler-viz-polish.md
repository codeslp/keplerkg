# Spec 003 — KeplerKG Rename + Viz Polish Phase

**Parent:** [001-btrain-integration.md](001-btrain-integration.md)
**Progress doc:** [001-progress.md](001-progress.md)
**Status:** COMPLETE
**Created:** 2026-04-17

---

## Scope

User-driven polish pass on the three-tab viz dashboard (2D Cytoscape, 3D
force-graph, Embedding Projector) plus a rename from `cgraph` to **KeplerKG**
across all user-facing surfaces. Infrastructure rename (pypi / module /
CLI `cgc`) is deferred — those names extend upstream codegraphcontext and
a mechanical rename would collide.

## Work items (ordered — implement in this order)

### 1. KeplerKG rename — user-facing strings only

- Dashboard nav title `cgraph` → `KeplerKG` in `commands/viz_dashboard.py`
- Inner 2D + 3D banner titles → `KeplerKG — Code Graph` / `KeplerKG — Code Graph (3D)` in `commands/viz_graph.py` (also see §2 — banner will be restructured)
- Projector banner `Embedding Projector: Which Functions Are Similar to Each Other?` stays (it's a descriptive lede, not a product mark)
- README.md title
- Package docstring `"""cgc viz-dashboard: ..."""` headers
- **Deferred** (separate spec): `pyproject.toml` package name, `cgc` CLI entry point, module directory rename

### 2. Edge visibility — thicker lines, Contains green

- 2D Cytoscape: edge `"width"` 1 → 2 (all edges)
- 3D force-graph: use `.linkWidth(3)` — currently the default which reads as hairlines
- `EDGE_COLORS.CONTAINS`: `#30363d` → `#7ee787` (cgraph Function-green — already in the palette, matches "Function is contained in Module" visual semantics)
- Legend entries update to match in both 2D + 3D templates

### 3. Collapsible help ribbons on 2D + 3D tabs

Port the Embeddings-tab `.emb-explainer` ribbon pattern into the 2D and 3D
iframe templates. Each ribbon gets:
- A lede: one-sentence description
- 3-column tip grid: "What you're looking at" / "How to interact" / "Layout / controls"
- `Clean`/`Advanced` buttons — placeholder (not wired) for future Cytoscape simple mode
- Chevron collapse button identical to Embeddings ribbon

Ribbon lives inside the iframe so it collapses the graph surface, not the
outer dashboard chrome.

### 4. Remove redundant inner banner on 2D + 3D

Current dashboard nav already shows `{name} · N nodes · N edges · N embeddings`.
The inner iframe banners (`#header h1 + .stats`) duplicate that. Drop the
title + stats — keep only the controls (layout select for 2D, search for
both). New header is a single thin control bar, not a full banner.

### 5. Color mapper fix (Embeddings)

User-reported: "color filters don't seem to be doing anything." Likely my
`boostPointVisibility` re-applies every 500ms–10s and clobbers the color
mapper's per-point colors during that window. Fix:

- Detect "color mapper is active" by checking whether `pointColors` has
  varied values across points
- Once active, set a flag and stop re-applying the fallback color
- Keep the `pointScaleFactors` boost unconditionally — that doesn't
  interfere with color mapping

### 6. About / Credits modal (last)

Full-visibility "About" link in the dashboard nav (right side, **not
muted** — user called this out explicitly). Opens a modal with:

1. **Purpose** — what KeplerKG is and why it exists (1 paragraph).
   Must include the broader aim the user stated: *KeplerKG exists to make
   the creation of knowledge graphs and embeddings for institutional
   knowledge of all kinds — code is the pilot domain, not the ceiling.*
   Code-graph work is framed as the beachhead; the generalised goal is
   turning any corpus (docs, transcripts, tickets, process knowledge)
   into a navigable graph + embedding space.
2. **Future plans** — roadmap bullets:
   - Generalise beyond source code to institutional corpora (docs,
     meeting notes, ticket histories, process wikis)
   - MCP server mode for agentic retrieval against a KeplerKG graph
   - Drift detection + advisories on stale or contradicted knowledge
   - Scale to larger repos / multi-corpus federation
3. **Credits** — license-linked table: cgraph / codegraphcontext /
   TensorFlow Embedding Projector / Cytoscape.js / 3d-force-graph /
   KùzuDB / sentence-transformers / HuggingFace Hub.  Anchored at the
   bottom of the modal per user instruction.

Credits block sits at the bottom of the modal per user instruction.

License references:

| Tool | License | Upstream |
|---|---|---|
| KeplerKG (this repo) | MIT | `./LICENSE` |
| codegraphcontext | Apache 2.0 | https://github.com/Vi-Sri/CodeGraphContext |
| TensorFlow Embedding Projector | Apache 2.0 © Google | https://github.com/tensorflow/embedding-projector-standalone (already tracked in [projector/NOTICE.md](../src/codegraphcontext_ext/viz_assets/projector/NOTICE.md)) |
| Cytoscape.js | MIT | https://js.cytoscape.org/ |
| 3d-force-graph | MIT | https://github.com/vasturiano/3d-force-graph |
| KùzuDB | MIT | https://kuzudb.com/ |
| sentence-transformers | Apache 2.0 | https://sbert.net/ |

Confirm upstream licenses during implementation — table above is provisional.

## Acceptance

- 2D and 3D graphs have thicker, more-visible edges; Contains edges are green
- Dashboard nav + inner iframes all read "KeplerKG"
- 2D and 3D tabs have the same collapsible help ribbon UX as Embeddings
- Inner 2D/3D banners are controls-only (no duplicated title/stats)
- Color mapper in Embeddings changes point colors live (no clobber)
- About modal opens from visible nav link, shows purpose + future plans, with credits at bottom

## Out of scope (this phase)

- Package / CLI / module rename — separate spec
- Shipping a real "Simple / Advanced" mode for Cytoscape (placeholder only)
- Color mapper redesign (just fix the clobber)
