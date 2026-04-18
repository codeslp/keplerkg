# Spec 001 — Implementation Progress

**Spec:** [001-btrain-integration.md](001-btrain-integration.md)
**Last updated:** 2026-04-17

---

## Phase 0 — Scaffolding — COMPLETE

- [x] Confirm upstream builds and indexes this repo locally
- [x] Add `src/codegraphcontext_ext/` skeleton and `schemas/` directory
- [x] CI workflow at `.github/workflows/cgraph.yml` (49 lines)
- [x] Ship `cgc sync-check` — 259 lines implemented; 316 lines of tests (`test_sync_check.py` + `test_sync_check_internals.py`)
- [x] JSON Schemas as stubs — `schemas/context.json` (65 lines) and `schemas/sync-check.json` (84 lines) are populated; `advise`, `blast-radius`, `drift-check`, `review-packet` are 7-line stubs

## Phase 1 — Hybrid Retrieval — COMPLETE

### KùzuDB driver (upstream, enhanced)

- [x] `core/database_kuzu.py` — 627-line singleton manager with thread-safe connection pooling, retry logic, and Neo4j→KùzuDB Cypher dialect translator
- [x] 18 node tables: Repository, File, Directory, Module, Function, Class, Variable, Trait, Interface, Macro, Struct, Enum, Union, Annotation, Record, Property, Parameter
- [x] 7 relationship groups: CONTAINS, CALLS, IMPORTS, INHERITS, HAS_PARAMETER, INCLUDES, IMPLEMENTS
- [x] Schema is idempotent — auto-created on first connection

### Embedding pipeline

- [x] `embeddings/schema.py` (98 lines) — idempotent `ALTER TABLE ... ADD embedding FLOAT[N]` + HNSW index creation for Function and Class tables
- [x] `embeddings/providers.py` (131 lines) — 3 providers: `LocalProvider` (jinaai/jina-embeddings-v2-base-code, 768-dim), `VoyageProvider` (voyage-code-3, 1024-dim), `OpenAIProvider` (text-embedding-3-large, 3072-dim)
- [x] `embeddings/runtime.py` (210 lines) — config resolution (CLI > env > defaults), KùzuDB-only backend probe, provider availability checks
- [x] `embeddings/_upstream.py` (72 lines) — lazy-loads private helpers from `codegraphcontext.core`

### Commands

- [x] `cgc embed` — `commands/embed.py` (250 lines). Batch processing (64 nodes/round-trip), idempotent (skips nodes with existing embeddings unless `--force`), auto-creates schema columns + HNSW indexes
- [x] `cgc context <query>` — `commands/context.py` (142 lines). Embeds query via selected provider, runs ANN search (top-k), traverses CALLS/IMPORTS neighborhood, emits JSON with token estimate

### Hybrid retrieval layer

- [x] `hybrid/ann.py` (62 lines) — `HNSW_SEARCH` across Function and Class embeddings, distance-to-similarity scoring, merged top-k results
- [x] `hybrid/traverse.py` (100 lines) — variable-depth CALLS/IMPORTS traversal from seed nodes, deduped neighborhood output

### Tests

- [x] `test_embed.py` (513 lines) — backend probing, provider availability, config resolution, mock DB embed pipeline
- [x] `test_context.py` (268 lines) — end-to-end context query tests
- [x] `test_schema_check.py` (121 lines) — JSON schema validation harness
- [x] `test_upstream_coupling.py` (61 lines) — upstream helper coupling tests
- [x] Additional test files: `test_command_metadata.py`, `test_json_stdout.py`, `test_scaffold.py`, `test_viz.py` — 1,623 total test lines across 12 files in `tests/cgraph_ext/`

## Phase 1.5 — Storage migration to `/Volumes/zombie` — COMPLETE

Operational stage wedged between Phase 1 (KùzuDB + embeddings live but on internal drive) and Phase 2 (review-packet starting to read heavily from the graph). All 7 steps done: storage moved, env set, shell wrapper shipped, Python preflight wired into `get_kuzu_connection()`, and smoke test passed on 2026-04-18.

