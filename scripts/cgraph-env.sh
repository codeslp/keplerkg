#!/usr/bin/env bash
# cgraph-env.sh — source this BEFORE running kkg/cgraph commands in a new shell.
#
# Sets HF_HOME so embedding model downloads use the migrated zombie cache, and
# verifies /Volumes/zombie is mounted so the active backend manager (reads
# FALKORDB_PATH / FALKORDB_SOCKET_PATH or KUZUDB_PATH from
# ~/.codegraphcontext/.env depending on DEFAULT_DATABASE) does not silently
# fail.
#
# Usage:
#   source scripts/cgraph-env.sh                       # default: FalkorDB Lite
#   CGRAPH_BACKEND=kuzudb source scripts/cgraph-env.sh # explicit Kuzu override
#
# The Python preflight in src/codegraphcontext_ext/preflight.py provides the
# same fail-closed check at runtime. This wrapper catches problems earlier,
# before any Python process starts.

set -u

ZOMBIE_MOUNT="/Volumes/zombie"
ZOMBIE_HF_CACHE="${ZOMBIE_MOUNT}/cgraph/hf-cache"
ZOMBIE_DB_ROOT="${ZOMBIE_MOUNT}/cgraph/db"
# Spec 006: per-project stores live under db/<slug>/<backend>/.
# The default project is "cgraph" and the default backend is FalkorDB Lite.
CGRAPH_BACKEND="${CGRAPH_BACKEND:-falkordb}"
ZOMBIE_FALKOR_DEFAULT="${ZOMBIE_DB_ROOT}/cgraph/falkordb"
ZOMBIE_FALKOR_SOCKET_DEFAULT="${ZOMBIE_DB_ROOT}/cgraph/falkordb.sock"
ZOMBIE_KUZU_DEFAULT="${ZOMBIE_DB_ROOT}/cgraph/kuzudb"

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

export HF_HOME="${ZOMBIE_HF_CACHE}"
export SENTENCE_TRANSFORMERS_HOME="${ZOMBIE_HF_CACHE}"
export CGC_RUNTIME_DB_TYPE="${CGRAPH_BACKEND}"

case "${CGRAPH_BACKEND}" in
    falkordb)
        if [ ! -d "${ZOMBIE_FALKOR_DEFAULT}" ]; then
            echo "cgraph-env: WARNING — ${ZOMBIE_FALKOR_DEFAULT} not present." >&2
            echo "cgraph-env: the next kkg index will create a fresh store at that path." >&2
        fi

        _falkor_path="${ZOMBIE_FALKOR_DEFAULT}"
        _falkor_socket="${ZOMBIE_FALKOR_SOCKET_DEFAULT}"
        if [ -f "${HOME}/.codegraphcontext/.env" ]; then
            _read_path="$(grep -E '^FALKORDB_PATH=' "${HOME}/.codegraphcontext/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
            [ -n "${_read_path}" ] && _falkor_path="${_read_path}"
            _read_sock="$(grep -E '^FALKORDB_SOCKET_PATH=' "${HOME}/.codegraphcontext/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
            [ -n "${_read_sock}" ] && _falkor_socket="${_read_sock}"
        fi
        echo "cgraph-env: HF_HOME=${HF_HOME}"
        echo "cgraph-env: CGC_RUNTIME_DB_TYPE=${CGC_RUNTIME_DB_TYPE}"
        echo "cgraph-env: FALKORDB_PATH → ${_falkor_path}"
        echo "cgraph-env: FALKORDB_SOCKET_PATH → ${_falkor_socket}"
        ;;
    kuzudb)
        if [ ! -f "${ZOMBIE_KUZU_DEFAULT}" ]; then
            echo "cgraph-env: WARNING — ${ZOMBIE_KUZU_DEFAULT} not present." >&2
            echo "cgraph-env: the next kkg index will create a fresh store at that path." >&2
        fi

        _kuzudb_path="${ZOMBIE_KUZU_DEFAULT}"
        if [ -f "${HOME}/.codegraphcontext/.env" ]; then
            _read_path="$(grep -E '^KUZUDB_PATH=' "${HOME}/.codegraphcontext/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
            [ -n "${_read_path}" ] && _kuzudb_path="${_read_path}"
        fi
        echo "cgraph-env: HF_HOME=${HF_HOME}"
        echo "cgraph-env: CGC_RUNTIME_DB_TYPE=${CGC_RUNTIME_DB_TYPE}"
        echo "cgraph-env: KUZUDB_PATH → ${_kuzudb_path}"
        ;;
    *)
        echo "cgraph-env: WARNING — CGRAPH_BACKEND='${CGRAPH_BACKEND}' is not a local embedded backend." >&2
        echo "cgraph-env: HF_HOME=${HF_HOME}"
        echo "cgraph-env: CGC_RUNTIME_DB_TYPE=${CGC_RUNTIME_DB_TYPE}"
        ;;
esac

# List any project stores that exist (both backends) so the layout is visible.
if [ -d "${ZOMBIE_DB_ROOT}" ]; then
    _falkor_projects="$(find "${ZOMBIE_DB_ROOT}" -name falkordb -maxdepth 2 -type d 2>/dev/null | sed "s|${ZOMBIE_DB_ROOT}/||;s|/falkordb||" | sort | tr '\n' ', ' | sed 's/,$//')"
    [ -n "${_falkor_projects}" ] && echo "cgraph-env: falkordb project stores: ${_falkor_projects}"
    _kuzu_projects="$(find "${ZOMBIE_DB_ROOT}" -name kuzudb -maxdepth 2 2>/dev/null | sed "s|${ZOMBIE_DB_ROOT}/||;s|/kuzudb||" | sort | tr '\n' ', ' | sed 's/,$//')"
    [ -n "${_kuzu_projects}" ] && echo "cgraph-env: kuzudb project stores: ${_kuzu_projects}"
fi
