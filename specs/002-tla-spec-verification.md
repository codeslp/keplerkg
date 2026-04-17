# Spec 002 — TLA+ integration for cgraph's spec process

**Status:** draft v0.1
**Owner:** codeslp
**Last updated:** 2026-04-17
**Depends on:** [spec 001](001-btrain-integration.md) (state-machine sections are the pilot subjects)

---

## 1. Why

Spec 001 is ~800 lines of prose. Two of its bug classes survived multiple review passes and only surfaced because codex triangulated across disjoint sections:

- **Cross-section semantic drift.** §2 declared "soft-warn only in v1" while §6.6 shipped `exit-2` hooks and `--require-hard-zero` gates. Seven sections had to be rereconciled in lane a.
- **Concurrent-path handwaving.** §5.1 describes the advisory-state lifecycle under a file lock that five different call paths contend on (`btrain status`, `handoff claim`, `handoff resolve`, `cgc watch`, daemon). The "fail-open after 500ms" branch is one paragraph of prose with no proof that advisories aren't lost under the interleaving.

Both classes are exactly what TLA+ catches cheaply when the relevant section is already a state machine. Prose reviewers miss them; TLC finds them in seconds on a 150-line spec. This meta-spec defines how we layer TLA+ into the existing prose workflow **without turning every section into formal notation**.

We name the pattern "**specs-as-state-machines**" — a complement to §6.5's specs-as-graph-queries. Prose stays authoritative; TLA+ adds a checkable skeleton on top of the sections that are actually state machines.

## 2. Non-goals

