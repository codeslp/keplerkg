#!/usr/bin/env bash
# Claude Code PostToolUse hook: run kkg audit --scope diff after Edit/Write.
#
# Install in .claude/settings.json:
#   "hooks": {
#     "PostToolUse": [{
#       "matcher": "Edit|Write",
#       "command": "bash scripts/hooks/post-tool-audit.sh",
#       "timeout": 5000
#     }]
#   }
#
# Exit codes:
#   0 — no hard violations (or audit skipped)
#   2 — hard violation found → blocks next tool call

set -euo pipefail

# Skip if kkg is not available
command -v kkg >/dev/null 2>&1 || exit 0

# Run audit in diff scope with claude-hook format
OUTPUT=$(kkg audit --scope diff --format json --require-hard-zero 2>/dev/null) || EXIT_CODE=$?

if [ "${EXIT_CODE:-0}" -eq 2 ]; then
    echo "kkg audit: HARD violation detected — blocking." >&2
    echo "$OUTPUT" >&2
    exit 2
fi

# Warn-level findings go to stderr as reminders
if echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('counts',{}).get('warn',0)==0 else 1)" 2>/dev/null; then
    exit 0
fi

echo "kkg audit: warn-level findings (non-blocking):" >&2
echo "$OUTPUT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for a in d.get('advisories',[]):
    if a['severity']=='warn':
        print(f\"  [{a['standard_id']}] {a['suggestion'][:100]}\", file=sys.stderr)
" 2>/dev/null

exit 0
