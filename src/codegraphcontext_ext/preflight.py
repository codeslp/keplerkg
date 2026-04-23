"""Fail-closed preflight: block cgraph commands when storage is offline.

Phase 1.5 Step 7. Shared by the Phase 3 btrain adapter, Phase 6 hooks,
and any direct CLI invocation via ``check_storage()``.

The check is deliberately conservative: if the active local backend path
(or HF_HOME) points under a mount-point that is not currently mounted,
we refuse to proceed. Otherwise an embedded backend can silently
recreate the store on the internal drive, masking the real data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _mounted_volumes() -> set[str]:
    """Return the set of currently-mounted volume paths (macOS/Linux)."""
    try:
        out = subprocess.check_output(["mount"], text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return set()
    mounts: set[str] = set()
    for line in out.splitlines():
        parts = line.split(" on ", 1)
        if len(parts) == 2:
            mount_point = parts[1].split(" (", 1)[0].strip()
            mounts.add(mount_point)
    return mounts


def _requires_mount(path: str) -> str | None:
    """If *path* lives under /Volumes/<name>, return that mount-point."""
    p = Path(path).resolve()
    parts = p.parts  # ('/', 'Volumes', 'zombie', 'cgraph', ...)
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return f"/{parts[1]}/{parts[2]}"
    return None


def check_storage() -> dict[str, Any] | None:
    """Return a ``storage_offline`` payload if any cgraph path is on an
    unmounted volume, or *None* if everything looks fine.

    Paths checked (in order):
      1. Active local backend path from env/config
      2. Active FalkorDB socket path from env/config when relevant
      3. ``HF_HOME`` env var

    Returns ``None`` when all paths are either local or on mounted volumes.
    """
    paths_to_check: list[tuple[str, str]] = []  # (label, path)

    try:
        from codegraphcontext.cli.config_manager import get_config_value
    except Exception:
        get_config_value = None

    configured_backend = (
        os.environ.get("CGC_RUNTIME_DB_TYPE")
        or os.environ.get("DEFAULT_DATABASE")
        or (get_config_value("DEFAULT_DATABASE") if get_config_value else None)
        or "falkordb"
    ).lower()

    def _env_or_config(key: str) -> str | None:
        value = os.environ.get(key)
        if value:
            return value
        if get_config_value is None:
            return None
        return get_config_value(key)

    if configured_backend == "kuzudb":
        kuzu_path = _env_or_config("KUZUDB_PATH")
        if kuzu_path:
            paths_to_check.append(("KUZUDB_PATH", kuzu_path))
    elif configured_backend == "falkordb":
        falkor_path = _env_or_config("FALKORDB_PATH")
        if falkor_path:
            paths_to_check.append(("FALKORDB_PATH", falkor_path))
        falkor_socket = _env_or_config("FALKORDB_SOCKET_PATH")
        if falkor_socket:
            paths_to_check.append(("FALKORDB_SOCKET_PATH", falkor_socket))

    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        paths_to_check.append(("HF_HOME", hf_home))

    if not paths_to_check:
        return None

    mounts = _mounted_volumes()

    offline: list[dict[str, str]] = []
    for label, path in paths_to_check:
        mount_point = _requires_mount(path)
        if mount_point and mount_point not in mounts:
            offline.append({
                "variable": label,
                "path": path,
                "mount_point": mount_point,
            })

    if not offline:
        return None

    detail_parts = [
        f"{e['variable']}={e['path']} requires {e['mount_point']}"
        for e in offline
    ]
    return {
        "ok": False,
        "kind": "storage_offline",
        "detail": "; ".join(detail_parts),
        "offline": offline,
    }


def require_storage() -> None:
    """Call from CLI commands or hooks.  Exits with JSON error if offline."""
    result = check_storage()
    if result is not None:
        print(json.dumps(result), file=sys.stdout)
        raise SystemExit(1)
