# KeplerKG

**Fast, token-efficient context for AI agents — powered by knowledge graphs and embeddings.**

KeplerKG turns codebases into structured graphs and embedding spaces that AI agents can query in hundreds of tokens instead of tens of thousands. Every command outputs machine-readable JSON so agents get precise, pre-computed context without reading entire files.

Code is the pilot domain, not the ceiling — the architecture generalizes to any corpus (documentation, transcripts, ticket histories, process wikis) where structure, similarity, and drift matter.

## Why agents need this

AI coding agents today burn most of their context window reading raw files to understand what they're changing. KeplerKG pre-computes the structural and semantic relationships so the agent gets exactly what it needs:

- **`kkg context <query>`** — semantic search returns the most relevant symbols for a question. ~200 tokens instead of reading 10 files.
- **`kkg review-packet`** — blast radius of a diff in ~2KB of JSON: touched nodes, external callers/callees, cross-module impact, advisories. The reviewer (human or agent) doesn't open a single file.
- **`kkg blast-radius --files <paths>`** — transitive graph expansion to find everything affected by a change, including overlap with other active work.

All outputs are JSON with stable schemas under `schemas/`. Agents parse them directly — no scraping, no heuristics.

## Quick start

```bash
git clone https://github.com/codeslp/keplerkg.git
cd keplerkg
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

kkg index                                    # build the graph
kkg embed                                    # embed functions + classes
kkg context "authentication token validation" # semantic search
kkg viz-dashboard                            # interactive 3-tab dashboard
```

## Commands

| Command | What it does | Tokens saved |
|---------|-------------|-------------|
| `kkg index` | Parse repo into KuzuDB graph (18 node types, 7 edge types) | — |
| `kkg embed` | Batch-embed functions and classes (local Jina v2, 768-dim) | — |
| `kkg context <query>` | Semantic search + graph neighborhood expansion | 10-50x vs. raw file reads |
| `kkg review-packet` | Reviewer JSON: touched nodes, callers, callees, advisories | 5-20x vs. raw diff |
| `kkg blast-radius` | Transitive caller/callee expansion + lock overlap detection | Catches what `git diff` misses |
| `kkg sync-check` | Report upstream commits not yet merged | — |
| `kkg viz-dashboard` | Three-tab browser dashboard (2D, 3D, Embeddings) | — |
| `kkg viz-graph` | Standalone 2D or 3D graph visualization | — |
| `kkg viz-embeddings` | Standalone embedding scatter plot | — |
| `kkg export-embeddings` | Export embeddings as TSV for external tools | — |

## Embedding providers

| Provider | Model | Dimensions | Requires |
|----------|-------|-----------|----------|
| `local` (default) | jina-embeddings-v2-base-code | 768 | Nothing (runs on CPU) |
| `voyage` | voyage-code-3 | 1024 | `VOYAGE_API_KEY` |
| `openai` | text-embedding-3-large | 3072 | `OPENAI_API_KEY` |

## Architecture

```
src/
  codegraphcontext/          # Graph indexer, KuzuDB driver, parsers
  codegraphcontext_ext/      # KeplerKG extensions
    commands/                # CLI commands (context, embed, review-packet, blast-radius, viz-*)
    embeddings/              # Embedding pipeline (providers, schema, runtime)
    hybrid/                  # Hybrid retrieval (ANN search + graph traversal)
    viz_assets/projector/    # Vendored TF Embedding Projector
    viz_server.py            # HTTP server for dashboard + projector
schemas/                     # JSON Schema for every command output
tests/                       # 228 tests
```

**Graph store:** KuzuDB (embedded, no server). 18 node tables, 7 relationship groups, HNSW indexes for ANN search.

**Agent integration:** Every command emits structured JSON to stdout with a `kind` discriminator and stable schema. Agents pipe command output directly into their context — no parsing needed.

## Credits

| Tool | License |
|------|---------|
| [CodeGraphContext](https://github.com/Vi-Sri/CodeGraphContext) | Apache 2.0 |
| [TensorFlow Embedding Projector](https://github.com/tensorflow/embedding-projector-standalone) | Apache 2.0 |
| [Cytoscape.js](https://js.cytoscape.org/) | MIT |
| [3d-force-graph](https://github.com/vasturiano/3d-force-graph) | MIT |
| [KuzuDB](https://kuzudb.com/) | MIT |
| [sentence-transformers](https://sbert.net/) | Apache 2.0 |

## License

MIT