- Not formalizing every spec section. Most aren't state machines and adding notation for its own sake creates rot without signal.
- Not LLM-generated TLA+. Current correctness floor is ~8.6% semantic accuracy ([AI4FM 2025 study](https://arxiv.org/pdf/2512.09758)). TLA+ files in this repo are hand-written by a human or by an agent operating under TLC's counter-example feedback loop, never blind.
- Not replacing prose. The Markdown spec remains the primary artifact. `.tla` is a secondary artifact that must stay consistent with the prose or fail CI loudly.
- Not a new spec language. No DSL, no meta-meta-spec. TLA+ + TLC + a small sync script.
- Not blocking handoffs on state-space explosion. If TLC exhausts its budget, the handoff warns but does not block (see §5).

## 3. What qualifies for TLA+

A section is **eligible** (not required) when it contains at least one of:

1. **Named state transitions.** "X moves from `in-progress` to `needs-review`…" — any lifecycle with more than two states and > 1 transition rule.
2. **Temporal/quantification claims.** "never", "always", "at most once", "eventually", "at least one of" — any claim that implies a temporal operator.
3. **Multi-actor concurrency.** Two or more processes, lanes, hooks, or callers can touch the same state.
4. **Chain-of-guarantee reasoning.** "If any of these N gates holds, the invariant is preserved" (e.g. the §6.6 enforcement chain).

**From spec 001, eligible sections:**

| Section | Why |
| --- | --- |
| §5.1 Advisory lifecycle | State machine with concurrent writers under a file lock |
| §5.3 Cross-lane pull model | Fanout + dedup across N active lanes |
| §6.4 Graph roles and lifecycle | `working/<lane>` → `review/<lane>` → delete / rebuild transitions |
| §6.6 Enforcement chain | "At least one of four gates catches every hard violation" |
| btrain handoff state machine (across §§4, 10) | `in-progress` → `needs-review` → `resolved` / `changes-requested` with lock ownership |

**Ineligible (intentionally):**

| Section | Why |
| --- | --- |
| §3.3 JSON output contract | JSON Schema already formalises it |
| §6.5 Code-quality standards | Cypher queries; graph-shape, not state-machine |
| §8 Upstream-sync policy | Policy, not protocol |
| §9 Repo layout | Declarative file tree |
| §11 Success metrics | Statistical, not logical |

## 4. File layout

```
specs/
├── 001-btrain-integration.md
├── 002-tla-spec-verification.md    # this file
└── tla/
    ├── README.md                   # how to run TLC locally + in CI
    ├── advisory_lifecycle.tla      # pilot — spec 001 §5.1
    ├── advisory_lifecycle.cfg      # TLC config (INIT, NEXT, INVARIANT, CONSTANTS)
    ├── enforcement_chain.tla       # follow-up — spec 001 §6.6
    ├── enforcement_chain.cfg
    └── handoff_state.tla           # follow-up — btrain lifecycle
```

One `.tla` per state machine. No mega-spec. Each `.tla` is < 300 lines of TLA+ plus one `.cfg` of < 30 lines. If a spec needs more, it should be split into two state machines.

## 5. Rot prevention (the critical bit)

Specs change often. Without a mechanical sync check, the `.tla` and the `.md` drift and the formal spec becomes worse than no spec — a stale artifact that review trusts and shouldn't.

### 5.1 Pinned-hash header

Every `.tla` file starts with a header block. Q1 in §10 is resolved to **line-range pinning** for v1; the forward-compat migration path to section-heading pinning is documented under Q1 itself.

```
---- MODULE AdvisoryLifecycle ----
(* Pinned to: specs/001-btrain-integration.md §5.1 (Advisory lifecycle) *)
(* Pinned-hash: sha256:abc123…  range: lines 329-374  version: 0.5 *)
(* Regenerate hash with: scripts/tla_pin.py specs/001-btrain-integration.md 329 374 *)
```

The hash is a SHA-256 of the normalised prose block referenced by the line range (trailing whitespace stripped, line endings normalised, no other transforms). One `.tla` may pin to multiple prose ranges; each pin carries its own hash line.

The `range:` token is the only scope encoding `tla_pin.py` will accept in v1. If Phase 1's pilot surfaces excessive false-stale rewrites triggered by unrelated edits above the pinned range, the Q1 migration plan escalates to section-heading pinning — a strictly additive `--scope heading:§5.1` parser alongside the existing `--scope lines:...` path, with no breaking change to already-landed `.tla` headers.

### 5.2 Sync script (`scripts/tla_pin.py`)

One script, two modes:

- `tla_pin.py <spec.md> <start> <end>` — prints the current hash for the given line range. Used by authors when re-pinning after a semantic-neutral edit or after a scope shift.
- `tla_pin.py --check` — exits non-zero if any `.tla` in `specs/tla/` has a stale pin. Scans every pin header, re-hashes the referenced range in its source spec, compares. Used by pre-handoff and CI.

Phase 1 ships the line-range path only; the interface above stays stable across any future scope encoding (Q1 migration) because only the positional args change, and `--check` remains encoding-agnostic.

### 5.3 pre-handoff integration

The `pre-handoff` skill gains a new check: run `scripts/tla_pin.py --check`. On mismatch, the handoff is blocked with a reviewer-facing message:

```
Stale TLA+ pin detected:
  specs/tla/advisory_lifecycle.tla pins specs/001-btrain-integration.md lines 329-374 @ sha:abc123
  current hash of that range is sha:def456
Either:
  a) update the .tla to reflect the prose change and re-pin, or
  b) re-pin without changing the .tla (acknowledge the prose edit was semantically neutral).
```

Re-pinning without updating is explicitly allowed — many prose edits are reformatting, typo fixes, or clarifications that do not change the state machine. The author's acknowledgement is the signal; the block exists to force the choice to be conscious, not to force a `.tla` rewrite on every whitespace change.

### 5.4 CI integration

`.github/workflows/cgraph.yml` gains one job **and** widens its path triggers. The current workflow only fires on `src/codegraphcontext_ext/**`, `schemas/**`, `tests/cgraph_ext/**`, `pyproject.toml`, and the workflow file itself. Spec-only or script-only changes bypass CI entirely. Phase 1 adds the following paths to both `push` and `pull_request` triggers so the TLA gate cannot be silently skipped:

- `specs/**`
- `specs/tla/**`
- `scripts/tla_pin.py`

The new job:

1. `tla_pin.py --check` — fails if any pin is stale.
2. For each `.tla`, run TLC with its `.cfg`. Timeout: 5 min per spec (configurable).
3. TLC counterexample → job fails, trace posted as a PR comment.
4. TLC state-space-exhausted before counterexample → job **warns** (comment on PR, does not block). State-space tuning is an ongoing discipline, not a release blocker.

### 5.5 What rot-prevention does *not* do

- **Does not ensure the `.tla` is semantically correct.** Sync-check only ensures author acknowledged the prose change. Correctness is verified by TLC.
- **Does not force every spec edit to touch a `.tla`.** If the edited section is ineligible (see §3), no pin is required.
- **Does not version-pin across spec files.** Each pin is within one spec; cross-spec invariants would need a second design.

## 6. Spec-change workflow

```
author edits prose in specs/001-btrain-integration.md §5.1
        │
        ▼
pre-handoff (skill) runs tla_pin.py --check
        │
        ├── pin matches → continue handoff
        │
        └── pin stale → author chooses:
                ├── edit .tla + re-pin (semantic change)
                └── re-pin only (semantically-neutral edit)
        │
        ▼
handoff → reviewer sees prose diff + .tla diff + TLC result side by side
        │
        ▼
CI runs TLC
        │
        ├── passes → review proceeds normally
        ├── counterexample → PR blocked, trace in comment
        └── state-space exhausted → PR warning, review proceeds
```

Under this workflow a reviewer reading a state-machine change reads the `.tla` diff before the prose diff, because `.tla` is the authoritative behavioural statement of the section.

## 7. Pilot: §5.1 Advisory lifecycle

First `.tla` to land. Pilot-grade, not exhaustive.

**Scope.**

- 2 lanes (`a`, `b`)
- 3 advisory kinds (`lock_overlap`, `drift`, `stale_index`)
- 1 file lock on `.btrain/cgraph-advisory-state.jsonl`
- 4 actions: `surface`, `suppress_dup`, `resolve_by_event`, `resolve_by_lane_close`
- 1 concurrent-interleaving action: `status_call_races_with_claim`

**Invariants to prove.**

1. **No-spam.** A given `(lane, kind, context_hash)` surfaces at most once while the condition is continuously present.
2. **No-lost-signal.** After a `resolve` event, the next `surface` of the same key is not suppressed.
3. **At-most-one-writer.** No two actions hold the state-file lock concurrently.
4. **Fail-open-safety.** When a caller times out on lock acquisition (the `500ms fail-open` branch), the advisory it intended to write is *surfaced* (human-visible) even though the state write was skipped — the `≥ 1 surface` property holds.
5. **Lane-close cleanup.** After `handoff resolve` on lane L, no active entries for L remain in the state file.

**Success criterion.** TLC runs to completion (or depth ≥ 10 hops) on a Mac M-series in < 60s. Either TLC finds a counterexample against one of the five invariants (win — spec prose gets updated), or it exhaustively proves them at the chosen bound (also a win — prose now has a machine-checked skeleton).

**Budget.** 1–2 days. If the pilot lane overruns, we hand off to a new lane with the .tla incomplete + a `NON_BLOCKING` note on the invariant list so review can still happen.

## 8. Phases

| Phase | Work | Gate |
| --- | --- | --- |
| 0 | This meta-spec lands | lane b (this lane) review-approved |
| 1 | Pilot §5.1 .tla + .cfg + `scripts/tla_pin.py` (line-range scope per Q1) + CI job + **widen `.github/workflows/cgraph.yml` triggers to `specs/**`, `specs/tla/**`, `scripts/tla_pin.py` per §5.4** + `specs/tla/README.md` | TLC pass or documented counterexample; pre-handoff + CI gates live; spec-only PR correctly triggers the TLA job |
| 2 | §6.6 enforcement chain .tla | Invariant: ∀ hard_violation → ∃ gate ∈ {PostToolUse, Stop, pre-handoff, CI} that rejects. Hard to prove — expected to surface real edge cases |
| 3 | Handoff state machine .tla | Completes the btrain-integration formal surface |
| 4 | `speckit-formal` skill | Wraps pin-check, TLC invocation, and trace-to-prose explanation |
| 5 | Retrofit §5.3 pull-model and §6.4 graph-role lifecycle if earlier phases show value | Data-driven decision, not committed here |

Phase 4 is explicitly after phase 3 because the skill should be shaped by three real `.tla` files, not designed in advance.

## 9. Decisions recorded

| # | Decision | Chosen |
| --- | --- | --- |
| 1 | Prose-vs-TLA authority | Prose is authoritative for *intent*; `.tla` is authoritative for *state-machine behaviour* of eligible sections. Neither is subordinate; they are co-checked. |
| 2 | TLA+ authorship | Hand-written by humans or by agents operating under TLC feedback. No blind LLM generation. |
| 3 | Rot prevention | Pinned-hash headers + `scripts/tla_pin.py --check` in pre-handoff and CI. Re-pin without edit allowed when prose change is semantically neutral. |
| 4 | Failure modes | TLC counterexample → PR blocked. Stale pin → handoff blocked. State-space exhaustion → warn only. |
| 5 | Pilot target | §5.1 advisory lifecycle. Smallest state machine with real concurrency in spec 001. |
| 6 | No auto-generation | Neither direction (prose → .tla nor .tla → prose) is auto-generated. Both are human-edited artifacts that must stay consistent. |

## 10. Open questions

- **Q1:** Pin granularity. ~~Open.~~ **Resolved: line-range pinning.** §5.1–5.2 already standardize a `range: lines <start>-<end>` header and `tla_pin.py <spec.md> <start> <end>` CLI. Line range is the simplest mechanism that gives a deterministic hash. If the pilot reveals that unrelated edits above the range cause excessive false-stale pins, we can migrate to section-heading pinning (pin against all content under a heading until the next same-level heading) as a Phase 4+ refinement — that requires a small Markdown parser but is backward-compatible with the same `--check` interface.
- **Q2:** TLC bounds for the pilot. 2 lanes × 3 kinds is the starting guess. If state space is < 100k states we can go wider; if it's already millions we need to abstract harder. **Open; empirical during Phase 1.**
- **Q3:** Who owns the `.tla` when the author doesn't know TLA+? Options: (a) claude pairs with the author on every `.tla` edit under TLC's feedback loop, (b) a dedicated TLA+-fluent reviewer gates all `.tla` changes, (c) we decline to formalise sections whose authors don't want to learn TLA+. **Open; defer to Phase 4 skill design.**
- **Q4:** Interaction with `speckit-analyze`. That skill already looks for cross-section inconsistencies. Does it learn about `.tla` files, or do we keep them independent? **Open; prefer independence until Phase 4.**
- **Q5:** Publishing TLA+ artifacts. Do `.tla` files ship inside the `cgraph` PyPI distribution, or stay specs-only? Argument for shipping: agent tooling could run `cgc verify-spec` against a user's local btrain state. Argument against: adds Java (for TLC) to runtime deps. **Deferred; revisit after Phase 3 if demand materialises.**
