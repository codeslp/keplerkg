# Spec 001 — cgraph: a btrain-native hybrid GraphRAG layer

**Status:** draft v0.1
**Owner:** codeslp
**Last updated:** 2026-04-16
**Upstream:** [CodeGraphContext/CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) (MIT, forked at [codeslp/cgraph](https://github.com/codeslp/cgraph))

---

## 1. Why

btrain coordinates multiple AI agents across lanes with file locks and handoffs. Its weakest moments are exactly the moments where raw text is the wrong representation:

- **Lane claims** — agent picks `--files "src/auth/"` without knowing every transitive caller.
- **Reviews** — reviewer reads raw diff + hunts through files to reconstruct blast radius.
- **Drift** — locked files change, upstream callers break, nobody notices until merge.
- **Context fetching** — agent reads 10 files to understand "the auth flow" before editing one.

Today all four situations consume tokens linearly with code size. With a structural graph of the codebase, the same answers come back in hundreds of tokens instead of tens of thousands.

**cgraph** is a thin, forked, btrain-aware distribution of CodeGraphContext (CGC) that adds a hybrid retrieval CLI and four btrain-facing subcommands. It does not replace btrain. It makes btrain's guardrails sharper and its handoffs cheaper.

## 2. Non-goals

- Not an MCP server. Upstream's MCP surface is left intact for merge hygiene but is neither documented nor depended on.
- Not a new graph database. We use KùzuDB via upstream's existing backend.
- Not a parser. Tree-sitter parsing is upstream's job.
- Not a replacement for `git diff`. cgraph augments; it does not compete on commit-level operations.
- Not hard-failing guardrails. Every check is soft-warn in v1. Hard fails only land after soft-warn data shows low false-positive rates.

## 3. Architecture

### 3.1 Direction of knowledge (decision 1a)

```
  btrain CLI  ──shells out──►  cgraph CLI  ──reads──►  KùzuDB (on-disk)
     │                                │
     └──── passes lane/files/paths ───┘
```

btrain knows about cgraph. cgraph does not know about btrain. This keeps cgraph reusable in non-btrain repos and lets btrain evolve its state format without breaking cgraph.

### 3.2 The adapter

A thin module inside **btrain**, not cgraph: `src/brain_train/cgraph_adapter.mjs`. Its job:

- Locate the `cgraph` binary (honor `[cgraph].bin_path` in `.btrain/project.toml`, else `$PATH`).
- Translate btrain state (locked files, lane id, base ref) into cgraph CLI arguments.
- Parse cgraph's stdout JSON.
- Degrade gracefully: if `cgraph` is missing or errors, btrain prints "cgraph unavailable — skipping advisory" and continues. **cgraph is never a hard dependency of btrain.**

### 3.3 Output contract

Every cgraph subcommand follows the same I/O discipline:

- **stdout** = one JSON object or JSON-lines stream. Nothing else.
- **stderr** = human-readable progress, warnings, errors.
- **exit code** = 0 on success, 1 on user error, 2 on internal error. Soft-warnings are reported *inside* the JSON, not via exit code.

This is non-negotiable. Agents consuming cgraph must be able to pipe stdout directly into a parser without stripping prose.

## 4. Command surface (MVP)

### 4.1 Inherited from upstream (documented, unchanged)

- `cgraph index [path]` — full parse, populates KùzuDB.
- `cgraph watch [path]` — daemon, incremental updates on file change.

### 4.2 New in cgraph

#### `cgraph context <query> [--lane <id>] [--k <n>] [--depth <n>] [--json]`

Hybrid retrieval. Runs ANN vector search for top-`k` (default 8) semantically relevant function/class nodes, then traverses `depth` hops (default 1) of CALLS / IMPORTS / DEFINED_IN edges. Emits:

```json
{
  "query": "auth flow",
  "seeds": [{"id": "...", "name": "verify_token", "file": "src/auth.py:42", "score": 0.81}, ...],
  "neighborhood": {
    "callers": [...],
    "callees": [...],
    "imports": [...]
  },
  "token_estimate": 1840
}
```

Replaces the "read 10 files to ground yourself" pattern.

#### `cgraph review-packet [--base <ref>] [--head <ref>] [--files <paths>] [--include-untracked] [--include-staged] [--include-workdir]`

Generates the reviewer's JSON. **Base and head are both optional** so the command works when btrain hands off without a usable diff (see §4.3). Output:

```json
{
  "source": "diff",                       // "diff" | "staged" | "workdir" | "untracked" | "locked_files"
  "base": "abc123",                       // null if source != "diff"
  "head": "def456",                       // null if source != "diff"
  "diff_stats": {"files": 4, "additions": 87, "deletions": 12},
  "touched_nodes": [...],
  "callers_not_in_diff": [{"name": "pay_out", "file": "...", "untested": true}, ...],
  "callees_not_in_diff": [...],
  "cross_module_impact": ["billing", "notifications"],
  "advisories": [
    {"level": "warn", "kind": "untested_caller", "detail": "..."},
    {"level": "warn", "kind": "stale_index", "detail": "3 locked files newer than graph index"}
  ]
}
```

The `source` field tells the reviewer how the packet was built so they're never misled by an empty payload. A reviewer reading this packet has the blast radius in ~2KB without opening a file.

### 4.3 Diff-absent modes (fallback hierarchy)

`cgraph review-packet` walks a fallback chain until it produces non-empty content. Each step is labeled in the output `source` field so the reviewer always knows what they're looking at:

1. **`diff`** — `base..head` both resolve and produce a non-empty diff. Default path.
2. **`staged`** — no usable diff refs, but `git diff --cached` is non-empty. Uses index.
3. **`workdir`** — nothing staged, but `git diff` (working tree vs HEAD) is non-empty.
4. **`untracked`** — still nothing, but `git ls-files --others --exclude-standard` (filtered to `--files` if provided) finds files the graph knows. Synthesizes "touched_nodes" by looking up the whole file's node set.
5. **`locked_files`** — last resort. btrain adapter passed `--files` from the lane's lock list. Return graph neighborhood of those files as-is. Advisory: `kind: "no_diff_available"`, level `warn`.

At every step, cgraph emits advisories when the data is suspicious:

- **`missing_base_ref`** — `--base` unresolvable (deleted branch, prose-wrapped value, force-pushed). cgraph still falls through and reports what it can.
- **`empty_diff`** — refs resolved but `base..head` is empty. Triggers `staged` → `workdir` → `untracked` → `locked_files` fallback.
- **`untracked_only`** — all changed paths are untracked. Tells reviewer "this lane's work is not committed yet."
- **`stale_index`** — any file in the packet has an mtime newer than the KùzuDB's last-indexed timestamp. Tells reviewer the graph neighborhood may be incomplete. Suggests `cgraph index` or restart `cgraph watch`.
- **`excluded_by_cgcignore`** — ≥ 1 file in the diff matches `.cgcignore`. Prevents the silently-empty-packet failure mode.
- **`refs_diverged_from_main`** — `base` is not an ancestor of `head`; diff spans a merge. Packet is still correct but flagged.

**Key invariant:** `cgraph review-packet` never exits non-zero because of an "unusual" diff state. It always emits JSON, always sets `source`, always lists any advisories. The btrain adapter decides whether to surface them; cgraph's job is to report honestly.

### 4.4 Packet size cap

Review packets are capped to prevent unbounded output when a lane locks a broad scope with no diff. Cap applies to the `touched_nodes` + `callers_not_in_diff` + `callees_not_in_diff` sets combined.

- **Default cap:** 50 nodes. Configurable via `--max-nodes <n>` or `[cgraph.review_packet].max_nodes` in `.btrain/project.toml`.
- **When hit:** output includes `"truncated": true`, `"total_nodes": 247`, `"returned_nodes": 50`. The truncation is not silent.
- **Selection when truncated:** prefer nodes with highest in-degree (most callers) on the assumption that high-fan-in code is higher-stakes to review.
- **Estimated hit rate:** ~3-5% of all review-packets, concentrated on broad-lock lanes. Telemetry in §11 will confirm.

Every truncated packet carries a **context-aware workaround advisory** with `kind: "packet_truncated"` and a suggestion chosen by the fallback source that fired:

| `source` at truncation | `suggestion` field |
| --- | --- |
| `locked_files` + `untracked_only` advisory | `"Commit or stage your changes and re-run — the packet will narrow to just what you touched."` |
| `locked_files` + no diff-related advisory (lane just claimed, no edits) | `"Once you start editing, re-run with --include-workdir to scope to changed files only."` |
| `locked_files`, broad scope, commits exist | `"Narrow with --files <subpath>, or query by intent: cgraph context '<topic>'."` |
| `workdir` / `staged` / `untracked` (rare — means uncommitted change set itself is huge) | `"Commit in smaller logical chunks, or narrow with --files <subpath>."` |

The suggestion is embedded as plain text in the JSON so the consuming agent can surface it directly without translation.

#### `cgraph blast-radius --files <paths> [--lane <id>]`

Pre-lock collision check. Expands the requested lock set through the graph and reports:

- Transitive callers / callees outside the requested paths.
- Overlap with other active btrain lanes (btrain adapter passes the lock table in).
- Advisory level per overlap (soft-warn v1).

#### `cgraph drift-check --lane <id> --since <timestamp>`

Has anything in the lane's locked files' graph neighborhood changed outside the lane since `--since`? Used by btrain to warn owners mid-lane that upstream or sibling code shifted under them.

#### `cgraph sync-check` (decision 4)

Runs `git fetch upstream` in the cgraph repo itself and reports commits on `upstream/main` not yet merged into `origin/main`. Never auto-merges. Output:

```json
{
  "upstream": "CodeGraphContext/CodeGraphContext",
  "local_head": "...",
  "upstream_head": "...",
  "behind_by": 14,
  "new_commits": [{"sha": "...", "subject": "..."}, ...]
}
```

A quarterly cron or manual `cgraph sync-check` keeps the fork fresh on the user's schedule.

#### `cgraph advise --situation <kind> [--context <json>]`

The agent-advisory surface (your #2 addition). btrain calls this when it detects a situation where graph reasoning beats git. Returns a short, concrete recommendation:

```json
{
  "situation": "lock_overlap",
  "suggestion": "Run `cgraph blast-radius --files src/auth/ --lane b` — lane A holds locks on 3 transitive callers.",
  "rationale": "git diff won't show you who calls verify_token; the graph does."
}
```

btrain surfaces these as one-line tips in its own output. Agents see them inline and can choose to run the named cgraph command.

**Latency budget: < 200ms per call.** `cgraph advise` is a lookup, not a query — it reads a precomputed advisory table keyed by `situation`. If a given situation requires graph traversal to compute, that traversal is cached or done ahead of time by the relevant heavier command (`blast-radius`, `drift-check`). `advise` itself never triggers a full graph walk. If cgraph cannot respond inside 200ms, the btrain adapter times out silently and proceeds without the tip.

## 5. btrain integration points

These are the triggers inside **btrain** (not cgraph) that shell out to cgraph. Each is a one-liner the adapter wraps.

| btrain event | cgraph call | What the agent sees |
| --- | --- | --- |
| `btrain handoff claim --files X` | `cgraph blast-radius --files X --lane <id>` | Soft-warn if any listed file has high-fan-in callers outside X. |
| `bth` output when `status: needs-review` | `cgraph review-packet --base <base> --head <head>` | Reviewer sees "cgraph review packet available — run: cgraph review-packet ..." as a tip. |
| `pre-handoff` skill, after diff gate | `cgraph review-packet ...` | Fails soft if `untested_caller` advisories exist. Never blocks; prints advisory. |
| `btrain doctor` | `cgraph sync-check` | Reports if cgraph fork is behind upstream. |
| `btrain handoff` when two lanes' locks have graph overlap | `cgraph advise --situation lock_overlap` | One-line tip in btrain stdout. |
| Agent runs `btrain status` | `cgraph advise` per active lane (pull model, see §5.3) | Surfaces all currently-active advisories across every active lane, deduped per §5.1. |

**Every integration is opt-in** via `[cgraph]` in `.btrain/project.toml`:

```toml
[cgraph]
enabled = true
bin_path = "cgraph"                               # default; can point at custom install
db_path = "/Volumes/SSD/.cgraph/db"               # decision 3 — external SSD supported
model_cache = "/Volumes/SSD/.cache/jina"
advise_on = ["lock_overlap", "drift", "review_ready", "packet_truncated"]
advise_on_resolution = false                      # opt-in: show one-liner when a condition resolves

# Per-lane overrides (optional). Absent lanes inherit project defaults.
[cgraph.lanes.hotfix]
disable_advise = true                             # urgent hotfix lane wants silence

[cgraph.lanes.a]
advise_on = ["lock_overlap"]                      # only this category for lane a
```

When `enabled = false` or the section is absent, btrain behaves exactly as today.

### 5.1 Advisory lifecycle (condition-scoped, de-duplicated)

An advisory is surfaced **once** when its underlying condition transitions from absent → present for a given lane, and suppressed while the condition remains continuously present. When the condition resolves, the entry is closed. If the same condition reappears later (new transition), it surfaces again.

This means: no spam while a long-running condition persists, but no lost signal when a new one appears.

**State model.** Each advisory is keyed by `(lane_id, kind, context_hash)`, where `context_hash` uniquely identifies the situation (e.g. for `lock_overlap`: hash of the overlapping lane-id + overlapping node set). Two entries with the same `(lane, kind)` but different `context_hash` are distinct — e.g. lane `b` overlapping lane `a` on `verify_token` is a different advisory from lane `b` overlapping lane `c` on `pay_out`.

**State file:** `.btrain/cgraph-advisory-state.jsonl` (btrain-owned). One line per active advisory:

```json
{"lane": "b", "kind": "lock_overlap", "context_hash": "ab12…", "first_seen": "2026-04-16T12:00Z", "last_surfaced": "2026-04-16T12:00Z", "resolved_at": null, "detail": "…"}
```

**Lifecycle rules per btrain advisory-surfacing event** (table in §5):

1. Adapter asks cgraph for current conditions for the lane (e.g. `cgraph blast-radius --lane b`).
2. For each condition returned, compute its `context_hash`.
3. Match against state file:
   - **No matching active entry** → write new entry, surface the advisory (set `last_surfaced` = now).
   - **Matching active entry exists** → suppress (do not surface). Do not update `last_surfaced`.
4. For every active entry whose condition is **no longer** present → set `resolved_at` = now. If `advise_on_resolution = true` (opt-in), surface a one-line resolution tip: `"cgraph: lock_overlap on verify_token with lane a is resolved — you're clear."`. Default is `false` to minimize noise. Keep closed entries for telemetry regardless (see §5.2).
5. If the lane is resolved (`btrain handoff resolve`), close all still-active entries for that lane with `resolved_at` = resolution time.

**Why this shape:**
- Matches the "show while condition exists, stop when it's over" requirement literally.
- Deterministic — same (lane, kind, context_hash) always dedupes.
- Safe on crash — state is append-mostly JSONL, rebuildable from the telemetry log if corrupted.
- Cheap — reading the state file is O(active advisories), typically < 10 lines per lane.

### 5.2 Telemetry log

Separate from state, append-only: `.btrain/logs/cgraph-advisories.jsonl`. One line per surface event AND one per resolve event. Used for the success metrics in §11 (false-positive rate, agent adoption rate). Never read by the lifecycle logic.

### 5.3 Cross-lane surfacing (pull model)

Problem: lane-A holds locks. Lane-B claims with overlap → lane-B is warned by the claim-time check. Lane-A has no idea B is encroaching. Without a fix, half of every cross-lane situation goes unseen by the lane that was "there first."

**Decision: pull model.** `btrain status` aggregates advisories across every active lane, not just the caller's.

- When any agent runs `btrain status`, the adapter iterates active lanes and calls cgraph per-lane.
- Output groups advisories by lane: `lane a: lock_overlap with lane b on verify_token`, `lane b: drift on src/auth/`, etc.
- The §5.1 lifecycle applies unchanged — each `(lane, kind, context_hash)` surfaces once when it first appears and then stays quiet. Pull model just widens *which* lanes get evaluated at each `btrain status` call.
- No writes to other lanes' state files. A single status call may surface new advisories across multiple lanes; each lane's state entries are written atomically.

Why pull over push: zero cross-lane coupling, no mid-lane interrupts, agents see the information when they naturally check status, and `btrain status` becomes the canonical "situational awareness" surface.

Caveat: an agent who never runs `btrain status` on their own lane won't see cross-lane advisories. Acceptable in v1 since `btrain status` is already part of the recommended `bth` flow; if telemetry shows agents skip it, we revisit.

## 6. Embeddings (decision 3)

### 6.1 Model choice

Code is not English prose. General-purpose sentence-transformers (MiniLM et al.) are trained on web text and cap out at 256 tokens — they miss long function bodies and don't bridge natural-language queries to code identifiers. Default should be a **code-specific model**.

| Tier | Model | Size | Dims | Context | When to use |
| --- | --- | --- | --- | --- | --- |
| **Default (local, code-specific)** | `jinaai/jina-embeddings-v2-base-code` | ~640MB | 768 | 8192 | Offline, private, most repos |
| **API (best quality, paid)** | `voyage-code-3` | — | 1024 | 32000 | Teams OK with Voyage/Anthropic API calls |
| **Small-local fallback** | `BAAI/bge-small-en-v1.5` | ~130MB | 384 | 512 | Constrained-disk environments only |
| **Deprecated** | `all-MiniLM-L6-v2` | ~90MB | 384 | 256 | Not recommended; listed only because it's ubiquitous |

- **Default:** `jina-embeddings-v2-base-code` (local). Code-trained, 8K context handles real-world function lengths, open weights, CPU-runnable.
- **Configurable:** via `[cgraph.embedding]` — `model`, `provider` (`local` | `voyage` | `openai`), `dimensions`.
- **Storage impact:** 10K functions × 768 dims × 4 bytes ≈ 30MB. Negligible.
- **Rationale documented:** embedding-choice rationale plus a `cgraph eval-embeddings` command (out-of-MVP, §12 Q4) so teams can A/B on their own corpus.

### 6.2 Storage and privacy

- Model cache and DB dir both take explicit paths. External SSD supported (see §5 `db_path` / `model_cache`). Caveat documented in README: USB-3 degrades ANN latency; Thunderbolt/USB-4 is fine.
- **API providers opt-in only.** Local provider stays the shipping default so code never leaves the machine unless the user flips the switch.

### 6.3 Ingestion

- `cgraph embed` command (new) walks existing Function/Class nodes in KùzuDB, generates vectors, writes them back via `ALTER TABLE ... ADD embedding FLOAT[768]` (dimensionality derived from configured model, not hardcoded). Idempotent.
- Changing model = re-embed. `cgraph embed --force` triggers full re-vectorization. Dimensions are detected from the column schema; mismatch fails loudly.
- **Automation:** a `cgraph watch --with-embeddings` flag extends upstream's watcher to vectorize new/changed nodes incrementally. Alternatively, a post-commit git hook template ships in the repo.

## 7. Guardrails (decision 5: soft-warn only in v1)

Every advisory has three fields: `level` (always `warn` in v1), `kind`, `detail`. No guardrail blocks an agent action in v1. All land inside cgraph output JSON; btrain surfaces them verbatim.

Initial advisory kinds:

- `lock_overlap` — requested lock intersects another lane's graph neighborhood.
- `untested_caller` — diff touches a function whose callers have no tests.
- `cross_module_impact` — diff crosses a module boundary (heuristic: shared-prefix count).
- `drift` — files in lane's neighborhood changed outside the lane.
- `stale_memory` — `MEMORY.md` references symbols no longer in the graph (future).
- `no_diff_available` — review-packet fell all the way through to `locked_files` source.
- `missing_base_ref` — handoff's `Base` field unresolvable (deleted / force-pushed / prose-wrapped).
- `empty_diff` — base and head resolved, but diff is empty. Often a superseded or no-op lane.
- `untracked_only` — all changed paths are untracked files.
- `stale_index` — locked files have mtime newer than last graph index.
- `excluded_by_cgcignore` — ≥ 1 diff file excluded from the graph; packet may mislead.
- `refs_diverged_from_main` — `base..head` spans a merge; reviewer should check merge commits.
- `packet_truncated` — review-packet exceeded `max_nodes`; carries a context-aware `suggestion` field (see §4.4).

Before promoting any advisory to hard-fail we want ≥ 30 days of telemetry (JSONL log of `warn` events + agent outcome) showing < 5% false-positive rate.

## 8. Upstream-sync policy (decision 4)

- Fork relationship is the sync primitive. `origin = codeslp/cgraph`, `upstream = CodeGraphContext/CodeGraphContext`.
- Modifications live in `src/cgraph/` (new directory) so upstream's `src/` stays merge-clean.
- When upstream files must be modified, prefer subclassing/wrapping over editing in place.
- `cgraph sync-check` surfaces new upstream commits; user decides when to `git merge upstream/main`.
- Monthly minimum cadence. Major upstream version jumps (0.x → 0.y) get a checklist.

## 9. Repo layout

```
cgraph/
├── src/                        # upstream (untouched where possible)
├── src/cgraph/                 # our additions
│   ├── commands/
│   │   ├── context.py
│   │   ├── review_packet.py
│   │   ├── blast_radius.py
│   │   ├── drift_check.py
│   │   ├── advise.py
│   │   └── sync_check.py
│   ├── hybrid/
│   │   ├── ann.py              # KùzuDB HNSW query wrapper
│   │   └── traverse.py         # Cypher traversal helpers
│   ├── embeddings/
│   │   └── providers.py        # local, voyage, openai
│   └── io/
│       └── json_stdout.py      # enforce output contract
├── specs/                      # this file lives here
├── tests/cgraph/               # our tests
└── README.md                   # upstream; we add a "cgraph additions" section
```

All new code lives under a clearly-namespaced `cgraph/` subdirectory. Upstream merges never touch our code.

## 10. Phases

**Phase 0 — Scaffolding (week 1)**
- Confirm upstream builds and indexes this repo locally.
- Add `src/cgraph/` skeleton, CI passes.
- Ship `cgraph sync-check` (simplest; validates output contract).

**Phase 1 — Hybrid retrieval (week 2-3)**
- `cgraph embed` + schema ALTER.
- `cgraph context <query>` end-to-end with local embeddings.
- Tests: retrieval recall on a small fixture repo, JSON shape validation.

**Phase 2 — Review packet + blast radius (week 3-4)**
- `cgraph review-packet` against a real btrain lane's diff.
- `cgraph blast-radius` with lane-lock awareness via adapter.
- Measure: tokens-per-review vs. baseline on 10 recent btrain handoffs.

**Phase 3 — btrain adapter + advisories (week 4-5)**
- `src/brain_train/cgraph_adapter.mjs` lands in btrain (separate PR on btrain repo).
- `[cgraph]` config section supported in `.btrain/project.toml`.
- `cgraph advise` wired into `btrain handoff`, `bth`, `pre-handoff`.
- Advisory JSONL log for telemetry.

**Phase 4 — Drift and polish (week 5-6)**
- `cgraph drift-check` plus `btrain status` integration.
- README overhaul: distinguish upstream commands from cgraph additions.
- First `cgraph sync-check` cadence documented.

**Out of scope for MVP, tracked separately:**
- Jira / deployment / org-chart nodes in the graph (proposal §1). Code-only for now.
- Hard-fail guardrails.
- MCP server modifications.

## 11. Success metrics

All measured against btrain's last 30 handoffs, replayed with cgraph on vs. off.

1. **Review tokens**: ≥ 40% reduction in tokens passed to reviewer model per handoff.
2. **Lock collisions caught pre-claim**: ≥ 1 real collision caught per week across active lanes.
3. **Agent adoption of advisories**: ≥ 50% of advise-tips acted on (measured by subsequent cgraph command invocation).
4. **False-positive rate on soft-warns**: < 10% (agent runs the suggested command and finds nothing).
5. **Upstream sync lag**: never more than 30 days behind `upstream/main`.

## 12. Open questions

- **Q1:** Does btrain's `handoff_history` format have enough structure for `cgraph review-packet` to derive base/head refs without re-parsing the handoff file? The §4.3 fallback chain tolerates missing refs, so this is a polish question — worth a small `btrain handoff refs --lane <id>` helper in a later btrain PR. **Open, low priority.**
- **Q2:** ~~Cross-lane advisory surfacing~~ → **Closed.** Pull model via `btrain status` per §5.3.
- **Q3:** ~~Advisory telemetry storage~~ → **Closed.** btrain-owned at `.btrain/logs/cgraph-advisories.jsonl`.
- **Q4:** Embedding provider for repos with highly domain-specific jargon — Jina v2 code is strong on general code but may underperform on e.g. legal or medical domain-specific jargon. Ship a small eval harness (`cgraph eval-embeddings`) so teams can A/B providers on their own corpus. **Out of MVP scope.**
- **Q5:** Graph-backed `MEMORY.md`? Today MEMORY.md accumulates narrative. A `cgraph memory-refresh` command could regenerate the "current state" section from the graph. Exciting but big — **phase 5+.**

## 13. Decisions recorded

| # | Decision | Chosen |
| --- | --- | --- |
| 1 | Integration direction | (a) btrain-knows-cgraph, with btrain-side adapter |
| 2 | MVP priorities | Review packets, blast-radius, pre-handoff gate, context fetch — plus agent-advisory tips for dysfunction situations |
| 3 | Embeddings | Local default `jina-embeddings-v2-base-code` (code-specific, 8K ctx, 768-dim); swappable provider; paths configurable (external SSD supported); `voyage-code-3` as API upgrade path |
| 4 | Upstream sync | User-gated via `cgraph sync-check`; never auto-merge |
| 5 | Guardrail tone | Soft-warn only in v1; hard-fail requires telemetry |
