# Spec 004 — Multi-Repository Targeting (per-project KùzuDB stores)

**Parent:** [001-btrain-integration.md](001-btrain-integration.md)
**Progress doc:** [001-progress.md](001-progress.md)
**Status:** PLANNED
**Created:** 2026-04-17

---

## Why

Spec 001 assumes the graph holds a single codebase — cgraph's own — split across git refs (`main`, `working/<lane>`, `review/<lane>`) per §6.4. This spec extends the model so cgraph can be pointed at **arbitrary external codebases** (Flask, Redis, Django, CPython, …) and produce insights scoped to each one without cross-contamination.

Upstream already indexes arbitrary paths via `cgc index <path>` (14 languages, tree-sitter); the schema supports multiple `Repository` nodes keyed by path ([database_kuzu.py:102](../src/codegraphcontext/core/database_kuzu.py:102)). So nothing stops two repos from sharing one KùzuDB. But three properties of the current extension layer break under a shared store:

1. **ANN search is repo-blind.** [hybrid/ann.py:32](../src/codegraphcontext_ext/hybrid/ann.py:32) runs `hnsw_search` over Function/Class with no `Repository` filter. `cgc context "auth middleware"` against a shared store surfaces seeds from every repo ever indexed. Fixing this by filtering would thread repo scope through every query path (ann, traverse, context, review-packet, blast-radius, advise, viz-*). Per-repo stores sidestep the entire problem.
2. **Embedding column is dimension-locked.** `embedding FLOAT[N]` on Function/Class is fixed at column-creation time. Mixing Jina v2 (768) for Flask and Voyage-code-3 (1024) for Redis in one store is impossible without a second column per model.
3. **Re-index blast radius.** Re-embedding Flask after an upstream bump shouldn't touch Redis' HNSW index or WAL.

## Non-goals

- **Cross-repo queries.** Finding an anti-pattern that spans Flask + Django requires opening N DBs; deferred to a later fan-out layer.
- **UI project-switcher.** CLI-only in v1 (`--project <slug>` flag or `CGRAPH_PROJECT` env var).
- **Auto-detect which repo you're in.** Explicit is better. `basename($CWD)` fallback is opt-in, not default.
- **Migrating away from the global `~/.codegraphcontext/.env`.** That's Phase 3 of spec 001. This spec piggybacks on the env-var mechanism.

## Decisions

### 1. One KùzuDB per repo

Storage layout under the zombie-drive root established in 001 §Storage:

```
/Volumes/zombie/cgraph/db/
  cgraph/kuzudb + .wal      ← migrated from current global store
  flask/kuzudb + .wal       ← first external target
  redis/kuzudb + .wal       ← future
  …
```

### 2. Project identity = slug

A `<slug>` is a short kebab-case identifier, resolved in this precedence (first hit wins):

1. `--project <slug>` on the command line.
2. `CGRAPH_PROJECT` environment variable.
3. `project = "<slug>"` in a repo-local `.cgraph/project.toml` (if present at the cwd or any ancestor).
4. **Fallback:** `basename($CWD)` normalized to `[a-z0-9-]+`, with a stderr warning recommending an explicit slug.

Reserved slugs:
- `cgraph` — this repo's own store (migrated in Phase B).
- Empty / `default` / `global` — rejected; forces explicit choice.

### 3. Path resolution via `KUZUDB_PATH`

Upstream's `KuzuDBManager` is a singleton ([database_kuzu.py:19-32](../src/codegraphcontext/core/database_kuzu.py:19)) that resolves its path from `$KUZUDB_PATH` → `~/.codegraphcontext/.env` → default, in that order. Per-repo routing needs no upstream changes: each ext command sets `os.environ["KUZUDB_PATH"]` **before** touching upstream, and the singleton opens the right store.

Resolved slug `<slug>` → `/Volumes/zombie/cgraph/db/<slug>/kuzudb`. Directory is auto-created on first use.

### 4. Process-per-project in v1

Because the manager is a singleton, switching projects inside one process is undefined. v1 ships **"one project per process invocation"** — documented in every command's `--help`. A `KuzuDBManager.reset()` helper is a later concern (noted in "Open questions" below).

---

## Work items (ordered)

### Phase A — resolver + CLI wiring

