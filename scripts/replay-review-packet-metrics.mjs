#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";

const DEFAULT_LIMIT = 10;
const DEFAULT_MAX_NODES = 50;
const DEFAULT_DEPTH = 5;

function printUsage() {
  const scriptName = path.basename(process.argv[1] || "replay-review-packet-metrics.mjs");
  console.error(`Usage: node ${scriptName} [options]

Replay recent btrain needs-review handoffs from .btrain/events/lane-*.jsonl
and measure review-packet / blast-radius payload sizes against a raw-diff baseline.

Options:
  --events-dir PATH   Directory containing lane-*.jsonl history files
                      Default: .btrain/events
  --limit N           Number of recent needs-review handoffs to replay
                      Default: ${DEFAULT_LIMIT}
  --lane ID           Only replay a specific lane
  --project SLUG      Explicit cgraph project slug
  --head REF          Head ref to pair with recorded base refs
                      Default: HEAD
  --checkout-ref REF  Ref used for detached replay worktree when isolation is enabled
                      Default: HEAD
  --max-nodes N       Forwarded to kkg review-packet / blast-radius
                      Default: ${DEFAULT_MAX_NODES}
  --depth N           Forwarded to kkg blast-radius
                      Default: ${DEFAULT_DEPTH}
  --kkg-bin PATH      Explicit kkg executable path
  --no-isolate        Replay in the live repo instead of a detached clean worktree
  --help              Show this message
`);
}

function parseArgs(argv) {
  const options = {
    eventsDir: path.resolve(".btrain/events"),
    limit: DEFAULT_LIMIT,
    lane: null,
    project: null,
    head: "HEAD",
    checkoutRef: "HEAD",
    maxNodes: DEFAULT_MAX_NODES,
    depth: DEFAULT_DEPTH,
    kkgBin: null,
    isolate: true,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = argv[index + 1];
    if (arg === "--help" || arg === "-h") {
      printUsage();
      process.exit(0);
    }
    if (arg === "--events-dir") {
      options.eventsDir = path.resolve(requireValue(arg, next));
      index += 1;
      continue;
    }
    if (arg === "--limit") {
      options.limit = parsePositiveInteger(arg, requireValue(arg, next));
      index += 1;
      continue;
    }
    if (arg === "--lane") {
      options.lane = requireValue(arg, next);
      index += 1;
      continue;
    }
    if (arg === "--project") {
      options.project = requireValue(arg, next);
      index += 1;
      continue;
    }
    if (arg === "--head") {
      options.head = requireValue(arg, next);
      index += 1;
      continue;
    }
    if (arg === "--checkout-ref") {
      options.checkoutRef = requireValue(arg, next);
      index += 1;
      continue;
    }
    if (arg === "--max-nodes") {
      options.maxNodes = parsePositiveInteger(arg, requireValue(arg, next));
      index += 1;
      continue;
    }
    if (arg === "--depth") {
      options.depth = parsePositiveInteger(arg, requireValue(arg, next));
      index += 1;
      continue;
    }
    if (arg === "--kkg-bin") {
      options.kkgBin = requireValue(arg, next);
      index += 1;
      continue;
    }
    if (arg === "--no-isolate") {
      options.isolate = false;
      continue;
    }
    throw new Error(`Unknown argument: ${arg}`);
  }

  return options;
}

function requireValue(flag, value) {
  if (!value || value.startsWith("--")) {
    throw new Error(`${flag} requires a value.`);
  }
  return value;
}

function parsePositiveInteger(flag, value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`${flag} must be a positive integer.`);
  }
  return parsed;
}

function detectKkgCommand(repoRoot, override) {
  if (override) {
    return {
      executable: path.isAbsolute(override) ? override : path.resolve(repoRoot, override),
      args: [],
      env: {},
    };
  }

  const discovered = findOnPath("kkg");
  if (discovered) {
    return { executable: discovered, args: [], env: {} };
  }

  const srcPath = path.join(repoRoot, "src");
  const pythonPath = [srcPath, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  return {
    executable: process.env.PYTHON || "python3",
    args: ["-m", "codegraphcontext.cli.main"],
    env: pythonPath ? { PYTHONPATH: pythonPath } : {},
  };
}

function findOnPath(executableName) {
  const searchPath = process.env.PATH || "";
  for (const segment of searchPath.split(path.delimiter)) {
    if (!segment) {
      continue;
    }
    const candidate = path.join(segment, executableName);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      // Keep scanning PATH.
    }
  }
  return null;
}

