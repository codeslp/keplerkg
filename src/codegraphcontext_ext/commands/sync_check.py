"""Implementation for the `sync-check` cgraph extension command."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any, Optional

import typer

from ..io.json_stdout import emit_json

COMMAND_NAME = "sync-check"
SCHEMA_FILE = "sync-check.json"
SUMMARY = "Report upstream commits not yet merged into the cgraph fork."

NO_SOURCE_CHECKOUT_REASON = "no_source_checkout"
NO_SOURCE_CHECKOUT_SUGGESTION = (
    "Set [cgraph].source_checkout in .btrain/project.toml to the path of your "
    "cgraph fork clone, or pass --source-dir. Standalone CLI installs have no "
    "upstream to compare against."
)


class SyncCheckInputError(ValueError):
    """Raised when a user-provided source checkout cannot be used."""


def sync_check_command(
    source_dir: Optional[Path] = typer.Option(
        None,
        "--source-dir",
        dir_okay=True,
        file_okay=False,
        exists=False,
        readable=True,
        resolve_path=True,
        help="Path to a cgraph source checkout with origin/upstream remotes.",
    ),
) -> None:
    """Emit a JSON sync report or a skipped payload."""

    try:
        payload = build_sync_check_payload(source_dir=source_dir)
    except SyncCheckInputError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(emit_json(payload))


def build_sync_check_payload(
    *,
    source_dir: Optional[Path] = None,
    btrain_project_dir: Optional[Path] = None,
    executable_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Build the sync-check response described by the cgraph spec."""

    resolved_source_dir = resolve_source_checkout(
        source_dir=source_dir,
        btrain_project_dir=btrain_project_dir,
        executable_path=executable_path,
    )
    if resolved_source_dir is None:
        return skipped_payload(
            reason=NO_SOURCE_CHECKOUT_REASON,
            suggestion=NO_SOURCE_CHECKOUT_SUGGESTION,
        )

    missing_remotes = tuple(
        remote_name
        for remote_name in ("origin", "upstream")
        if not _has_remote(resolved_source_dir, remote_name)
    )
    if missing_remotes:
        missing_list = ", ".join(missing_remotes)
        return skipped_payload(
            reason="missing_remote",
            suggestion=(
                f"Configure {missing_list} remote(s) in {resolved_source_dir} so "
                "sync-check can compare origin/main against upstream/main."
            ),
        )

    try:
        local_head = _run_git(resolved_source_dir, "rev-parse", "origin/main")
        upstream_head = _run_git(resolved_source_dir, "rev-parse", "upstream/main")
        new_commits = _list_new_commits(resolved_source_dir)
    except subprocess.CalledProcessError:
        return skipped_payload(
            reason="missing_remote_ref",
            suggestion=(
                "Fetch origin/main and upstream/main in the source checkout before "
                "running sync-check."
            ),
        )

    return {
        "upstream": _remote_label(_run_git(resolved_source_dir, "remote", "get-url", "upstream")),
        "source_dir": str(resolved_source_dir),
        "local_head": local_head,
        "upstream_head": upstream_head,
        "behind_by": len(new_commits),
        "new_commits": new_commits,
    }


def resolve_source_checkout(
    *,
    source_dir: Optional[Path],
    btrain_project_dir: Optional[Path],
    executable_path: Optional[Path],
) -> Optional[Path]:
    """Resolve the cgraph source checkout using the spec's precedence order."""

    if source_dir is not None:
        return _resolve_explicit_checkout(source_dir)

    config_root = _find_btrain_project_root(btrain_project_dir or Path.cwd())
    if config_root is not None:
        configured = _source_checkout_from_project_config(config_root / ".btrain" / "project.toml")
        if configured is not None:
            resolved = _git_toplevel(configured)
            if resolved is not None:
                return resolved

    return _checkout_from_install_location(executable_path)


def skipped_payload(*, reason: str, suggestion: str) -> dict[str, Any]:
    """Build a skipped payload that still exits cleanly."""

    return {
        "skipped": True,
        "reason": reason,
        "suggestion": suggestion,
    }


def _resolve_explicit_checkout(source_dir: Path) -> Path:
    resolved = _git_toplevel(source_dir)
    if resolved is None:
        raise SyncCheckInputError(
            f"{source_dir} is not inside a git checkout with a readable .git directory."
        )
    return resolved


def _find_btrain_project_root(start_dir: Path) -> Optional[Path]:
    for candidate in (start_dir, *start_dir.parents):
        if (candidate / ".btrain" / "project.toml").is_file():
            return candidate
    return None


def _source_checkout_from_project_config(project_toml: Path) -> Optional[Path]:
    current_section: tuple[str, ...] = ()
    for raw_line in project_toml.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = tuple(part.strip() for part in line[1:-1].split("."))
            continue
        if current_section != ("cgraph",) or not line.startswith("source_checkout"):
            continue

        key, _, value = line.partition("=")
        if key.strip() != "source_checkout":
            continue

        parsed_value = _parse_toml_string(value.strip())
        if not parsed_value:
            return None
        return Path(parsed_value).expanduser()

    return None


def _parse_toml_string(value: str) -> str:
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        return str(ast.literal_eval(value))
    return value


def _checkout_from_install_location(executable_path: Optional[Path]) -> Optional[Path]:
    candidate = executable_path or Path(__file__).resolve()
    for path in (candidate, *candidate.parents):
        resolved = _git_toplevel(path)
        if resolved is None:
            continue
        if _has_remote(resolved, "upstream"):
            return resolved
    return None


def _git_toplevel(path: Path) -> Optional[Path]:
    try:
        output = _run_git(path, "rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        return None
    return Path(output).resolve()


def _has_remote(source_dir: Path, remote_name: str) -> bool:
    try:
        remotes = _run_git(source_dir, "remote").splitlines()
    except subprocess.CalledProcessError:
        return False
    return remote_name in remotes


def _list_new_commits(source_dir: Path) -> list[dict[str, str]]:
    log_output = _run_git(
        source_dir,
        "log",
        "--format=%H%x00%s",
        "origin/main..upstream/main",
    )
    commits: list[dict[str, str]] = []
    if not log_output:
        return commits

    for line in log_output.splitlines():
        sha, _, subject = line.partition("\x00")
        commits.append({"sha": sha, "subject": subject})
    return commits


def _remote_label(remote_url: str) -> str:
    trimmed = remote_url.rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[:-4]

    path_part = trimmed
    if "://" in trimmed:
        _, _, remainder = trimmed.partition("://")
        _, _, path_part = remainder.partition("/")
    elif ":" in trimmed and "@" in trimmed.split(":", 1)[0]:
        _, _, path_part = trimmed.partition(":")

    parts = [part for part in path_part.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return trimmed


def _run_git(source_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_dir), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()
