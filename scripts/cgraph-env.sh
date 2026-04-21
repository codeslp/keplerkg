#!/usr/bin/env bash
# cgraph-env.sh — source this BEFORE running kkg/cgraph commands in a new shell.
#
# Sets HF_HOME so embedding model downloads use the migrated zombie cache, and
# verifies /Volumes/zombie is mounted so upstream's KuzuDBManager() (which reads
# KUZUDB_PATH from ~/.codegraphcontext/.env) does not silently fail.
#
# Usage:
#   source scripts/cgraph-env.sh
#
# The Python preflight in src/codegraphcontext_ext/preflight.py provides the
# same fail-closed check at runtime. This wrapper catches problems earlier,
# before any Python process starts.

set -u

ZOMBIE_MOUNT="/Volumes/zombie"
ZOMBIE_HF_CACHE="${ZOMBIE_MOUNT}/cgraph/hf-cache"
ZOMBIE_KUZU_ROOT="${ZOMBIE_MOUNT}/cgraph/db"
# Spec 004: per-project stores live under db/<slug>/kuzudb.
# The default project is "cgraph".
ZOMBIE_KUZU_DEFAULT="${ZOMBIE_KUZU_ROOT}/cgraph/kuzudb"

is_actual_mount() {
    local mount_point
    mount_point="$1"

    command -v mount >/dev/null 2>&1 || return 1
    mount | awk -v mount_point="${mount_point}" '
        $2 == "on" && $3 == mount_point { found = 1 }
        END { exit found ? 0 : 1 }
    '
}

if ! is_actual_mount "${ZOMBIE_MOUNT}"; then
    echo "cgraph-env: ERROR — ${ZOMBIE_MOUNT} is not mounted." >&2
    echo "cgraph-env: refuse to continue; kkg would silently write to the internal drive." >&2
    return 1 2>/dev/null || exit 1
fi

if [ ! -d "${ZOMBIE_HF_CACHE}" ]; then
    echo "cgraph-env: ERROR — ${ZOMBIE_HF_CACHE} missing." >&2
    echo "cgraph-env: has the Phase 1.5 migration been run?" >&2
    return 1 2>/dev/null || exit 1
fi

if [ ! -f "${ZOMBIE_KUZU_DEFAULT}" ]; then
    echo "cgraph-env: WARNING — ${ZOMBIE_KUZU_DEFAULT} not present." >&2
    echo "cgraph-env: the next kkg index will create a fresh store at that path." >&2
fi

export HF_HOME="${ZOMBIE_HF_CACHE}"
export SENTENCE_TRANSFORMERS_HOME="${ZOMBIE_HF_CACHE}"
export CGC_RUNTIME_DB_TYPE="kuzudb"

# Read the actual KUZUDB_PATH from upstream config so the status line is accurate.
_kuzudb_path="${ZOMBIE_KUZU_DEFAULT}"
if [ -f "${HOME}/.codegraphcontext/.env" ]; then
    _read_path="$(grep -E '^KUZUDB_PATH=' "${HOME}/.codegraphcontext/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    [ -n "${_read_path}" ] && _kuzudb_path="${_read_path}"
fi

echo "cgraph-env: HF_HOME=${HF_HOME}"
echo "cgraph-env: CGC_RUNTIME_DB_TYPE=${CGC_RUNTIME_DB_TYPE}"
echo "cgraph-env: KUZUDB_PATH → ${_kuzudb_path}"

# List any project stores that exist
if [ -d "${ZOMBIE_KUZU_ROOT}" ]; then
    _projects="$(find "${ZOMBIE_KUZU_ROOT}" -name kuzudb -maxdepth 2 2>/dev/null | sed "s|${ZOMBIE_KUZU_ROOT}/||;s|/kuzudb||" | sort | tr '\n' ', ' | sed 's/,$//')"
    [ -n "${_projects}" ] && echo "cgraph-env: project stores: ${_projects}"
fi

