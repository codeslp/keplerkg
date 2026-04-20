from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


NODE_BIN = shutil.which("node")
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "replay-review-packet-metrics.mjs"

pytestmark = pytest.mark.skipif(NODE_BIN is None, reason="node is required for replay harness tests")


def test_replay_harness_replays_recent_needs_review_events(tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    events_dir = repo / ".btrain" / "events"
    events_dir.mkdir(parents=True)

    source_file = repo / "src" / "demo.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("print('base')\n", encoding="utf-8")
    _run_git(repo, "add", "src/demo.py")
    _run_git(repo, "commit", "-m", "base")
    base_ref = _run_git(repo, "rev-parse", "HEAD").strip()

    source_file.write_text("print('base')\nprint('head')\n", encoding="utf-8")
    _run_git(repo, "commit", "-am", "head")

    _write_events(
        events_dir / "lane-a.jsonl",
        [
            {
                "version": 1,
                "recordedAt": "2026-04-19T23:00:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Replay harness sample",
                    "base": base_ref,
                    "lockedFiles": ["src/demo.py"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["src/demo.py"],
                },
            }
        ],
    )

    fake_kkg = _write_fake_kkg(tmp_path)
    log_path = tmp_path / "kkg-log.jsonl"
    payload = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            str(fake_kkg),
            "--limit",
            "5",
        ],
        extra_env={"FAKE_KKG_LOG": str(log_path)},
    )

    assert payload["summary"]["total_entries"] == 1
    assert payload["summary"]["replayed_entries"] == 1
    assert payload["summary"]["comparable_entries"] == 1
    assert payload["summary"]["by_packet_source"] == {"diff": 1}
    assert payload["summary"]["by_raw_diff_mode"] == {"base_head": 1}
    assert payload["summary"]["review_packet_tokens_total"] > 0
    assert payload["summary"]["raw_diff_tokens_total"] > 0

    sample = payload["results"][0]
    assert sample["lane"] == "a"
    assert sample["task"] == "Replay harness sample"
    assert sample["files"] == ["src/demo.py"]
    assert sample["review_packet"]["ok"] is True
    assert sample["review_packet"]["source"] == "diff"
    assert sample["blast_radius"]["ok"] is True
    assert sample["raw_diff"]["mode"] == "base_head"
    assert sample["raw_diff"]["approxTokens"] > 0

    logged_calls = _read_jsonl(log_path)
    assert any(call["argv"][0] == "review-packet" for call in logged_calls)
    assert any(call["argv"][0] == "blast-radius" for call in logged_calls)
    review_call = next(call for call in logged_calls if call["argv"][0] == "review-packet")
    assert "--base" in review_call["argv"]
    assert "--head" in review_call["argv"]
    assert review_call["argv"][review_call["argv"].index("--files") + 1] == "src/demo.py"


def test_replay_harness_normalizes_combined_file_specs_and_skips_non_review_events(
    tmp_path: Path,
):
    repo = _init_git_repo(tmp_path / "repo")
    events_dir = repo / ".btrain" / "events"
    events_dir.mkdir(parents=True)

    src_a = repo / "src" / "a.py"
    src_b = repo / "src" / "b.py"
    test_file = repo / "tests" / "test_a.py"
    src_a.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)

    src_a.write_text("A = 1\n", encoding="utf-8")
    src_b.write_text("B = 1\n", encoding="utf-8")
    test_file.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    _run_git(repo, "add", "src/a.py", "src/b.py", "tests/test_a.py")
    _run_git(repo, "commit", "-m", "base")

    src_a.write_text("A = 2\n", encoding="utf-8")
    src_b.write_text("B = 2\n", encoding="utf-8")

    _write_events(
        events_dir / "lane-a.jsonl",
        [
            {
                "version": 1,
                "recordedAt": "2026-04-19T22:59:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "in-progress",
                    "task": "Ignore me",
                    "base": "",
                    "lockedFiles": ["src/a.py"],
                },
                "details": {"files": ["src/a.py"]},
            },
            {
                "version": 1,
                "recordedAt": "2026-04-19T23:01:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Normalize file list",
                    "base": "",
                    "lockedFiles": ["src/a.py src/b.py", "tests/test_a.py"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["src/a.py src/b.py", "tests/test_a.py"],
                },
            },
        ],
    )

    fake_kkg = _write_fake_kkg(tmp_path)
    log_path = tmp_path / "kkg-log.jsonl"
    payload = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            str(fake_kkg),
            "--lane",
            "a",
        ],
        extra_env={"FAKE_KKG_LOG": str(log_path)},
    )

    assert payload["summary"]["total_entries"] == 1
    assert payload["summary"]["replayed_entries"] == 1
    assert payload["summary"]["comparable_entries"] == 1
    assert payload["summary"]["by_packet_source"] == {"workdir": 1}
    assert payload["summary"]["by_raw_diff_mode"] == {"head_commit": 1}

    sample = payload["results"][0]
    assert sample["files"] == ["src/a.py", "src/b.py", "tests/test_a.py"]
    assert sample["review_packet"]["source"] == "workdir"
    assert sample["raw_diff"]["mode"] == "head_commit"

    review_call = next(call for call in _read_jsonl(log_path) if call["argv"][0] == "review-packet")
    files_arg = review_call["argv"][review_call["argv"].index("--files") + 1]
    assert files_arg == "src/a.py,src/b.py,tests/test_a.py"
    assert "--base" not in review_call["argv"]
    assert payload["workspace"]["mode"] == "detached-worktree"