function collectNeedsReviewEntries(eventsDir, laneFilter, limit) {
  if (!fs.existsSync(eventsDir)) {
    throw new Error(`Events directory does not exist: ${eventsDir}`);
  }

  const laneFiles = fs
    .readdirSync(eventsDir)
    .filter((name) => /^lane-[^.]+\.jsonl$/.test(name))
    .sort();

  const entries = [];
  for (const fileName of laneFiles) {
    const laneId = fileName.slice("lane-".length, -".jsonl".length);
    if (laneFilter && laneId !== laneFilter) {
      continue;
    }
    const filePath = path.join(eventsDir, fileName);
    const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/).filter(Boolean);
    for (const line of lines) {
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      if (event.type !== "update") {
        continue;
      }
      if (event.after?.status !== "needs-review") {
        continue;
      }
      const files = normalizeFiles(
        event.details?.files ?? event.after?.lockedFiles ?? event.before?.lockedFiles ?? [],
      );
      entries.push({
        id: `${event.laneId || laneId}:${event.recordedAt}`,
        recordedAt: event.recordedAt,
        lane: event.laneId || event.after?.lane || laneId,
        actor: event.actor || null,
        task: event.after?.task || event.before?.task || null,
        base: normalizeRef(event.after?.base),
        files,
      });
    }
  }

  entries.sort((left, right) => {
    const leftTime = Date.parse(left.recordedAt || "");
    const rightTime = Date.parse(right.recordedAt || "");
    return rightTime - leftTime;
  });

  return entries.slice(0, limit).reverse();
}

function normalizeRef(value) {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  if (/^[0-9a-f]{7,40}$/i.test(trimmed)) {
    return trimmed;
  }

  const parenMatch = trimmed.match(/\(([0-9a-f]{7,40})\)\s*$/i);
  if (parenMatch?.[1]) {
    return parenMatch[1];
  }

  return trimmed;
}

function normalizeSlug(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function resolveProjectSlug(repoRoot, explicitProject) {
  if (explicitProject) {
    return normalizeSlug(explicitProject);
  }

  if (process.env.CGRAPH_PROJECT) {
    return normalizeSlug(process.env.CGRAPH_PROJECT);
  }

  const projectTomlPath = path.join(repoRoot, ".cgraph", "project.toml");
  if (fs.existsSync(projectTomlPath)) {
    const content = fs.readFileSync(projectTomlPath, "utf8");
    const match = content.match(/^project\s*=\s*["']([^"']+)["']\s*$/m);
    if (match?.[1]) {
      return normalizeSlug(match[1]);
    }
  }

  return normalizeSlug(path.basename(repoRoot));
}

function normalizeFiles(rawFiles) {
  const flattened = Array.isArray(rawFiles) ? rawFiles : [rawFiles];
  const files = [];
  const seen = new Set();

  for (const value of flattened) {
    if (typeof value !== "string") {
      continue;
    }
    for (const token of splitFileSpec(value)) {
      if (!token || seen.has(token)) {
        continue;
      }
      seen.add(token);
      files.push(token);
    }
  }

  return files;
}

function splitFileSpec(input) {
  const tokens = [];
  let current = "";
  let quote = null;

  const pushCurrent = () => {
    const trimmed = current.trim();
    if (trimmed) {
      tokens.push(trimmed);
    }
    current = "";
  };

  for (const char of input) {
    if (quote) {
      if (char === quote) {
        quote = null;
      } else {
        current += char;
      }
      continue;
    }

    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }

    if (char === "," || /\s/.test(char)) {
      pushCurrent();
      continue;
    }

    current += char;
  }

  pushCurrent();
  return tokens;
}

function runProcess(executable, args, { cwd, env }) {
  const result = spawnSync(executable, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
  return {
    status: result.status ?? 1,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    error: result.error ? String(result.error.message || result.error) : null,
  };
}

function runGit(repoRoot, args) {
  return runProcess("git", args, { cwd: repoRoot, env: {} });
}

function repoIsDirty(repoRoot) {
  const status = runGit(repoRoot, ["status", "--porcelain"]);
  return status.status === 0 && Boolean(status.stdout.trim());
}

function createReplayWorkspace(repoRoot, checkoutRef) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "cgraph-replay-"));
  const result = runGit(repoRoot, ["worktree", "add", "--detach", "--quiet", tempDir, checkoutRef]);
  if (result.status !== 0) {
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {
      // best effort cleanup
    }
    throw new Error(result.stderr.trim() || result.error || "git worktree add failed");
  }
  return tempDir;
}

function removeReplayWorkspace(repoRoot, workspacePath) {
  runGit(repoRoot, ["worktree", "remove", "--force", workspacePath]);
  try {
    fs.rmSync(workspacePath, { recursive: true, force: true });
  } catch {
    // best effort cleanup
  }
}

