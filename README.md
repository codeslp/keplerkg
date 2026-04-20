# KeplerKG

**Fast, token-efficient context for AI agents — powered by knowledge graphs and embeddings.**

KeplerKG turns codebases into structured graphs and embedding spaces that AI agents can query in hundreds of tokens instead of tens of thousands. Every command outputs machine-readable JSON so agents get precise, pre-computed context without reading entire files.

Code is the pilot domain, not the ceiling — the architecture generalizes to any corpus (documentation, transcripts, ticket histories, process wikis) where structure, similarity, and drift matter.

## Why agents need this

AI coding agents today burn most of their context window reading raw files to understand what they're changing. KeplerKG pre-computes the structural and semantic relationships so the agent gets exactly what it needs:

- **`kkg search <query>`** — semantic search returns the most relevant symbols for a question. ~200 tokens instead of reading 10 files.
- **`kkg review-packet`** — blast radius of a diff in ~2KB of JSON: touched nodes, external callers/callees, cross-module impact, advisories. The reviewer (human or agent) doesn't open a single file.
- **`kkg blast-radius --files <paths>`** — transitive graph expansion to find everything affected by a change, including overlap with other active work.
- **`kkg audit`** — run code-quality standards backed by graph queries and report violations. Configurable presets (default, strict, SOC 2, minimal) with per-rule overrides.

All outputs are JSON with stable schemas under `schemas/`. Agents parse them directly — no scraping, no heuristics.

## Validated by dogfooding

We ran KeplerKG on its own codebase and measured the results (reproducible scripts in `research/experiments/dogfooding/`):

| Validated claim | Result | How we measured |
|-------|--------|-----------------|
| **Token-efficient context** | **67.4% reduction** — review-packet uses 37K tokens vs 115K for raw diff across 15 commits | tiktoken cl100k_base exact token counting (Exp 3A) |
| **Context compression** | **~760 tokens** per query vs 366K for reading all source files (token count only — answer quality evaluation pending) | 15 code-understanding queries, 5 approaches (Exp 3B Phase 1) |
| **Finds what line-by-line tools can't** | **323 graph-exclusive findings** — circular imports, unreferenced symbols, cross-file access violations | Compared kkg audit vs radon + pylint on the same codebase (Exp 2B) |

Structural accuracy (Exp 1A) and search relevance (Exp 1B) experiments ran but have known path-normalization issues documented in the findings. Follow-up runs planned.

Reproduce the experiments yourself:

```bash
git clone https://github.com/codeslp/keplerkg.git && cd keplerkg
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -r research/experiments/dogfooding/requirements-experiments.txt
kkg index && kkg embed
cd research/experiments/dogfooding && make all
```

## Quick start

```bash
git clone https://github.com/codeslp/keplerkg.git
cd keplerkg
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

kkg index                                    # build the graph
kkg embed                                    # embed functions + classes
kkg search "authentication token validation" # semantic search
kkg audit --list                             # see available quality rules
kkg viz-dashboard                            # interactive 4-tab dashboard
```

## Targeting Other Codebases

KeplerKG can now route each target repo to its own Kuzu store under
`/Volumes/zombie/cgraph/db/<slug>/kuzudb`.

Project resolution precedence:
- `--project <slug>`
- `CGRAPH_PROJECT=<slug>`
- repo-local `.cgraph/project.toml` with top-level `project = "<slug>"`
- fallback to the current target directory basename

Example `project.toml`:

```toml
project = "flask"
```

Flask bootstrap flow:

```bash
git clone https://github.com/pallets/flask /Volumes/zombie/cgraph/src/flask
source scripts/cgraph-env.sh

kkg index --project flask /Volumes/zombie/cgraph/src/flask
kkg embed --project flask
kkg search --project flask "request context"
kkg audit --project flask --format summary
kkg viz-graph --project flask
kkg viz-embeddings --project flask
```

Verified on April 18, 2026:
- Flask graph store: `/Volumes/zombie/cgraph/db/flask/kuzudb` (`83.6M`)
- Graph counts: `1` repo, `83` files, `1463` functions, `161` classes
- Embeddings written: `856`
- `kkg search --project flask "request context"` returned `8` seeds, all under the Flask checkout

Notes:
- `kkg index` and `kkg watch` now honor `--project`.
- DB-touching extension commands (`search`, `embed`, `audit`, `blast-radius`, `review-packet`, `viz-*`, `export-embeddings`, `drift-check`) honor `--project`.
- On Kuzu builds that reject `CREATE HNSW INDEX`, semantic search falls back to a linear embedding scan so new project stores stay usable.

## Agent CLI Contract (Phase 2.5 — Complete)