def test_replay_harness_isolates_dirty_repo_and_forwards_project(tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    events_dir = repo / ".btrain" / "events"
    events_dir.mkdir(parents=True)

    project_dir = repo / ".cgraph"
    project_dir.mkdir()
    (project_dir / "project.toml").write_text('project = "demo-target"\n', encoding="utf-8")

    tracked_file = repo / "src" / "tracked.py"
    tracked_file.parent.mkdir(parents=True)
    tracked_file.write_text("print('clean')\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "base")

    tracked_file.write_text("print('dirty dirty dirty dirty dirty dirty')\n", encoding="utf-8")

    _write_events(
        events_dir / "lane-a.jsonl",
        [
            {
                "version": 1,
                "recordedAt": "2026-04-19T23:02:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Dirty repo sample",
                    "base": "",
                    "lockedFiles": ["src/tracked.py"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["src/tracked.py"],
                },
            }
        ],
    )

    fake_kkg = _write_fake_kkg(tmp_path)
    isolated_log = tmp_path / "isolated-log.jsonl"
    isolated = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            str(fake_kkg),
        ],
        extra_env={"FAKE_KKG_LOG": str(isolated_log)},
    )

    live_log = tmp_path / "live-log.jsonl"
    live = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            str(fake_kkg),
            "--no-isolate",
        ],
        extra_env={"FAKE_KKG_LOG": str(live_log)},
    )

    assert isolated["workspace"]["mode"] == "detached-worktree"
    assert live["workspace"]["mode"] == "live-repo"
    assert isolated["project"] == "demo-target"
    assert isolated["results"][0]["raw_diff"]["mode"] == "head_commit"
    assert live["results"][0]["raw_diff"]["mode"] == "worktree_head"
    assert isolated["results"][0]["raw_diff"]["approxTokens"] != live["results"][0]["raw_diff"]["approxTokens"]

    isolated_calls = _read_jsonl(isolated_log)
    review_call = next(call for call in isolated_calls if call["argv"][0] == "review-packet")
    assert "--project" in review_call["argv"]
    assert review_call["argv"][review_call["argv"].index("--project") + 1] == "demo-target"


def test_replay_harness_resolves_relative_kkg_bin_in_detached_worktree(tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    events_dir = repo / ".btrain" / "events"
    events_dir.mkdir(parents=True)

    tracked_file = repo / "src" / "tracked.py"
    tracked_file.parent.mkdir(parents=True)
    tracked_file.write_text("print('clean')\n", encoding="utf-8")
    _run_git(repo, "add", "src/tracked.py")
    _run_git(repo, "commit", "-m", "base")

    tracked_file.write_text("print('dirty')\n", encoding="utf-8")

    _write_events(
        events_dir / "lane-a.jsonl",
        [
            {
                "version": 1,
                "recordedAt": "2026-04-20T02:05:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Relative kkg path",
                    "base": "",
                    "lockedFiles": ["src/tracked.py"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["src/tracked.py"],
                },
            }
        ],
    )

    local_kkg = _write_fake_kkg(repo / ".venv" / "bin" / "kkg")
    log_path = tmp_path / "kkg-log.jsonl"
    payload = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            ".venv/bin/kkg",
        ],
        extra_env={"FAKE_KKG_LOG": str(log_path)},
    )

    assert payload["workspace"]["mode"] == "detached-worktree"
    assert payload["summary"]["replayed_entries"] == 1
    assert payload["kkg_command"][0] == str(local_kkg)


