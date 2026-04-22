#!/usr/bin/env bash
# publish-site-to-kkg-gh-pages.sh — stage cgraph/site/ into keplerkg's gh-pages branch.
#
# Usage:
#   bash scripts/publish-site-to-kkg-gh-pages.sh [TARGET_REPO] [WORKTREE_DIR]
#   TARGET_REPO defaults to ../keplerkg
#   WORKTREE_DIR defaults to ../keplerkg-gh-pages-publish
#
# This script does not commit or push. It prepares a detached gh-pages worktree
# so the landing page can be reviewed and published separately from keplerkg/main.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGRAPH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${1:-${CGRAPH_ROOT}/../keplerkg}"
WORKTREE="${2:-${TARGET%/}-gh-pages-publish}"
BRANCH="gh-pages"
created_worktree=0
success=0

cleanup() {
    if [ "$created_worktree" -eq 1 ] && [ "$success" -ne 1 ]; then
        git -C "$TARGET" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if [ ! -d "$TARGET/.git" ]; then
    echo "error: target '$TARGET' is not a git repository."
    exit 1
fi

if ! git -C "$TARGET" rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
    echo "error: branch '$BRANCH' was not found in '$TARGET'."
    exit 1
fi

if [ -e "$WORKTREE" ]; then
    echo "error: worktree path '$WORKTREE' already exists."
    echo "  Remove it first or pass a different second argument."
    exit 1
fi

echo "preparing $BRANCH worktree at $WORKTREE"
git -C "$TARGET" worktree add --quiet --detach "$WORKTREE" "$BRANCH"
created_worktree=1

echo "syncing $CGRAPH_ROOT/site/ -> $WORKTREE/"
rsync -av --delete \
    --exclude='.git' \
    --exclude='.DS_Store' \
    "$CGRAPH_ROOT/site/" "$WORKTREE/"
touch "$WORKTREE/.nojekyll"

success=1
trap - EXIT

echo ""
echo "gh-pages worktree prepared at $WORKTREE"
echo "review:"
echo "  git -C $WORKTREE status --short"
echo ""
echo "publish:"
echo "  git -C $WORKTREE add -A"
echo "  git -C $WORKTREE commit -m 'deploy: publish landing page'"
echo "  git -C $WORKTREE push origin HEAD:$BRANCH"
