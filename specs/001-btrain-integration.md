# Spec 001 — cgraph: a btrain-native hybrid GraphRAG layer

**Status:** draft v0.2
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

**cgraph** is a thin, forked, btrain-aware distribution of CodeGraphContext (CGC) that adds a hybrid retrieval CLI and six btrain-facing subcommands (`context`, `review-packet`, `blast-radius`, `drift-check`, `sync-check`, `advise`). It does not replace btrain. It makes btrain's guardrails sharper and its handoffs cheaper.

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

- Locate the `cgraph` binary (honor `[cgraph].bin_path` in `.btrain/project.toml`, else `$PATH`). Prefer the `cgraph serve` daemon socket if present (§3.4).
- Translate btrain state (locked files, lane id, base ref) into cgraph CLI arguments.
- Parse cgraph's stdout JSON, optionally validating against `schemas/<command>.json` in dev mode.
- Own the **advisory state machine** — read/write `.btrain/cgraph-advisory-state.jsonl` under the OS advisory file lock, append telemetry events to `.btrain/logs/cgraph-advisories.jsonl`. Full lifecycle rules in §5.1.
- Enforce per-command timeout budgets (§3.4).
- Degrade gracefully: if `cgraph` is missing, errors, or times out, btrain prints a one-line "cgraph unavailable — skipping advisory" to stderr and continues. **cgraph is never a hard dependency of btrain.**

### 3.3 Btrain-facing output contract

Every **new btrain-facing command in §4.2** follows the same I/O discipline:

- **stdout** = one JSON object or JSON-lines stream. Nothing else. There is no non-JSON output mode.
- **stderr** = human-readable progress, warnings, errors.
- **exit code** = 0 on success, 1 on user error, 2 on internal error, 124 on adapter-enforced timeout (see §3.4). Soft-warnings are reported *inside* the JSON, not via exit code.

This is non-negotiable for the commands btrain parses. Agents consuming cgraph must be able to pipe stdout directly into a parser without stripping prose.

**Inherited upstream commands are explicitly out of scope.** `cgraph index` and `cgraph watch` in §4.1 keep their current human-facing Typer/Rich stdout in v1. btrain does not parse them on the handoff path, so we do not retrofit them into JSON-only commands. If we later need machine-readable indexing/watch control, we add explicit wrappers or new commands rather than changing the inherited UX in place.

**Schemas.** Every command ships a matching JSON Schema at `schemas/<command>.json`. CI runs each command against fixture inputs and validates stdout against the schema. Contract drift is a test failure, not a runtime surprise.

### 3.4 Process model, timeouts, and the warm path

**Per-command timeout budgets** (enforced by the btrain adapter; cgraph itself has no timer):

| Command | Budget | On timeout |
| --- | --- | --- |
| `cgraph advise` | 200ms | adapter proceeds without the tip, silently |
| `cgraph blast-radius` | 2s | adapter surfaces `cgraph_timeout` advisory and proceeds |
| `cgraph drift-check` | 2s | same |
| `cgraph review-packet` | 5s | adapter falls back to raw-diff packet + `cgraph_timeout` advisory |
| `cgraph context` | 3s | adapter prints "cgraph slow; falling back to raw code read" |
| `cgraph sync-check` | 5s | adapter silently skips |

No btrain command is ever blocked by cgraph. Every call has a budget and a fallback.

**Python cold-start mitigation.** Upstream CGC is Python. A cold `python -c "import cgraph"` costs ~100-300ms on typical hardware, which would blow the `advise` budget on first invocation.

Two mitigations ship in v1:

1. **Warm-daemon mode (`cgraph serve`).** Long-lived background process speaking a minimal line-protocol over a Unix domain socket at `~/.cache/cgraph/ipc.sock`. CLI commands detect the socket, forward requests, and relay the daemon's stdout. Cold start happens once at daemon startup, not per invocation. Daemon is **optional**; `launchd` / `systemd` templates ship in the repo for users who want always-on.
2. **Subprocess fallback.** When the socket is absent, CLI falls back to direct Python process. Subject to normal budgets. For first-call-heavy paths like `advise`, this means the first tip may time out silently — acceptable in v1, revisited if telemetry shows high cold-miss rate.

btrain's adapter prefers the socket; if not present, it falls through.

**Concurrent cgraph processes.** `cgraph watch` + ad-hoc CLI calls + daemon may share the KùzuDB directory. KùzuDB allows one writer at a time; readers are concurrent. Write operations (`embed`, `index`) acquire an exclusive process-file lock at `$db_path/.cgraph.lock`; contending calls fail fast with `kind: "db_busy"` instead of hanging.

## 4. Command surface (MVP)

### 4.1 Inherited from upstream (documented, unchanged)

- `cgraph index [path]` — full parse, populates KùzuDB.
- `cgraph watch [path]` — daemon, incremental updates on file change.

These inherited commands keep upstream's existing human-readable CLI behavior and are intentionally outside the JSON-only stdout contract in §3.3.

### 4.2 New in cgraph

#### `cgraph context <query> [--lane <id>] [--k <n>] [--depth <n>]`

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
  "token_estimate": 1840,
  "token_estimate_basis": "cl100k_base (approximate; consumers should re-tokenize with their own model)"
}
```

`token_estimate` is an approximation using the `cl100k_base` tokenizer (OpenAI's, close-enough for Claude and GPT for rough planning). `token_estimate_basis` in the output names the tokenizer used. Consumers who need exact counts should re-tokenize with their own model.

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

**`--include-*` flag semantics.** The flags override the §4.3 automatic fallback by *forcing* a specific source:

- With no `--include-*` flag, cgraph walks the auto-fallback chain (§4.3) starting at `diff`.
- `--include-staged` forces source to `staged` (skip diff attempt).
- `--include-workdir` forces source to `workdir`.
- `--include-untracked` forces source to `untracked`.
- Multiple flags: cgraph errors with `kind: "conflicting_include_flags"` on stderr; agent should pick one.

btrain's adapter does not set these by default; they exist for human CLI use and edge-case wiring.

#### `cgraph blast-radius --files <paths> [--lane <id>]`

Pre-lock collision check. Expands the requested lock set through the graph and reports:

- Transitive callers / callees outside the requested paths.
- Overlap with other active btrain lanes (btrain adapter passes the lock table in).
- Advisory level per overlap (soft-warn v1).

This command is also the primary producer of `lock_overlap` advisories consumed by `cgraph advise` (see §5.1 lifecycle). When btrain detects that a claim or status check calls for overlap analysis, it runs `blast-radius` first and then asks `advise` for the pre-formatted tip.

#### `cgraph drift-check --lane <id> [--since <timestamp>]`

Has anything in the lane's locked files' graph neighborhood changed outside the lane since `--since`? Used by btrain to warn owners mid-lane that upstream or sibling code shifted under them.

If `--since` is omitted, cgraph uses the lane's `first_seen` timestamp from the advisory state file (§5.1); if that's also absent, falls back to the lane's claim time from btrain's handoff metadata.

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
  "advisory_id": "adv_2026041619_ab12cd",
  "suggestion": "Run `cgraph blast-radius --files src/auth/ --lane b` — lane A holds locks on 3 transitive callers.",
  "rationale": "git diff won't show you who calls verify_token; the graph does."
}
```

btrain surfaces these as one-line tips in its own output. Agents see them inline and can choose to run the named cgraph command.

**`advisory_id` is a correlation handle.** When an agent subsequently runs the recommended command, cgraph logs the `advisory_id` in the telemetry log (§5.2) so §11 metric 3 (adoption rate) is actually measurable. Without the ID, adoption is unattributable.

**Latency budget: < 200ms per call.** `cgraph advise` is a lookup, not a query — it reads a precomputed advisory table keyed by `situation`. The table is populated by the heavier commands (`blast-radius`, `drift-check`, `review-packet`) as they run; advise itself never triggers a full graph walk.

**Cold-table behavior.** If btrain calls `advise` before any heavier command has populated the table for that situation, cgraph returns `{"situation": "...", "advisory_id": null, "suggestion": null, "rationale": "no cached analysis; run <the relevant command> first"}`. The adapter suppresses the tip. Adapter is expected to run the heavier command in the background on detection of the triggering event, so by the next `btrain status` the table is warm.