def test_replay_harness_reduction_uses_only_comparable_entries(tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    events_dir = repo / ".btrain" / "events"
    events_dir.mkdir(parents=True)

    changed_file = repo / "src" / "changed.py"
    unchanged_file = repo / "docs" / "notes.md"
    changed_file.parent.mkdir(parents=True)
    unchanged_file.parent.mkdir(parents=True)

    changed_file.write_text("print('base')\n", encoding="utf-8")
    unchanged_file.write_text("notes\n", encoding="utf-8")
    _run_git(repo, "add", "src/changed.py", "docs/notes.md")
    _run_git(repo, "commit", "-m", "base")

    changed_file.write_text("print('base')\nprint('head')\n", encoding="utf-8")
    _run_git(repo, "commit", "-am", "head")

    _write_events(
        events_dir / "lane-a.jsonl",
        [
            {
                "version": 1,
                "recordedAt": "2026-04-20T02:00:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Comparable replay sample",
                    "base": _run_git(repo, "rev-parse", "HEAD~1").strip(),
                    "lockedFiles": ["src/changed.py"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["src/changed.py"],
                },
            },
            {
                "version": 1,
                "recordedAt": "2026-04-20T02:01:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Locked-files-only replay sample",
                    "base": _run_git(repo, "rev-parse", "HEAD").strip(),
                    "lockedFiles": ["docs/notes.md"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["docs/notes.md"],
                },
            },
        ],
    )

    fake_kkg = _write_fake_kkg(tmp_path)
    payload = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            str(fake_kkg),
            "--limit",
            "2",
        ],
    )

    summary = payload["summary"]
    assert summary["total_entries"] == 2
    assert summary["comparable_entries"] == 1
    assert summary["raw_diff_tokens_total"] > 0
    assert summary["review_packet_tokens_total"] > summary["comparable_review_packet_tokens_total"]

    expected = round(
        100
        - (summary["comparable_review_packet_tokens_total"] / summary["raw_diff_tokens_total"]) * 100,
        2,
    )
    assert summary["review_vs_raw_token_reduction_pct"] == expected


def test_replay_harness_normalizes_recorded_base_refs(tmp_path: Path):
    repo = _init_git_repo(tmp_path / "repo")
    events_dir = repo / ".btrain" / "events"
    events_dir.mkdir(parents=True)

    source_file = repo / "src" / "demo.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("print('base')\n", encoding="utf-8")
    _run_git(repo, "add", "src/demo.py")
    _run_git(repo, "commit", "-m", "base")
    base_ref = _run_git(repo, "rev-parse", "--short", "HEAD").strip()

    source_file.write_text("print('base')\nprint('head')\n", encoding="utf-8")
    _run_git(repo, "commit", "-am", "head")

    _write_events(
        events_dir / "lane-a.jsonl",
        [
            {
                "version": 1,
                "recordedAt": "2026-04-20T02:10:00.000Z",
                "type": "update",
                "actor": "codex",
                "laneId": "a",
                "after": {
                    "status": "needs-review",
                    "task": "Normalize recorded base",
                    "base": f"main ({base_ref})",
                    "lockedFiles": ["src/demo.py"],
                },
                "details": {
                    "requestedStatus": "needs-review",
                    "files": ["src/demo.py"],
                },
            }
        ],
    )

    fake_kkg = _write_fake_kkg(tmp_path / "fake-kkg.py")
    log_path = tmp_path / "kkg-log.jsonl"
    payload = _run_harness(
        repo,
        [
            "--events-dir",
            str(events_dir),
            "--kkg-bin",
            str(fake_kkg),
        ],
        extra_env={"FAKE_KKG_LOG": str(log_path)},
    )

    assert payload["summary"]["comparable_entries"] == 1
    assert payload["results"][0]["raw_diff"]["mode"] == "base_head"

    review_call = next(call for call in _read_jsonl(log_path) if call["argv"][0] == "review-packet")
    assert review_call["argv"][review_call["argv"].index("--base") + 1] == base_ref


def _run_harness(repo: Path, args: list[str], extra_env: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [NODE_BIN, str(SCRIPT_PATH), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True)
    _run_git(repo, "init", "--initial-branch=main")
    _run_git(repo, "config", "user.name", "cgraph tests")
    _run_git(repo, "config", "user.email", "tests@example.com")
    return repo


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _write_fake_kkg(script_path: Path) -> Path:
    if script_path.exists() and script_path.is_dir():
        script_path = script_path / "fake-kkg.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            log_path = os.environ.get("FAKE_KKG_LOG")
            if log_path:
                with Path(log_path).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")

            command = sys.argv[1]
            if command == "review-packet":
                source = "diff" if "--base" in sys.argv else "workdir"
                print(json.dumps({
                    "ok": True,
                    "kind": "review_packet",
                    "source": source,
                    "advisories": [{"level": "warn", "kind": "sample"}],
                    "summary": {"files": 1},
                }))
                raise SystemExit(0)

            if command == "blast-radius":
                print(json.dumps({
                    "ok": True,
                    "kind": "blast_radius",
                    "advisories": [],
                    "summary": {"files_requested": 1},
                }))
                raise SystemExit(0)

            print(json.dumps({"ok": False, "kind": "unsupported"}))
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    return script_path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout
