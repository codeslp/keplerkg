#!/usr/bin/env bash
# sync-to-kkg.sh — copy KeplerKG-publishable files from cgraph to a sibling keplerkg repo.
#
# Usage:
#   bash scripts/sync-to-kkg.sh [TARGET_DIR]
#   TARGET_DIR defaults to ../keplerkg
#
# What gets synced:
#   src/codegraphcontext_ext/   -> src/codegraphcontext_ext/
#   src/codegraphcontext/       -> src/codegraphcontext/  (upstream, needed at runtime)
#   tests/cgraph_ext/           -> tests/cgraph_ext/
#   schemas/                    -> schemas/
#   site/                       -> site/
#   scripts/cgraph-env.sh       -> scripts/cgraph-env.sh
#   README.md                   -> README.md
#   LICENSE                     -> LICENSE
#   pyproject.toml              -> pyproject.toml
#
# What gets excluded:
#   .btrain/ .claude/ .codex/ .agents/ specs/ .cursor/
#   docs/ (upstream CGC docs, not KeplerKG)
#   HANDOFF_*.md, FEEDBACK_LOG.md
#   *.pyc, __pycache__, .venv/
#
# Important:
#   This sync updates the sibling keplerkg repo working tree (usually main).
#   It does NOT publish the landing page to keplerkg/gh-pages, and it
#   intentionally excludes research/ notes. Use
#   scripts/publish-site-to-kkg-gh-pages.sh for the live website payload.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CGRAPH_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${1:-${CGRAPH_ROOT}/../keplerkg}"

if [ ! -d "$TARGET" ]; then
    echo "error: target directory '$TARGET' does not exist."
    echo "  Create it first:  mkdir -p $TARGET && cd $TARGET && git init"
    exit 1
fi

echo "syncing cgraph -> $TARGET"

rsync -av --delete \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.venv/' \
    --exclude='.git/' \
    --exclude='.btrain/' \
    --exclude='.claude/' \
    --exclude='.codex/' \
    --exclude='.agents/' \
    --exclude='.cursor/' \
    --exclude='specs/' \
    --exclude='docs/' \
    --exclude='HANDOFF_*.md' \
    --exclude='FEEDBACK_LOG.md' \
    --exclude='.pytest_cache/' \
    --exclude='*.egg-info/' \
    --exclude='dist/' \
    --exclude='build/' \
    --exclude='.DS_Store' \
    --exclude='CLAUDE.md' \
    --exclude='AGENTS.md' \
    --exclude='TESTING.md' \
    --exclude='cgc_entry.py' \
    --exclude='README.ru-RU.md' \
    --exclude='README.zh-CN.md' \
    --exclude='README.kor.md' \
    --exclude='README.uk.md' \
    --exclude='scripts/sync-to-kkg.sh' \
    --exclude='scripts/create-bundle.sh' \
    --exclude='scripts/post_install_fix.sh' \
    --exclude='k8s/' \
    --exclude='research/' \
    --exclude='organizer/' \
    --exclude='agentchattr/' \
    --exclude='website/' \
    --exclude='images/' \
    --exclude='.github/' \
    --exclude='Dockerfile' \
    --exclude='docker-compose*' \
    --exclude='.dockerignore' \
    --exclude='.env.example' \
    --exclude='cgc.spec' \
    --exclude='CONTRIBUTING.md' \
    --exclude='.cgcignore' \
    "$CGRAPH_ROOT/" "$TARGET/"

echo ""
echo "done. synced to $TARGET"
echo "  cd $TARGET && git status"
