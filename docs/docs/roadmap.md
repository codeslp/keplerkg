# Project Roadmap

CodeGraphContext is an evolving tool. We believe in transparency about where we are and where we are going.

## Currently Supported (Stable)

These capabilities are live in version **0.4.2**.

- **Languages (19):** Python, JavaScript, TypeScript, TSX, Go, Rust, C, C++, Java, Ruby, C#, PHP, Kotlin, Scala, Swift, Dart, Perl, Haskell, and Elixir.
- **Database backends (4):** FalkorDB Lite (default on Unix with Python 3.12+), FalkorDB Remote, KuzuDB (Windows and embedded fallback), and Neo4j.
- **MCP server:** 20 tools for Cursor, Claude Desktop, Windsurf, VS Code, and other MCP clients (`cgc mcp start`).
- **Live watching:** Real-time updates via `cgc watch`.
- **Bundles and registry (shipped):** Export/import graphs and use the public bundle registry from the CLI and website.
- **Contexts:** Multiple isolated graphs (multi-graph workflows).
- **Visualization server (shipped):** Local graph exploration via `cgc visualize` (FastAPI + React UI).
- **SCIP indexing:** Available as an opt-in beta for richer symbol indexing.
- **CLI:** 55+ commands, including `cgc query` for Cypher and `cgc find` for name-based search.

## ASAP — Dogfooding-Driven Improvements

Issues surfaced by the [dogfooding validation experiments](https://github.com/codeslp/keplerkg/tree/main/research/experiments/dogfooding). These affect every new user applying KeplerKG to a codebase.

- **`kkg doctor` command:** Validate backend, DB access, embeddings, and PATH in one command. (Critical — three onboarding blockers hit before experiments could run.)
- **Zero-CALLS-edge warning:** `kkg index` should warn when no call relationships are extracted and suggest enabling SCIP. (High — graph-based features silently degrade without CALLS edges.)
- **Unified backend config loading:** The ext layer's `resolve_requested_backend()` should respect `.codegraphcontext/.env` instead of probing installed backends. (High — causes split-brain where config says kuzudb but ext detects falkordb.)
- **`relative_path` in search results:** Add a `relative_path` field to search output alongside the existing `file` field. (Medium — programmatic consumers need standard paths.)
- **Document uid format:** Publish the `{name}{abs_path}{line}` uid format. Consider normalizing to `relative_path::name`. (Medium — integration code currently reverse-engineers the format.)
- **AST-based call extraction fallback:** Extract basic call relationships from the AST when SCIP is not available. (Short-term — covers simple cases without requiring SCIP tooling.)
- **Single `--target` flag:** Replace the `--project` + `--source-dir` split with a single `--target /path/to/repo` that derives both. (Short-term — reduces cross-project confusion.)

## In Progress

Work underway in active development.

- **Advanced language query toolkits:** Deeper, language-aware query helpers on top of the graph.
- **Streaming for large results:** Better handling of very large query result sets.
- **More parser tests:** Broader coverage and regression tests across the Tree-sitter parsers.
- **Dogfooding experiment follow-ups:** Fix path normalization in 1B, scope alignment in 1A, cross-project validation on Flask.

## Planned

Directions we are exploring for upcoming releases.

- **SSE/HTTP MCP transport:** Alternatives to stdio for MCP hosting and integration.
- **CI/CD GitHub Action:** Automated indexing and feedback in pull-request workflows.
- **Semantic search:** Search and navigation beyond exact symbol and structural queries.
- **Cloud sync:** Optional sharing and synchronization of graphs across environments and teams.

---

!!! info "Request a feature"
    Have an idea? Open an issue on our [GitHub repository](https://github.com/CodeGraphContext/CodeGraphContext/issues).