If cgraph cannot respond inside 200ms, the btrain adapter times out silently (§3.4).

### 4.3 Diff-absent modes (fallback hierarchy)

`cgraph review-packet` walks a fallback chain until it produces non-empty content. Each step is labeled in the output `source` field so the reviewer always knows what they're looking at:

1. **`diff`** — `base..head` both resolve and produce a non-empty diff. Default path.
2. **`staged`** — no usable diff refs, but `git diff --cached` is non-empty. Uses index.
3. **`workdir`** — nothing staged, but `git diff` (working tree vs HEAD) is non-empty.
4. **`untracked`** — still nothing, but `git ls-files --others --exclude-standard` (filtered to `--files` if provided) finds untracked files. For each path, cgraph tries graph lookup first; if the file is brand-new and absent from KùzuDB, it falls back to direct parse-from-worktree (without mutating the graph) to synthesize nodes; if neither path works, the file is omitted with an explicit advisory naming the skipped paths.
5. **`locked_files`** — last resort. btrain adapter passed `--files` from the lane's lock list. Return graph neighborhood of those files as-is. Advisory: `kind: "no_diff_available"`, level `warn`.

At every step, cgraph emits advisories when the data is suspicious:

- **`missing_base_ref`** — `--base` unresolvable (deleted branch, prose-wrapped value, force-pushed). cgraph still falls through and reports what it can.
- **`empty_diff`** — refs resolved but `base..head` is empty. Triggers `staged` → `workdir` → `untracked` → `locked_files` fallback.
- **`untracked_only`** — all changed paths are untracked. Tells reviewer "this lane's work is not committed yet."
- **`untracked_unindexed_omitted`** — one or more brand-new untracked files could not be synthesized from the worktree (unsupported language, parse failure, or `.cgcignore` exclusion). Packet lists the omitted paths explicitly so they are never silently dropped.
- **`stale_index`** — for a file in the packet, its **content hash** (via `git hash-object <file>`) differs from the hash stored alongside the KùzuDB node's last index record. Using content hash avoids false positives on fresh clones where every file's mtime is the checkout time. Suggests `cgraph index <path>` or restart `cgraph watch`.
- **`excluded_by_cgcignore`** — ≥ 1 file in the diff matches `.cgcignore`. Prevents the silently-empty-packet failure mode.
- **`refs_diverged_from_main`** — `base` is not an ancestor of `head`; diff spans a merge. Packet is still correct but flagged.
- **`unsupported_repo_shape`** — emitted when the repo is in a shape cgraph can't fully handle. Details below.

**Key invariant:** `cgraph review-packet` never exits non-zero because of an "unusual" diff state. It always emits JSON, always sets `source`, always lists any advisories. Brand-new untracked files are either synthesized from the worktree or called out as omitted. The btrain adapter decides whether to surface advisories; cgraph's job is to report honestly.

**Repo-shape edge cases.** cgraph probes `git rev-parse --is-inside-work-tree`, `--is-bare-repository`, and `--show-superproject-working-tree` on every invocation and sets the `unsupported_repo_shape` advisory when any of the following holds, while still producing best-effort output:

- **Worktrees** (`git worktree`) — supported. KùzuDB path resolves via `$db_path` (explicit config) or `$repo_root/.cgraph/db`; worktrees share one index by default. Advisory fires if the worktree points at a different main repo than the configured `db_path`.
- **Bare repos** — unsupported in v1. cgraph exits with `kind: "unsupported_repo_shape", detail: "bare_repo"` and source `locked_files` (no working tree to read).
- **Submodules** — cgraph indexes the superproject only; submodule content is treated as excluded. Diff paths inside submodules are surfaced in the packet but not graph-analyzed. Advisory notes the boundary.

### 4.4 Packet size cap

Review packets are capped to prevent unbounded output when a lane locks a broad scope with no diff. Cap applies to the `touched_nodes` + `callers_not_in_diff` + `callees_not_in_diff` sets combined.

