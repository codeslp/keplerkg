"""Fail-closed preflight: block cgraph commands when storage is offline.

Phase 1.5 Step 7.  Shared by the Phase 3 btrain adapter, Phase 6 hooks,
and any direct CLI invocation via ``check_storage()``.

The check is deliberately conservative: if KUZUDB_PATH (or HF_HOME)
points under a mount-point that is not currently mounted, we refuse to
proceed — upstream's ``KuzuDBManager`` would silently ``makedirs`` the
path on the internal drive, creating an empty store that masks the real
data.
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
      1. ``KUZUDB_PATH`` env var
      2. ``KUZUDB_PATH`` from ``~/.codegraphcontext/.env`` (via upstream)
      3. ``HF_HOME`` env var

    Returns ``None`` when all paths are either local or on mounted volumes.
    """
    paths_to_check: list[tuple[str, str]] = []  # (label, path)

    # KUZUDB_PATH — env first, then upstream config
    kuzu_env = os.environ.get("KUZUDB_PATH")
    if kuzu_env:
        paths_to_check.append(("KUZUDB_PATH", kuzu_env))
    else:
        try:
            from codegraphcontext.cli.config_manager import get_config_value
            kuzu_cfg = get_config_value("KUZUDB_PATH")
            if kuzu_cfg:
                paths_to_check.append(("KUZUDB_PATH", kuzu_cfg))
        except Exception:
            pass

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