Every command emits a canonical JSON envelope with reserved keys (`ok`, `kind`,
`schema_version`, `project`, `error`).  Agents parse one contract, not
per-command quirks.

- **`kkg manifest --json`** — enumerate commands, schemas, `--project`
  support, required env/prereqs, output modes, and KuzuDB dependency.
- **`kkg repl`** — interactive session with sticky `--project`, audit
  profile, query history, and command dispatch.
- **Contract tests** — 29 backend-free tests cover envelope schema,
  `make_envelope`, manifest schema, and command metadata without a live graph.
- **Agent skills** — `kkg-query` and `kkg-audit` skill definitions live in
  `.claude/skills/` (gitignored; local to each developer's Claude Code
  environment). See the skill YAML source in the repo wiki or create them
  locally via `/skill-creator`.

## Commands

### Retrieval & analysis

| Command | What it does | Tokens saved |
|---------|-------------|-------------|
| `kkg search <query>` | Semantic search + graph neighborhood expansion | 10-50x vs. raw file reads |
| `kkg review-packet` | Reviewer JSON: touched nodes, callers, callees, advisories | 5-20x vs. raw diff |
| `kkg blast-radius --files <paths>` | Transitive caller/callee expansion + lock overlap detection | Catches what `git diff` misses |
| `kkg drift-check --files <paths>` | Detect graph-neighborhood changes outside a lane | Catches silent upstream drift |
| `kkg impact --symbol <name>` | Symbol-oriented blast radius — BFS callers/callees | Focused impact analysis |
| `kkg execution-flow --symbol <name>` | Forward call-chain trace through CALLS edges | Call tree visualization |
| `kkg clusters` | Surface Louvain communities from the code graph | Module grouping discovery |
| `kkg entrypoints` | Entry-point scoring from decorators + in-degree | API surface enumeration |
| `kkg advise <situation>` | Situational tip lookup (lock_overlap, drift, etc.) | Pre-formatted recommendations |

### Code quality & standards

| Command | What it does |
|---------|-------------|
| `kkg audit` | Run 24 quality rules against the graph — coupling, complexity, dead code, clarity, inheritance, compliance |
| `kkg audit --profile soc2` | Run with SOC 2 compliance preset (auth-bypass, logging gaps, secrets) |
| `kkg audit --list` | List all registered standards and their current severity |
| `kkg audit --explain <id>` | Show a rule's definition, thresholds, evidence, and exemptions |
| `kkg audit --scope diff` | Only check files you just changed (for PostToolUse hooks) |
| `kkg audit --require-hard-zero` | Exit 2 if any hard violation fires (for CI gates) |
| `kkg audit --calibration-report` | Show metric distributions and candidate thresholds for tuning |
| `kkg health` | A-F letter grade from audit data |

### Indexing & embedding

| Command | What it does |
|---------|-------------|
| `kkg index` | Parse repo into KuzuDB graph (18 node types, 7 edge types) |
| `kkg embed` | Batch-embed functions and classes (local Jina v2, 768-dim) |
| `kkg sync-check` | Report upstream commits not yet merged (see cadence below) |

### Visualization

| Command | What it does |
|---------|-------------|
| `kkg viz-dashboard` | 4-tab browser dashboard: 2D graph, 3D graph, embeddings, standards config |
| `kkg viz-graph` | Standalone 2D or 3D graph visualization |
| `kkg viz-embeddings` | Standalone embedding scatter plot |
| `kkg viz-projector` | TF Embedding Projector (UMAP/t-SNE/PCA) |
| `kkg export-embeddings` | Export embeddings as TSV for external tools |

### Infrastructure

| Command | What it does |
|---------|-------------|
| `kkg serve` | Warm daemon on Unix socket — eliminates Python cold-start for fast commands |

## Standards & enforcement

KeplerKG ships 24 code-quality rules that query the graph for structural problems linters can't catch:

| Category | Rules | What they detect |
|----------|-------|-----------------|
| **Coupling** (4) | circular_imports, cross_file_private_access, excessive_fan_out, test_import_in_prod | Import cycles, private API misuse, high fan-out, test/prod boundary violations |
| **Complexity** (4) | function_cyclomatic_complexity, function_too_long, class_too_large, parameter_count | Functions and classes that are too complex |
| **Compliance** (8) | auth_bypass, unlogged_endpoint, sensitive_data_unprotected, hardcoded_secret_in_graph, admin_action_no_audit_trail, rate_limit_missing, separation_of_duties_violation, error_handler_leaks_internals | SOC 2 mapped compliance rules |
| **Naming** (4) | inconsistent_naming, misleading_name, module_content_mismatch, suggest_better_name | Embedding-backed naming analysis |
| **Dead code** (2) | unreferenced_public_function, unreferenced_public_class | Public symbols with zero callers in the graph |
| **Clarity** (1) | missing_docstring_public | Public API without documentation |
| **Inheritance** (1) | deep_inheritance | Inheritance chains deeper than 4 levels |

