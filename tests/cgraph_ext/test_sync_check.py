import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner
import typer

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.commands.sync_check import (
    NO_SOURCE_CHECKOUT_REASON,
    build_sync_check_payload,
)
from codegraphcontext_ext.io.schema_check import validate_payload


runner = CliRunner()


def test_sync_check_cli_emits_success_payload(tmp_path: Path):
    repo = _init_sync_check_repo(tmp_path)
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)

    result = runner.invoke(app, ["sync-check", "--source-dir", str(repo)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    validate_payload("sync-check.json", payload)
    assert payload["upstream"] == "CodeGraphContext/CodeGraphContext"
    assert payload["source_dir"] == str(repo)
    assert payload["behind_by"] == 2
    assert [commit["subject"] for commit in payload["new_commits"]] == [
        "upstream change 2",
        "upstream change 1",
    ]


def test_sync_check_uses_btrain_source_checkout_config(tmp_path: Path):
    repo = _init_sync_check_repo(tmp_path)
    project_dir = tmp_path / "project"
    (project_dir / ".btrain").mkdir(parents=True)
    (project_dir / ".btrain" / "project.toml").write_text(
        '[cgraph]\nsource_checkout = "{path}"\n'.format(path=repo),
        encoding="utf-8",
    )

    payload = build_sync_check_payload(
        btrain_project_dir=project_dir,
        executable_path=tmp_path / "standalone-cgc",
    )

    validate_payload("sync-check.json", payload)
    assert payload["source_dir"] == str(repo)
    assert payload["behind_by"] == 2


def test_sync_check_falls_back_to_cwd_when_btrain_project_dir_not_supplied(
    tmp_path: Path, monkeypatch
):
    repo = _init_sync_check_repo(tmp_path)
    project_dir = tmp_path / "project" / "nested"
    (project_dir.parent / ".btrain").mkdir(parents=True)
    (project_dir.parent / ".btrain" / "project.toml").write_text(
        f'[cgraph]\nsource_checkout = "{repo}"\n',
        encoding="utf-8",
    )
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    payload = build_sync_check_payload(executable_path=tmp_path / "standalone-cgc")

    validate_payload("sync-check.json", payload)
    assert payload["source_dir"] == str(repo)
    assert payload["behind_by"] == 2


def test_sync_check_skips_without_source_checkout(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    payload = build_sync_check_payload(
        btrain_project_dir=project_dir,
        executable_path=tmp_path / "standalone-cgc",
    )

    validate_payload("sync-check.json", payload)
    assert payload == {
        "skipped": True,
        "reason": NO_SOURCE_CHECKOUT_REASON,
        "suggestion": (
            "Set [cgraph].source_checkout in .btrain/project.toml to the path of your "
            "cgraph fork clone, or pass --source-dir. Standalone CLI installs have no "
            "upstream to compare against."
        ),
    }


def test_sync_check_cli_rejects_non_git_source_dir(tmp_path: Path):
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    register_extensions(app)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    result = runner.invoke(app, ["sync-check", "--source-dir", str(not_a_repo)])

    assert result.exit_code != 0
    import re
    clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    normalized = " ".join(clean.replace("│", " ").split())
    assert "is not inside a git checkout" in normalized


def test_sync_check_reports_missing_remote_when_origin_and_upstream_absent(tmp_path: Path):
    repo = tmp_path / "bare-repo"
    repo.mkdir()
    _run_git(repo, "init", "--initial-branch=main")
    _run_git(repo, "config", "user.name", "cgraph tests")
    _run_git(repo, "config", "user.email", "tests@example.com")
    (repo / "demo.txt").write_text("x", encoding="utf-8")
    _run_git(repo, "add", "demo.txt")
    _run_git(repo, "commit", "-m", "base")

    payload = build_sync_check_payload(source_dir=repo)

    validate_payload("sync-check.json", payload)
    assert payload["skipped"] is True
    assert payload["reason"] == "missing_remote"
    assert "origin" in payload["suggestion"] and "upstream" in payload["suggestion"]


def test_sync_check_reports_missing_remote_ref_when_branches_unfetched(tmp_path: Path):
    repo = tmp_path / "no-refs-repo"
    repo.mkdir()
    _run_git(repo, "init", "--initial-branch=main")
    _run_git(repo, "config", "user.name", "cgraph tests")
    _run_git(repo, "config", "user.email", "tests@example.com")
    _run_git(repo, "remote", "add", "origin", "https://github.com/codeslp/cgraph.git")
    _run_git(
        repo,
        "remote",
        "add",
        "upstream",
        "https://github.com/CodeGraphContext/CodeGraphContext.git",
    )
    (repo / "demo.txt").write_text("x", encoding="utf-8")
    _run_git(repo, "add", "demo.txt")
    _run_git(repo, "commit", "-m", "base")

    payload = build_sync_check_payload(source_dir=repo)

    validate_payload("sync-check.json", payload)
    assert payload["skipped"] is True
    assert payload["reason"] == "missing_remote_ref"


def test_sync_check_falls_back_to_install_location_when_no_config(tmp_path: Path):
    repo = _init_sync_check_repo(tmp_path)
    pretend_bin = repo / "bin" / "cgc"
    pretend_bin.parent.mkdir(parents=True)
    pretend_bin.write_text("", encoding="utf-8")

    payload = build_sync_check_payload(
        btrain_project_dir=tmp_path / "no-config-here",
        executable_path=pretend_bin,
    )

    validate_payload("sync-check.json", payload)
    assert payload["source_dir"] == str(repo)
    assert payload["behind_by"] == 2


def test_sync_check_resolves_a_subdirectory_inside_a_git_checkout(tmp_path: Path):
    repo = _init_sync_check_repo(tmp_path)
    subdir = repo / "sub"
    subdir.mkdir()

    payload = build_sync_check_payload(source_dir=subdir)

    validate_payload("sync-check.json", payload)
    assert payload["source_dir"] == str(repo)
    assert payload["behind_by"] == 2


def _init_sync_check_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    _run_git(repo, "init", "--initial-branch=main")
    _run_git(repo, "config", "user.name", "cgraph tests")
    _run_git(repo, "config", "user.email", "tests@example.com")
    _run_git(repo, "remote", "add", "origin", "https://github.com/codeslp/cgraph.git")
    _run_git(
        repo,
        "remote",
        "add",
        "upstream",
        "https://github.com/CodeGraphContext/CodeGraphContext.git",
    )

    tracked_file = repo / "demo.txt"
    tracked_file.write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "demo.txt")
    _run_git(repo, "commit", "-m", "base")
    base_sha = _run_git(repo, "rev-parse", "HEAD")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", base_sha)

    tracked_file.write_text("base\nupstream one\n", encoding="utf-8")
    _run_git(repo, "commit", "-am", "upstream change 1")
    tracked_file.write_text("base\nupstream one\nupstream two\n", encoding="utf-8")
    _run_git(repo, "commit", "-am", "upstream change 2")
    upstream_sha = _run_git(repo, "rev-parse", "HEAD")
    _run_git(repo, "update-ref", "refs/remotes/upstream/main", upstream_sha)

    return repo


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()