- **Default cap:** 50 nodes, applied to each of the three buckets (`touched_nodes`, `callers_not_in_diff`, `callees_not_in_diff`) independently. Configurable via `--max-nodes <n>` or `[cgraph.review_packet].max_nodes` in `.btrain/project.toml`.
- **When hit:** output includes `"truncated": true`, `"total_nodes": {"touched": 247, "callers": 12, "callees": 40}`, `"returned_nodes": {"touched": 50, "callers": 12, "callees": 40}`. The truncation is not silent.
- **Selection when truncated:** prefer nodes with highest in-degree (most callers) on the assumption that high-fan-in code is higher-stakes to review.
- **Estimated hit rate (speculative, not yet measured):** ~3-5% of all review-packets on typical btrain usage, concentrated on broad-lock lanes. Telemetry in §11 will confirm or correct this.

Every truncated packet carries a **context-aware workaround advisory** with `kind: "packet_truncated"` and a suggestion chosen by the fallback source that fired:

| `source` at truncation | `suggestion` field |
| --- | --- |
| `locked_files` + `untracked_only` advisory | `"Commit or stage your changes and re-run — the packet will narrow to just what you touched."` |
| `locked_files` + no diff-related advisory (lane just claimed, no edits) | `"Once you start editing, re-run with --include-workdir to scope to changed files only."` |
| `locked_files`, broad scope, commits exist | `"Narrow with --files <subpath>, or query by intent: cgraph context '<topic>'."` |
| `workdir` / `staged` / `untracked` (rare — means uncommitted change set itself is huge) | `"Commit in smaller logical chunks, or narrow with --files <subpath>."` |

The suggestion is embedded as plain text in the JSON so the consuming agent can surface it directly without translation.

## 5. btrain integration points

These are the triggers inside **btrain** (not cgraph) that shell out to cgraph. Each is a one-liner the adapter wraps.

| btrain event | cgraph call | What the agent sees |
| --- | --- | --- |
| `btrain handoff claim --files X` | `cgraph blast-radius --files X --lane <id>` | Advisory if any listed file has high-fan-in callers outside X. Never blocks the claim. |
| `bth` output when `status: needs-review` | `cgraph review-packet` (refs passed if resolvable; omitted otherwise — cgraph falls through §4.3) | Reviewer sees "cgraph review packet available — run: cgraph review-packet ..." as a tip. |
| `pre-handoff` skill, after diff gate | `cgraph review-packet ...` | Prints advisory if `untested_caller` fires. **Never blocks the handoff** in v1 (soft-warn per decision 5). |
| `btrain doctor` | `cgraph sync-check` | Reports if cgraph fork is behind upstream. |
| `btrain handoff claim` / `btrain status` | adapter runs `cgraph blast-radius` to detect overlap, then `cgraph advise --situation lock_overlap` for the pre-formatted tip | Detection and advisory production are two steps: the heavier command computes the condition and populates the advise cache; `advise` is the formatting lookup. One-line tip in btrain stdout. |
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

**State model.** Each advisory is keyed by `(lane_id, kind, context_hash)`, where `context_hash` uniquely identifies the situation. Two entries with the same `(lane, kind)` but different `context_hash` are distinct — e.g. lane `b` overlapping lane `a` on `verify_token` is a different advisory from lane `b` overlapping lane `c` on `pay_out`.

**`context_hash` per advisory kind** — BLAKE3 over a canonical UTF-8 string (first 12 hex chars retained):

| `kind` | Hash input |
| --- | --- |
| `lock_overlap` | `sorted(other_lane_ids) + "\n" + sorted(overlapping_node_ids)` |
| `drift` | `sorted(changed_node_ids_outside_lane)` |
| `untested_caller` | `sorted(touched_function_ids_without_test_coverage)` |
| `cross_module_impact` | `sorted(module_boundaries_crossed)` |
| `stale_index` | `sorted(stale_file_paths)` |
| `packet_truncated` | `source + ":" + str(total_touched_node_count)` |
| `missing_base_ref` / `empty_diff` / `untracked_only` / `untracked_unindexed_omitted` / `excluded_by_cgcignore` / `refs_diverged_from_main` / `unsupported_repo_shape` / `no_diff_available` | `lane_id + ":" + handoff_id` (one per handoff attempt) |

