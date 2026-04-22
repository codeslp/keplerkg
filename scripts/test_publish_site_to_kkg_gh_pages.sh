#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PUBLISH_SCRIPT="$REPO_ROOT/scripts/publish-site-to-kkg-gh-pages.sh"
TMP_ROOT="$(mktemp -d)"
TARGET="$TMP_ROOT/keplerkg"
WORKTREE="$TMP_ROOT/keplerkg-gh-pages-publish"

cleanup() {
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

mkdir -p "$TARGET"
git -C "$TARGET" init --quiet -b main
git -C "$TARGET" config user.email "test@example.com"
git -C "$TARGET" config user.name "Test User"
printf "seed\n" > "$TARGET/README.md"
git -C "$TARGET" add README.md
git -C "$TARGET" commit -m "init main" >/dev/null

git -C "$TARGET" switch --quiet -c gh-pages
printf "<!DOCTYPE html><title>seed</title>\n" > "$TARGET/index.html"
: > "$TARGET/.nojekyll"
git -C "$TARGET" add index.html .nojekyll
git -C "$TARGET" commit -m "seed gh-pages" >/dev/null
git -C "$TARGET" switch --quiet main

bash "$PUBLISH_SCRIPT" "$TARGET" "$WORKTREE" >/dev/null

test -e "$WORKTREE/.git"
git -C "$WORKTREE" status --short >/dev/null
test -f "$WORKTREE/index.html"

git -C "$TARGET" worktree remove --force "$WORKTREE" >/dev/null
printf "ok\n"