Every rule is backed by a Cypher query against the knowledge graph — not regex, not heuristics. The `evidence` field in each rule documents exactly what graph pattern proves the finding.

### Configuration

```toml
# In .btrain/project.toml or kkg.toml
[cgraph.standards]
profile = "soc2"                          # Preset: default | strict | soc2 | minimal
categories = ["coupling", "compliance"]   # Which categories to run

[cgraph.standards.overrides]
CGQ-B04 = "off"                           # Disable parameter_count
CGQ-A05 = "blocker"                       # Promote god_class to hard-stop
```

### Enforcement hooks

KeplerKG integrates with Claude Code hooks to enforce standards automatically:

- **PostToolUse hook** — runs `kkg audit --scope diff` after every Edit/Write (5s timeout)
- **Stop hook** — runs `kkg audit --scope session` before turn closes (10s timeout)
- **Pre-handoff** — `kkg audit --scope lane --require-hard-zero` gates handoffs
- **CI gate** — `kkg audit --require-hard-zero` on PRs (exit 2 on hard violations)

Copy `scripts/hooks/settings.example.json` to `.claude/settings.json` to enable.

### Sync-check cadence

`kkg sync-check` reports upstream commits not yet merged into your working
branch.  It resolves the source checkout via `--source-dir` or the
`[cgraph].source_checkout` key in `.btrain/project.toml`.

Recommended cadence:

| When | Command | Why |
|------|---------|-----|
| Before starting a new lane | `kkg sync-check` | Catch drift before you lock files |
| Before handoff (`pre-handoff` skill) | `kkg sync-check` | Flag upstream changes reviewers should know about |
| Weekly (or after major upstream merges) | `kkg sync-check --source-dir /path/to/fork` | Keep fork in sync |
| After `kkg index` | `kkg sync-check` | Verify the graph reflects the latest code |

For automated cadence, wire `kkg sync-check` into a CI step or a
`PostToolUse` hook that runs on `git pull` / `git merge`.

### Visual configuration

Run `kkg viz-dashboard` and click the **Standards** tab to configure rules interactively:
- See rules as a graph organized by category
- Click any rule to read its evidence and change its severity
- Toggle entire categories on/off
- Switch between presets (default, strict, SOC 2, minimal)
- Export your config as TOML

## Embedding providers

| Provider | Model | Dimensions | Requires |
|----------|-------|-----------|----------|
| `local` (default) | jina-embeddings-v2-base-code | 768 | Nothing (runs on CPU) |
| `voyage` | voyage-code-3 | 1024 | `VOYAGE_API_KEY` |
| `openai` | text-embedding-3-large | 3072 | `OPENAI_API_KEY` |

## Architecture

```
src/
  codegraphcontext/          # Graph indexer, KuzuDB driver, parsers (upstream)
  codegraphcontext_ext/      # KeplerKG extensions
    commands/                # CLI: search, review-packet, blast-radius, drift-check,
                             #      advise, audit, embed, viz-*, export, sync-check
    standards/               # Standards engine: YAML rule loader + Cypher runner
    embeddings/              # Embedding pipeline (providers, schema, runtime)
    hybrid/                  # Hybrid retrieval (ANN search + graph traversal)
    daemon/                  # Warm daemon (Unix socket server)
    config.py                # Config layer: reads [cgraph] from project.toml
    preflight.py             # Fail-closed storage check (zombie mount guard)
    viz_server.py            # HTTP server for dashboard + projector
standards/                   # 12 YAML rule definitions + _exemptions.yaml
schemas/                     # JSON Schema for every command output
scripts/hooks/               # Claude Code enforcement hook scripts
tests/                       # 575+ tests
```

**Graph store:** KuzuDB (embedded, no server). 18 node tables, 7 relationship groups, HNSW indexes for ANN search.

**Node uid format:** `{name}{absolute_path}{line_number}` — e.g., `authenticate/Users/dev/project/src/auth.py42`. Search results include both `file` (`relative_path:line`) and `relative_path` (`relative_path`) fields for programmatic consumers.

**Agent integration:** Every command emits structured JSON to stdout with a `kind` discriminator and stable schema. Agents pipe command output directly into their context — no parsing needed.

**Setup validation:** Run `kkg doctor` to check backend, DB access, graph health, CALLS edges, and embeddings in one command.

**Standalone-safe:** KeplerKG works without btrain. The config layer falls back to sensible defaults when no `.btrain/project.toml` exists.

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