Any new advisory kind MUST declare its hash rule before shipping.

**State file:** `.btrain/cgraph-advisory-state.jsonl` (btrain-owned). Despite the `.jsonl` extension this is **not** append-only. It is a small JSONL-formatted snapshot of **active advisories only**, read in full and rewritten in full on each update. Resolved advisories are removed from this file immediately and preserved only in telemetry (§5.2). Typical size: < 2KB per repo. One line per active advisory:

```json
{"lane": "b", "kind": "lock_overlap", "context_hash": "ab12…", "first_seen": "2026-04-16T12:00Z", "last_surfaced": "2026-04-16T12:00Z", "resolved_at": null, "detail": "…"}
```

**Lifecycle rules per btrain advisory-surfacing event** (table in §5):

1. Adapter asks cgraph for current conditions for the lane (e.g. `cgraph blast-radius --lane b`).
2. For each condition returned, compute its `context_hash`.
3. Match against state file:
   - **No matching active entry** → write new entry, surface the advisory (set `last_surfaced` = now).
   - **Matching active entry exists** → suppress (do not surface). Do not update `last_surfaced`.
4. For every active entry whose condition is **no longer** present → remove it from the active snapshot and append a `resolved` event to telemetry with `resolved_at = now`. If `advise_on_resolution = true` (opt-in), surface a one-line resolution tip: `"cgraph: lock_overlap on verify_token with lane a is resolved — you're clear."`. Default is `false` to minimize noise.
5. If the lane is resolved (`btrain handoff resolve`), remove all still-active entries for that lane from the active snapshot and append the same `resolved` telemetry events.

**Why this shape:**
- Matches the "show while condition exists, stop when it's over" requirement literally.
- Deterministic — same (lane, kind, context_hash) always dedupes.
- Safe on crash — state can be reconstructed from the telemetry log (§5.2) by replaying surface+resolve events, so partial writes are recoverable.
- Cheap — reading the state file is O(active advisories), typically < 10 lines per lane.

**Concurrency and write ownership.** Two btrain invocations may race on the state file (e.g., lane-A's `btrain status` running while lane-B's `handoff claim` finishes), so the **btrain adapter is the sole writer** for both `.btrain/cgraph-advisory-state.jsonl` and `.btrain/logs/cgraph-advisories.jsonl`; cgraph never writes either file. The adapter acquires an OS advisory file lock (`flock` on Unix, `LockFileEx` on Windows) on `.btrain/cgraph-advisory-state.jsonl.lock` before any read-modify-write cycle, rewrites the active snapshot atomically, then appends any surface/resolve telemetry rows. Holds are < 50ms typical; contending callers wait up to 500ms then fail open (proceed without state update, surface the advisory anyway to avoid losing signal). Lost-update is preferable to lost-advisory in v1.

### 5.2 Telemetry log

Separate from state, append-only: `.btrain/logs/cgraph-advisories.jsonl`. One line per surface event AND one per resolve event. This is the historical record; resolved advisories live here, not in `.btrain/cgraph-advisory-state.jsonl`. Used for the success metrics in §11 (false-positive rate, agent adoption rate). Never read by the lifecycle logic.

### 5.3 Cross-lane surfacing (pull model)

Problem: lane-A holds locks. Lane-B claims with overlap → lane-B is warned by the claim-time check. Lane-A has no idea B is encroaching. Without a fix, half of every cross-lane situation goes unseen by the lane that was "there first."

**Decision: pull model.** `btrain status` aggregates advisories across every active lane, not just the caller's.

- When any agent runs `btrain status`, the adapter iterates active lanes and calls cgraph per-lane.
- Output groups advisories by lane: `lane a: lock_overlap with lane b on verify_token`, `lane b: drift on src/auth/`, etc.
- The §5.1 lifecycle applies unchanged — each `(lane, kind, context_hash)` surfaces once when it first appears and then stays quiet. Pull model just widens *which* lanes get evaluated at each `btrain status` call.
- A single status call may surface new advisories across multiple lanes; it still commits through the single shared advisory-state lock and rewrites one shared active-state snapshot atomically.