- **A1.** `src/codegraphcontext_ext/project.py` — 4-tier resolver returning `(slug, db_path)`. ≤ 80 lines: CLI override → env → TOML (read top-level `project` key only, no schema validation) → basename fallback with warning.
- **A2.** Add `--project <slug>` option to every ext command in [cli.py](../src/codegraphcontext_ext/cli.py): `blast-radius`, `sync-check`, `embed`, `context`, `review-packet`, `viz-embeddings`, `viz-graph`, `viz-dashboard`, `viz-projector`, `export-embeddings`.
- **A3.** Each command sets `os.environ["KUZUDB_PATH"]` from the resolver **before** calling upstream. Resolver also ensures the target directory exists (`mkdir -p`).
- **A4.** Unit tests in `tests/cgraph_ext/test_project_resolver.py`:
  - CLI override beats env.
  - Env beats TOML.
  - TOML beats basename.
  - Reserved slugs rejected.
  - Non-existent slug auto-creates directory.
  - No leakage between two sequential invocations in the same test process (via subprocess, not in-process).

### Phase B — migrate cgraph's own store (one-shot)

- **B1.** `mkdir -p /Volumes/zombie/cgraph/db/cgraph` and `mv` the existing `kuzudb` + `kuzudb.wal` into it.
- **B2.** Update `~/.codegraphcontext/.env`: `KUZUDB_PATH=/Volumes/zombie/cgraph/db/cgraph/kuzudb` (or remove it and require `--project cgraph` / env var).
- **B3.** Update [001-progress.md §Storage](001-progress.md) path snapshot and the Phase 1.5 Step 4 reference.
- **B4.** Update `memory/storage_zombie_drive.md` layout.
- **B5.** Smoke: `cgc context --project cgraph "embedding runtime"` returns the same seeds as before the move.

### Phase C — Flask as first external target

- **C1.** Clone Flask to `/Volumes/zombie/cgraph/src/flask` (out-of-tree; not a cgraph submodule).
- **C2.** `cgc index --project flask /Volumes/zombie/cgraph/src/flask` (upstream's `index` inherits `KUZUDB_PATH` from env; needs no new flag, just needs cgraph's entrypoint to export it — if upstream's `index` is not wrapped by ext, this may require a wrapper — verify during Phase A).
- **C3.** `cgc embed --project flask`.
- **C4.** `cgc context --project flask "request context"` — produces JSON with >0 seeds, all paths under `/Volumes/zombie/cgraph/src/flask`.
- **C5.** `cgc viz-graph --project flask` opens a KeplerKG view scoped to Flask only.
- **C6.** Record byte-size of `db/flask/kuzudb`, embed latency, and seed counts in 001-progress.md.

### Phase D — insight seed (hand-off to spec 005)

Out of scope here. Once Flask is queryable under a scoped DB, the immediate next spec (005) is a first `cgc advise` implementation with candidate heuristics: deep call chains, god modules, circular imports, factory-vs-module-level `Flask(__name__)` patterns. Each is a Cypher query against the now-isolated Flask store.

---

## Risks / open questions

- **Upstream `cgc list`** lists whatever's in the current `KUZUDB_PATH`. A `cgc list --all-projects` would iterate the `db/` directory; defer until asked for.
- **`cgc watch`** holds the KuzuDB singleton open; switching projects while a watcher runs is undefined. Document: one watcher per shell.
- **Upstream commands** (`index`, `watch`, `list`, `analyze`, …) are wired directly to upstream's Typer app; they don't pass through [cli.py](../src/codegraphcontext_ext/cli.py). Phase A must decide: (a) add a thin ext wrapper that exports `KUZUDB_PATH` then delegates, or (b) trust the env var the user's shell already has set. Leaning (a) so `--project` works uniformly — to be confirmed during A2 implementation.
- **Singleton reset.** Mid-process project switching is undefined. A `KuzuDBManager.reset()` would unlock this but is a v2 concern.

## Done when

- All 10 ext commands accept `--project <slug>` and route to the correct store.
- `cgraph`'s own store migrated to `/Volumes/zombie/cgraph/db/cgraph/` with 001-progress.md updated.
- Flask indexed + embedded + queryable under `/Volumes/zombie/cgraph/db/flask/`, with `cgc context` and `cgc viz-graph` proven to return Flask-only results.
- Resolver test suite passes (Phase A4).
- `.cgraph/project.toml` example documented in README under a new "Targeting other codebases" section.