- [x] **Step 1 — Writer check.** Migration lane completed without leaving Kùzu files on the internal drive; `~/.codegraphcontext/global/db/` now contains only the unused falkordb files.
- [x] **Step 2 — Target dir.** `/Volumes/zombie/cgraph/db` exists and contains the live Kùzu store.
- [x] **Step 3 — Move store.** `kuzudb` and `kuzudb.wal` live at `/Volumes/zombie/cgraph/db/`; no Kùzu files remain under `~/.codegraphcontext/global/db/`.
- [x] **Step 4 — Update upstream `.env`.** `~/.codegraphcontext/.env` contains `KUZUDB_PATH=/Volumes/zombie/cgraph/db/kuzudb`.
- [x] **Step 5 — Export `HF_HOME`.** `scripts/cgraph-env.sh` is the canonical wrapper; it exports `HF_HOME`/`SENTENCE_TRANSFORMERS_HOME` to `/Volumes/zombie/cgraph/hf-cache` and fails closed when zombie is not mounted.
- [x] **Step 6 — End-to-end smoke test.** 2026-04-18: `kkg search "authentication token validation"` ran successfully — Jina v2 model loaded from `/Volumes/zombie/cgraph/hf-cache`, KùzuDB connected at `/Volumes/zombie/cgraph/db/kuzudb`, query embedded and ANN search executed. Preflight also verified: `check_storage()` correctly returns `storage_offline` when pointed at an unmounted `/Volumes/fakemount`.
- [x] **Step 7 — Fail-closed preflight.** `src/codegraphcontext_ext/preflight.py` — `check_storage()` inspects `KUZUDB_PATH` (env → upstream config) and `HF_HOME`; if either path lives under an unmounted `/Volumes/*` mount-point, returns `{"ok": false, "kind": "storage_offline", ...}`. `require_storage()` exits with that JSON. Wired into `io/kuzu.py:get_kuzu_connection()` so every DB-touching command fails closed. 13 tests in `test_preflight.py`.

Cross-cuts into other phases — see annotations in Phase 3, Phase 4, and Phase 6 below.

## Phase 2 — Review Packet + Blast Radius — IN PROGRESS

- [x] `cgc review-packet` — `commands/review_packet.py` (21.8K) with `schemas/review-packet.json`; lane d is currently back in-progress after codex review findings on missing advisory coverage
- [x] `cgc blast-radius` — `commands/blast_radius.py` (611 lines). Transitive caller/callee graph expansion, cross-module impact, lane lock overlap detection, truncation by in-degree ranking. 629 lines of tests in `test_blast_radius.py`; schema at `schemas/blast-radius.json` (124 lines)
- [ ] Replay harness (Node script for §11 metrics)
- [ ] Measure tokens-per-review vs. raw-diff baseline on 10 recent handoffs

### Phase 2+ helpers (outside spec 001's phase plan, shipped early)

- [x] `cgc viz-embeddings` — `commands/viz_embeddings.py` (8.2K) — HTML visualization over KùzuDB embeddings. Lane a is resolved.
- [x] `cgc viz-graph` — `commands/viz_graph.py` (10K) — HTML visualization over the graph itself. Same lane.
- Spec 001 does not plan these; they arrived ahead of Phase 4 polish. Worth a small spec amendment if they stay.
- [x] **Spec 003 — KeplerKG Rename + Viz Polish** — [specs/003-kepler-viz-polish.md](003-kepler-viz-polish.md). **COMPLETE.** All six items shipped: KeplerKG rename, edge visibility (thicker lines + green Contains), collapsible help ribbons on 2D/3D/Embeddings, redundant banner strip removed, color-mapper clobber fix, and About modal with credits.
- [ ] **Spec 004 — Multi-Repository Targeting** — [specs/004-multi-repo-targeting.md](004-multi-repo-targeting.md). **PLANNED.** Per-project KùzuDB stores under `/Volumes/zombie/cgraph/db/<slug>/` so cgraph can be pointed at arbitrary codebases (Flask first) without ANN leakage or embedding-dimension collisions. Phase A resolver + `--project` flag on all 10 ext commands; Phase B migrate cgraph's own store to `db/cgraph/`; Phase C Flask smoke. Spec 005 (insight heuristics) hands off from here.

