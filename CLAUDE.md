<!-- btrain:managed:start -->
## Brain Train Workflow

This repo uses the `btrain` collaboration workflow.

- **Always use CLI commands** (`btrain handoff`, `handoff claim`, `handoff update`, `handoff resolve`) to read and update handoff state. Do not read or edit `HANDOFF_*.md` files directly.
- Keep the lane `Delegation Packet` current for active work: `Objective`, `Deliverable`, `Constraints`, `Acceptance checks`, `Budget`, and `Done when`.
- When handing work to a reviewer, always fill the structured handoff fields: `Base`, `Pre-flight review`, `Files changed`, `Verification run`, `Remaining gaps`, `Why this was done`, and `Specific review asks`.
- If the repo provides a `pre-handoff` skill, run it immediately before `btrain handoff update --status needs-review`.
- Run `btrain handoff` before acting so btrain can verify the current agent and tell you whose turn it is.
- Before editing, do a short pre-flight review of the locked files, nearby diff, and likely risk areas so you start from known problems.
- Run `btrain status` or `btrain doctor` if the local workflow files look stale.
- Repo config lives at `.btrain/project.toml`.
- Use the `feedback-triage` skill when processing user-reported issues. It logs entries to `.claude/collab/FEEDBACK_LOG.md` and drives test-first resolution.
- Use the `bug-fix` skill for developer-found bugs. Write a failing reproduction test before editing production code.

### Collaboration Setup

- Active collaborating agents: `claude`, `codex`
- Current lane target: 6 lane(s) (3 per collaborating agent): `a`, `b`, `c`, `d`, `e`, `f`
- Change `[agents].active` or `[lanes].per_agent`, then run `btrain init`, `btrain agents set`, or `btrain agents add` to scaffold missing lanes and refresh docs.

### Multi-Lane Workflow

When `[lanes]` is enabled in `project.toml`, agents work concurrently on separate lanes:

- Use `--lane <id>` (e.g. `a`, `b`, `c`, `d`, `e`, `f`) with `handoff claim|update|resolve`.
- Lock files with `--files "path/"` when claiming to prevent cross-lane collisions.
- Run `btrain locks` to see active file locks.
- When your lane is done, hand it to a peer reviewer while you continue on other work.

<!-- btrain:managed:end -->

## Project-Specific Instructions

Add repo-specific Claude instructions below. `btrain init` only updates the managed block above.