Why pull over push: zero cross-lane coupling, no mid-lane interrupts, agents see the information when they naturally check status, and `btrain status` becomes the canonical "situational awareness" surface.

Caveat: an agent who never runs `btrain status` on their own lane won't see cross-lane advisories. Acceptable in v1 since `btrain status` is already part of the recommended `bth` flow; if telemetry shows agents skip it, we revisit.

**Performance.** Fanning out cgraph calls across N active lanes serially would make `btrain status` stall proportionally. Mitigations:

1. **Parallel fanout.** The adapter fires all per-lane `cgraph` calls concurrently (Node `Promise.all`), bounded by the max of the per-command budgets (§3.4) rather than their sum.
2. **Status-level cache.** For `btrain status`, the adapter reuses any advisory whose `last_surfaced` is within the last 2s for the same `(lane, kind, context_hash)` rather than re-querying cgraph. This makes rapid successive `btrain status` calls cheap.
3. **Lane-count budget.** If `active_lanes > 8`, the adapter samples (oldest-first) the first 8 and flags the rest as `cgraph: N additional lanes not analyzed — run \`btrain status --lane <id>\` for a specific one`. Prevents runaway cost on pathological project layouts.

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
- **Distribution.** Model is **not** bundled with the cgraph package (too large for PyPI norms). First `cgraph embed` run downloads from HuggingFace to `$SENTENCE_TRANSFORMERS_HOME` (or `$HF_HOME`, in that precedence order), with an explicit progress bar on stderr. Subsequent runs load from cache. `cgraph embed --check-model` verifies the weights exist without triggering a download — useful for air-gapped environments that pre-stage the cache.
- **Storage impact:** 10K functions × 768 dims × 4 bytes ≈ 30MB. Negligible.
- **Rationale documented:** embedding-choice rationale plus a `cgraph eval-embeddings` command (out-of-MVP, §12 Q4) so teams can A/B on their own corpus.

### 6.2 Storage and privacy

- Model cache and DB dir both take explicit paths. External SSD supported (see §5 `db_path` / `model_cache`). Caveat documented in README: USB-3 degrades ANN latency; Thunderbolt/USB-4 is fine.
- **API providers opt-in only.** Local provider stays the shipping default so code never leaves the machine unless the user flips the switch.

### 6.3 Ingestion

- **Backend requirement:** KùzuDB only in v1. Upstream CGC supports FalkorDB Lite and Neo4j too, but cgraph's ALTER / HNSW / vector columns rely on KùzuDB-specific syntax. On startup, cgraph probes the upstream-configured backend and, if non-KùzuDB, exits with `kind: "unsupported_backend", detail: "cgraph v1 requires kuzu; found <backend>. Set CGC backend to kuzu and re-index."`. FalkorDB/Neo4j support is a post-v1 question (§12 new entry).
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
- `untracked_unindexed_omitted` — brand-new untracked files could not be synthesized from the worktree, so the packet names and omits them explicitly.
- `stale_index` — locked files' current content hash differs from the hash recorded at last graph index.
- `excluded_by_cgcignore` — ≥ 1 diff file excluded from the graph; packet may mislead.
- `refs_diverged_from_main` — `base..head` spans a merge; reviewer should check merge commits.
- `packet_truncated` — review-packet exceeded `max_nodes`; carries a context-aware `suggestion` field (see §4.4).

Before promoting any advisory to hard-fail we want ≥ 30 days of telemetry (JSONL log of `warn` events + agent outcome) showing < 5% false-positive rate.

## 8. Upstream-sync policy (decision 4)

- Fork relationship is the sync primitive. `origin = codeslp/cgraph`, `upstream = CodeGraphContext/CodeGraphContext`.
- Most implementation lives in a **top-level `cgraph_ext/`** directory so the bulk of our code stays outside upstream's source tree.
- One deliberate upstream seam remains: `src/codegraphcontext/cli/main.py` must register/import the new Typer commands, and `cgc_entry.py` may need a packaging-level shim. That seam is intentionally small, but it is still an upstream-owned merge surface.
- When upstream files must be modified, prefer subclassing/wrapping over editing in place.
- `cgraph sync-check` surfaces new upstream commits; user decides when to `git merge upstream/main`.
- Monthly minimum cadence. Major upstream version jumps (0.x → 0.y) get a checklist.