## Phase 3 — btrain Adapter + Advisories + Warm Daemon — NOT STARTED

- [ ] `cgraph_adapter.mjs` in btrain repo (timeout budgets, parallel fanout, advisory-file-lock)
- [ ] `[cgraph]` config section in `.btrain/project.toml`
- [ ] **cgraph config layer owning `db_path` + `model_cache`.** Per spec 001 §5, these are cgraph-level keys. Today cgraph has no config plumbing — it calls upstream's `KuzuDBManager()` directly and inherits upstream's `KUZUDB_PATH` resolution. Phase 3 ships a small `src/codegraphcontext_ext/config.py` that reads `.btrain/project.toml [cgraph]` and (a) exports `KUZUDB_PATH` + `HF_HOME` into the subprocess env before every command, and (b) fails closed via the Phase 1.5 Step 7 preflight helper when the configured paths are on an unmounted volume.
- [ ] `cgc advise` implementation — currently a 6-line scaffold
- [ ] `cgc serve` warm daemon + launchd/systemd templates
- [ ] Advisory state file + telemetry log wired end-to-end

## Phase 4 — Drift + Polish — NOT STARTED

- [ ] `cgc drift-check` implementation — currently a 6-line scaffold
- [ ] `btrain status` pull-model integration
- [ ] README overhaul — includes a "Storage conventions" section documenting `/Volumes/zombie/cgraph/{db,hf-cache}` as the canonical layout, the `KUZUDB_PATH` + `HF_HOME` env vars, and the fail-closed preflight from Phase 1.5 Step 7.
- [ ] `cgc sync-check` cadence documented

## Phase 5 — Code-Quality Standards — NOT STARTED

- [ ] Standards loader + `standards/` directory (does not exist yet)
- [ ] 12 seed YAML rules per §6.5
- [ ] `cgc audit` CLI surface + `schemas/audit.json` (neither exists yet)
- [ ] `cgc snapshot` + `cgc index --incremental`
- [ ] Graph-role lifecycle (working/review per lane)

## Phase 6 — Agent-Write Enforcement Wiring — NOT STARTED

- [ ] `PostToolUse` + `Stop` hooks in `.claude/settings.json` — each hook invocation runs the Phase 1.5 Step 7 preflight first; if zombie is unmounted the hook exits 0 with `{"status":"storage_offline"}` rather than silently regenerating artifacts on internal.
- [ ] `pre-handoff` skill gate with `--require-hard-zero`
- [ ] CI regression gate in `.github/workflows/cgraph.yml` — CI's `cgc audit` call uses a tmpfs-backed kuzudb for the regression build (not zombie); document this exception so nobody is surprised when CI writes under `/tmp` rather than the configured path.
- [ ] Calibration tooling (`--calibration-report`)
- [ ] btrain worktree support (external dependency)

---

## Storage — where artifacts actually live

cgraph writes two large on-disk artifacts: the HuggingFace embedding-model cache (Jina v2 base-code, ~311 MB) and the KùzuDB graph store (~156 MB and growing). Spec 001 §5 and `memory/storage_zombie_drive.md` say both should live on `/Volumes/zombie`. Actual state as of 2026-04-17:

| Artifact | Spec target | Actual location | Size | Status |
|---|---|---|---|---|
| HuggingFace cache (Jina v2 base-code + bert-v2-qk-post-norm + transformer modules) | `/Volumes/zombie/cgraph/hf-cache/` | `/Volumes/zombie/cgraph/hf-cache/` | 311 MB | **Migrated.** This is the live cache target used by `scripts/cgraph-env.sh`. |
| KùzuDB graph + embeddings | `/Volumes/zombie/cgraph/db/` | `/Volumes/zombie/cgraph/db/kuzudb` (+ `.wal`) | 149 MB + 7.4 MB WAL | **Migrated.** No Kùzu files remain on the internal drive. |
| Upstream CGC falkordb files (unused by cgraph v1) | n/a (§6.3 is KùzuDB-only) | `~/.codegraphcontext/global/db/falkordb*` | 2.9 MB | Leave — not cgraph's concern. |

