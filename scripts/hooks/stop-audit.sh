#!/usr/bin/env bash
# Claude Code Stop hook: run kkg audit --scope session before turn closes.
#
# Install in .claude/settings.json:
#   "hooks": {
#     "Stop": [{
#       "command": "bash scripts/hooks/stop-audit.sh",
#       "timeout": 10000
#     }]
#   }
#
# Exit codes:
#   0 — clean or audit unavailable
#   2 — hard violation → blocks turn close

set -euo pipefail

command -v kkg >/dev/null 2>&1 || exit 0

kkg audit --scope session --format json --require-hard-zero 2>/dev/null || EXIT_CODE=$?

if [ "${EXIT_CODE:-0}" -eq 2 ]; then
    echo "kkg audit: HARD violations found at turn close — review required." >&2
    exit 2
fi

exit 0
