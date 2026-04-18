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
# Phase 1.5 step 7 will replace this shell-level check with a Python preflight
# helper inside src/codegraphcontext_ext/ when the cgraph config layer lands in
# Phase 3. Until then, this wrapper is the only preflight.

set -u

ZOMBIE_MOUNT="/Volumes/zombie"
ZOMBIE_HF_CACHE="${ZOMBIE_MOUNT}/cgraph/hf-cache"
ZOMBIE_KUZU_DIR="${ZOMBIE_MOUNT}/cgraph/db"

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
    echo "cgraph-env: refuse to continue; cgc would silently write to the internal drive." >&2
    return 1 2>/dev/null || exit 1
fi

if [ ! -d "${ZOMBIE_HF_CACHE}" ]; then
    echo "cgraph-env: ERROR — ${ZOMBIE_HF_CACHE} missing." >&2
    echo "cgraph-env: has the Phase 1.5 migration been run?" >&2
    return 1 2>/dev/null || exit 1
fi

if [ ! -f "${ZOMBIE_KUZU_DIR}/kuzudb" ]; then
    echo "cgraph-env: WARNING — ${ZOMBIE_KUZU_DIR}/kuzudb not present." >&2
    echo "cgraph-env: the next cgc embed will create a fresh store at that path." >&2
fi

export HF_HOME="${ZOMBIE_HF_CACHE}"
export SENTENCE_TRANSFORMERS_HOME="${ZOMBIE_HF_CACHE}"
export CGC_RUNTIME_DB_TYPE="kuzudb"

echo "cgraph-env: HF_HOME=${HF_HOME}"
echo "cgraph-env: CGC_RUNTIME_DB_TYPE=${CGC_RUNTIME_DB_TYPE}"
echo "cgraph-env: KUZUDB_PATH (via ~/.codegraphcontext/.env) → ${ZOMBIE_KUZU_DIR}/kuzudb"