function refExists(repoRoot, ref) {
  const probe = runGit(repoRoot, ["rev-parse", "--verify", ref]);
  return probe.status === 0;
}

function replayReviewPacket(entry, options, kkgCommand) {
  if (entry.files.length === 0) {
    return {
      ok: false,
      skipped: true,
      reason: "no_files",
      command: [],
      bytes: 0,
      approxTokens: 0,
      source: null,
      advisoriesCount: 0,
      stderr: "",
      error: null,
      exitCode: 0,
    };
  }

  const args = ["review-packet", "--files", entry.files.join(","), "--max-nodes", String(options.maxNodes)];
  if (entry.base && entry.base !== options.head) {
    args.push("--base", entry.base, "--head", options.head);
  }
  if (options.project) {
    args.push("--project", options.project);
  }

  const command = [...kkgCommand.args, ...args];
  const result = runProcess(kkgCommand.executable, command, {
    cwd: options.commandRepoRoot,
    env: kkgCommand.env,
  });

  const trimmedStdout = result.stdout.trim();
  const payload = tryParseJson(trimmedStdout);
  return {
    ok: result.status === 0 && payload !== null,
    skipped: false,
    reason: null,
    command,
    bytes: byteLength(trimmedStdout),
    approxTokens: trimmedStdout ? estimateTokens(trimmedStdout) : 0,
    source: payload?.source ?? null,
    advisoriesCount: Array.isArray(payload?.advisories) ? payload.advisories.length : 0,
    stderr: result.stderr.trim(),
    error: payload === null && trimmedStdout ? "invalid_json" : result.error,
    exitCode: result.status,
  };
}

function replayBlastRadius(entry, options, kkgCommand) {
  if (entry.files.length === 0) {
    return {
      ok: false,
      skipped: true,
      reason: "no_files",
      command: [],
      bytes: 0,
      approxTokens: 0,
      advisoriesCount: 0,
      stderr: "",
      error: null,
      exitCode: 0,
    };
  }

  const args = [
    "blast-radius",
    "--files",
    entry.files.join(","),
    "--max-nodes",
    String(options.maxNodes),
    "--depth",
    String(options.depth),
  ];
  if (entry.lane) {
    args.push("--lane", entry.lane);
  }
  if (options.project) {
    args.push("--project", options.project);
  }

  const command = [...kkgCommand.args, ...args];
  const result = runProcess(kkgCommand.executable, command, {
    cwd: options.commandRepoRoot,
    env: kkgCommand.env,
  });

  const trimmedStdout = result.stdout.trim();
  const payload = tryParseJson(trimmedStdout);
  return {
    ok: result.status === 0 && payload !== null,
    skipped: false,
    reason: null,
    command,
    bytes: byteLength(trimmedStdout),
    approxTokens: trimmedStdout ? estimateTokens(trimmedStdout) : 0,
    advisoriesCount: Array.isArray(payload?.advisories) ? payload.advisories.length : 0,
    stderr: result.stderr.trim(),
    error: payload === null && trimmedStdout ? "invalid_json" : result.error,
    exitCode: result.status,
  };
}

function buildRawDiffBaseline(entry, options) {
  if (entry.files.length === 0) {
    return {
      mode: "none",
      command: [],
      bytes: 0,
      approxTokens: 0,
      notes: ["no_files"],
    };
  }

  const attempts = [];
  if (
    entry.base &&
    entry.base !== options.head &&
    refExists(options.commandRepoRoot, entry.base) &&
    refExists(options.commandRepoRoot, options.head)
  ) {
    attempts.push({
      mode: "base_head",
      args: ["diff", "--no-ext-diff", "--minimal", `${entry.base}..${options.head}`, "--", ...entry.files],
    });
  }

  if (refExists(options.commandRepoRoot, "HEAD")) {
    attempts.push({
      mode: "worktree_head",
      args: ["diff", "--no-ext-diff", "--minimal", "HEAD", "--", ...entry.files],
    });
    attempts.push({
      mode: "head_commit",
      args: ["show", "--no-ext-diff", "--format=medium", "HEAD", "--", ...entry.files],
    });
  }

  const notes = [];
  for (const attempt of attempts) {
    const result = runGit(options.commandRepoRoot, attempt.args);
    const trimmedStdout = result.stdout.trim();
    if (result.status === 0 && trimmedStdout) {
      return {
        mode: attempt.mode,
        command: attempt.args,
        bytes: byteLength(trimmedStdout),
        approxTokens: estimateTokens(trimmedStdout),
        notes,
      };
    }
    if (result.error) {
      notes.push(`${attempt.mode}:${result.error}`);
    } else if (result.status !== 0) {
      notes.push(`${attempt.mode}:exit_${result.status}`);
    } else {
      notes.push(`${attempt.mode}:empty`);
    }
  }

  return {
    mode: "none",
    command: [],
    bytes: 0,
    approxTokens: 0,
    notes,
  };
}