### 8.1 Versioning

- cgraph has its own semver independent of upstream CGC. Format: `cgraph-vX.Y.Z` tags on `origin/main`.
- **Output JSON is a stability surface.** Breaking changes to any documented JSON output shape require a major bump. Additive fields are minor. Advisory `kind` additions are minor; renaming/removing a kind is major.
- Each JSON Schema under `schemas/` is versioned in its `$id`. Agents can pin to a schema version.
- CHANGELOG entries explicitly call out "Output-compat" vs "Internal" changes.

### 8.2 CI strategy

- cgraph ships its own CI (`.github/workflows/cgraph.yml`) that runs **only** under `cgraph_ext/` and `schemas/` — upstream's CI files in `.github/workflows/` are untouched and keep running independently. On upstream sync, both CIs run; cgraph's CI guards our additions, upstream's guards the inherited surface.
- If an upstream sync breaks upstream's own CI, that's a signal to hold the merge and investigate — we don't suppress upstream CI even though we don't depend on it for cgraph-specific code.

## 9. Repo layout

```
cgraph/
├── src/                        # upstream, mostly unmodified
│   └── codegraphcontext/cli/main.py
│                              # upstream-owned Typer entrypoint; registers cgraph commands
├── cgraph_ext/                 # our additions — top-level, no collision with src/
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
│   ├── daemon/
│   │   └── serve.py            # `cgraph serve` warm-daemon (§3.4)
│   └── io/
│       ├── json_stdout.py      # enforce output contract
│       └── schema_check.py     # validate every command's output against schemas/
├── schemas/                    # JSON Schema for every cgraph command output
│   ├── context.json
│   ├── review-packet.json
│   ├── blast-radius.json
│   ├── drift-check.json
│   ├── advise.json
│   └── sync-check.json
├── specs/                      # this file lives here
├── tests/cgraph_ext/           # our tests
├── .github/workflows/
│   └── cgraph.yml              # our CI, scoped to cgraph_ext/ + schemas/
├── cgc_entry.py                # existing entrypoint; may need a small import shim
└── README.md                   # upstream; we add a "cgraph additions" section
```

Most new code lives under the top-level `cgraph_ext/` namespace. The CLI registration seam in upstream-owned entrypoints is small but real, so upstream merges can still touch that boundary.

## 10. Phases

**Phase 0 — Scaffolding (week 1)**
- Confirm upstream builds and indexes this repo locally.
- Add `cgraph_ext/` skeleton and `schemas/` directory; CI passes under `.github/workflows/cgraph.yml`.
- Ship `cgraph sync-check` (simplest; validates output contract and schema-validation harness).
- Publish JSON Schemas for all six new commands as stubs — populated by later phases.

**Phase 1 — Hybrid retrieval (week 2-3)**
- `cgraph embed` + schema ALTER; KùzuDB-only backend probe.
- `cgraph context <query>` end-to-end with local embeddings (Jina v2 code).
- Tests: retrieval recall on a small fixture repo, JSON shape validation against `schemas/context.json`.

**Phase 2 — Review packet + blast radius + replay harness (week 3-4)**
- `cgraph review-packet` against a real btrain lane's diff. All §4.3 fallback sources implemented (diff/staged/workdir/untracked/locked_files).
- `cgraph blast-radius` with lane-lock awareness via adapter.
- **Replay harness:** small Node script that reads btrain's recent handoff history and re-runs review-packet + blast-radius against each, logging output sizes. Needed for §11 metrics; without it, success is immeasurable.
- Measure: tokens-per-review vs. raw-diff baseline on 10 recent btrain handoffs.