### How KùzuDB's path is resolved (upstream source of truth)

`core/database_kuzu.py:52-54` picks the store path by this precedence:

1. Explicit `db_path` argument to `KuzuDBManager()` — cgraph's `embed` passes nothing, so skipped.
2. `KUZUDB_PATH` environment variable — still optional; the wrapper does not export it directly.
3. `KUZUDB_PATH` value in `~/.codegraphcontext/.env` via upstream's `get_config_value` — **set** to `/Volumes/zombie/cgraph/db/kuzudb`.
4. Default: `~/.codegraphcontext/global/kuzudb`.

Today the live Kùzu store is the 149 MB file at `/Volumes/zombie/cgraph/db/kuzudb`, and upstream resolves that path through `~/.codegraphcontext/.env`. `~/.codegraphcontext/global/db/` no longer contains `kuzudb` or `kuzudb.wal`, so the internal-drive risk has shifted from "migration not done" to "mount preflight must keep future runs from silently recreating the store on internal when zombie is offline."

### Migration is a named stage

The 7-step migration plan lives under **[Phase 1.5 — Storage migration](#phase-15--storage-migration-to-volumeszombie--in-progress)** above so it is tracked alongside implementation phases and wired into downstream phases (Phase 3 config layer, Phase 4 README, Phase 6 preflight + CI exception). This section stays as the reality snapshot; execution lives in Phase 1.5.

### Spec-vs-reality gap

Spec 001 §5 names `[cgraph].db_path` and `[cgraph].model_cache` as cgraph-level config keys, but today cgraph has no config layer of its own — it calls upstream's `KuzuDBManager()` directly and path resolution happens inside upstream. Until Phase 3 ships `src/codegraphcontext_ext/config.py`, the only operational knob is upstream's `KUZUDB_PATH` env var and the shell's `HF_HOME`. Worth a small amendment to spec 001 §5 noting this — deferred to the Phase 3 lane so the amendment lands with the code that closes the gap, not before.

---

## Summary

| Phase | Status | Key artifacts |
|-------|--------|---------------|
| 0 — Scaffolding | **Complete** | ext skeleton, CI, sync-check, schema stubs |
| 1 — Hybrid Retrieval | **Complete** | embed, context, ANN search, graph traversal, 1,623 test lines, 149 MB live KùzuDB |
| **1.5 — Storage migration** | **Complete** | All 7 steps done: Kùzu on zombie, preflight wired into DB accessor, smoke test passed 2026-04-18 |
| 2 — Review + Blast Radius | **In progress** | review-packet (21.8K), blast-radius (611 lines + 629 test lines + schema); replay harness + baseline metrics remain |
| **Spec 004 — Multi-repo targeting** | Planned | [specs/004-multi-repo-targeting.md](004-multi-repo-targeting.md). Per-project KùzuDB stores, `--project` flag, Flask smoke. Unblocks "point cgraph at any codebase." |
| 3 — Adapter + Advisories | Not started | 6-line scaffold for advise; adapter lives in btrain; Phase 3 also delivers cgraph's config layer (replaces Phase 1.5's upstream-`KUZUDB_PATH` reliance) |
| 4 — Drift + Polish | Not started | 6-line scaffold for drift-check; README adds Phase 1.5 storage conventions |
| 5 — Standards | Not started | No standards/ dir, no audit command |
| 6 — Enforcement | Not started | Depends on phases 2-5; hooks gain Phase 1.5 Step 7 preflight; CI uses tmpfs kuzudb exception |

**Overall: Phases 0-1 complete. Phase 1.5 nearly complete (preflight shipped, Step 6 smoke remains). Phase 2 blast-radius and review-packet implemented; replay harness and baseline metrics remain before Phase 3 (btrain adapter) can start.**