function estimateTokens(text) {
  return Math.max(1, Math.floor(text.length / 4));
}

function byteLength(text) {
  return Buffer.byteLength(text, "utf8");
}

function tryParseJson(text) {
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function summarize(results) {
  const summary = {
    total_entries: results.length,
    replayed_entries: 0,
    comparable_entries: 0,
    skipped_entries: 0,
    failed_entries: 0,
    review_packet_tokens_total: 0,
    comparable_review_packet_tokens_total: 0,
    blast_radius_tokens_total: 0,
    raw_diff_tokens_total: 0,
    review_vs_raw_token_reduction_pct: null,
    by_packet_source: {},
    by_raw_diff_mode: {},
  };

  for (const result of results) {
    if (result.review_packet.skipped) {
      summary.skipped_entries += 1;
      continue;
    }

    if (result.review_packet.ok && result.blast_radius.ok) {
      summary.replayed_entries += 1;
    } else {
      summary.failed_entries += 1;
    }

    if (result.review_packet.ok) {
      summary.review_packet_tokens_total += result.review_packet.approxTokens;
      if (result.review_packet.source) {
        summary.by_packet_source[result.review_packet.source] =
          (summary.by_packet_source[result.review_packet.source] || 0) + 1;
      }
    }

    if (result.blast_radius.ok) {
      summary.blast_radius_tokens_total += result.blast_radius.approxTokens;
    }

    if (result.raw_diff.mode !== "none" && result.review_packet.ok) {
      summary.comparable_entries += 1;
      summary.comparable_review_packet_tokens_total += result.review_packet.approxTokens;
      summary.raw_diff_tokens_total += result.raw_diff.approxTokens;
      summary.by_raw_diff_mode[result.raw_diff.mode] =
        (summary.by_raw_diff_mode[result.raw_diff.mode] || 0) + 1;
    }
  }

  if (summary.raw_diff_tokens_total > 0) {
    const reduction =
      100 - (summary.comparable_review_packet_tokens_total / summary.raw_diff_tokens_total) * 100;
    summary.review_vs_raw_token_reduction_pct = Number(reduction.toFixed(2));
  }

  return summary;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  options.repoRoot = process.cwd();
  options.commandRepoRoot = options.repoRoot;
  options.project = resolveProjectSlug(options.repoRoot, options.project);

  const kkgCommand = detectKkgCommand(options.repoRoot, options.kkgBin);
  const entries = collectNeedsReviewEntries(options.eventsDir, options.lane, options.limit);
  let workspaceMode = "live-repo";
  let workspacePath = null;

  if (options.isolate && repoIsDirty(options.repoRoot)) {
    workspacePath = createReplayWorkspace(options.repoRoot, options.checkoutRef);
    options.commandRepoRoot = workspacePath;
    workspaceMode = "detached-worktree";
  }

  try {
    const results = entries.map((entry) => {
      const reviewPacket = replayReviewPacket(entry, options, kkgCommand);
      const blastRadius = replayBlastRadius(entry, options, kkgCommand);
      const rawDiff = buildRawDiffBaseline(entry, options);
      return {
        id: entry.id,
        recorded_at: entry.recordedAt,
        lane: entry.lane,
        actor: entry.actor,
        task: entry.task,
        base: entry.base,
        head: options.head,
        files: entry.files,
        review_packet: reviewPacket,
        blast_radius: blastRadius,
        raw_diff: rawDiff,
      };
    });

    const payload = {
      generated_at: new Date().toISOString(),
      repo_root: options.repoRoot,
      events_dir: options.eventsDir,
      project: options.project,
      limit: options.limit,
      workspace: {
        mode: workspaceMode,
        ref: options.checkoutRef,
      },
      tokenizer: {
        mode: "approximate_chars_div_4",
        detail: "Matches context._estimate_tokens(): max(1, floor(chars / 4)).",
      },
      kkg_command: [kkgCommand.executable, ...kkgCommand.args],
      summary: summarize(results),
      results,
    };

    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } finally {
    if (workspacePath) {
      removeReplayWorkspace(options.repoRoot, workspacePath);
    }
  }
}

try {
  main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