**Phase 3 — btrain adapter + advisories + warm daemon (week 4-5)**
- `src/brain_train/cgraph_adapter.mjs` lands in btrain (separate PR on btrain repo). Includes: per-command timeout budgets (§3.4), parallel fanout + status-level cache (§5.3), OS advisory-file-lock on advisory state (§5.1), `advisory_id` correlation capture.
- `[cgraph]` config section supported in `.btrain/project.toml`.
- `cgraph advise` wired into `btrain handoff`, `bth`, `pre-handoff`.
- **`cgraph serve` warm daemon** with `launchd`/`systemd` templates. Optional but strongly recommended.
- Advisory state file (`.btrain/cgraph-advisory-state.jsonl`) and telemetry log (`.btrain/logs/cgraph-advisories.jsonl`) wired end-to-end with correlation IDs flowing through.

**Phase 4 — Drift and polish (week 5-6)**
- `cgraph drift-check` plus `btrain status` pull-model integration.
- README overhaul: distinguish upstream commands from cgraph additions.
- First `cgraph sync-check` cadence documented.

**Out of scope for MVP, tracked separately:**
- Jira / deployment / org-chart nodes in the graph (proposal §1). Code-only for now.
- Hard-fail guardrails.
- MCP server modifications.

## 11. Success metrics

All measured against btrain's last 30 handoffs, replayed with cgraph on vs. off via the Phase 2 replay harness.

1. **Review tokens**: ≥ 40% reduction in tokens passed to reviewer model per handoff (measured by `token_estimate` in review-packet vs. raw-diff baseline using the same tokenizer).
2. **Lock collisions caught pre-claim**: ≥ 1 real collision caught per week across active lanes.
3. **Agent adoption of advisories**: ≥ 50% of advise-tips acted on, measured via `advisory_id` correlation — a tip is "acted on" if the agent invokes any cgraph command within 30 min of the tip's surface event that logs the matching `advisory_id` in the telemetry log (§5.2). Without correlation IDs this metric is unattributable.
4. **False-positive rate on soft-warns**: < 10%, measured as: agent runs suggested command, resulting output reports no real finding (e.g., `blast-radius` called in response to `lock_overlap` tip returns zero overlapping callers).
5. **Upstream sync lag**: never more than 30 days behind `upstream/main`.

## 12. Open questions

- **Q1:** Does btrain's `handoff_history` format have enough structure for `cgraph review-packet` to derive base/head refs without re-parsing the handoff file? The §4.3 fallback chain tolerates missing refs, so this is a polish question — worth a small `btrain handoff refs --lane <id>` helper in a later btrain PR. **Open, low priority.**
- **Q2:** ~~Cross-lane advisory surfacing~~ → **Closed.** Pull model via `btrain status` per §5.3.
- **Q3:** ~~Advisory telemetry storage~~ → **Closed.** btrain-owned at `.btrain/logs/cgraph-advisories.jsonl`.
- **Q4:** Embedding provider for repos with highly domain-specific jargon — Jina v2 code is strong on general code but may underperform on e.g. legal or medical domain-specific jargon. Ship a small eval harness (`cgraph eval-embeddings`) so teams can A/B providers on their own corpus. **Out of MVP scope.**
- **Q5:** Graph-backed `MEMORY.md`? Today MEMORY.md accumulates narrative. A `cgraph memory-refresh` command could regenerate the "current state" section from the graph. Exciting but big — **phase 5+.**
- **Q6:** FalkorDB Lite and Neo4j backend support. v1 is KùzuDB-only (§6.3) because vector-column ALTER / HNSW syntax differs by backend. Post-v1 work: abstract the embedding-store layer behind a provider interface, or pick the right backend-specific idiom per target. **Deferred; revisit when a cgraph user requests non-KùzuDB.**

## 13. Decisions recorded

| # | Decision | Chosen |
| --- | --- | --- |
| 1 | Integration direction | (a) btrain-knows-cgraph, with btrain-side adapter |
| 2 | MVP priorities | Review packets, blast-radius, pre-handoff gate, context fetch — plus agent-advisory tips for dysfunction situations |
| 3 | Embeddings | Local default `jina-embeddings-v2-base-code` (code-specific, 8K ctx, 768-dim); swappable provider; paths configurable (external SSD supported); `voyage-code-3` as API upgrade path |
| 4 | Upstream sync | User-gated via `cgraph sync-check`; never auto-merge |
| 5 | Guardrail tone | Soft-warn only in v1; hard-fail requires telemetry |
