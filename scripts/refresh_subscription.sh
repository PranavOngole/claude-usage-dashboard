#!/bin/bash
# Stable entry point for launchd and the Desktop shortcut. Pulls the repo
# first, then execs the inner script so this run always executes the
# freshly pulled version (bash reads scripts lazily; pulling a new version
# of the currently running file mid-run executes garbage, so this wrapper
# must stay tiny and unchanging).
set -euo pipefail
cd "$(dirname "$0")/.."
# --autostash: local-only rebuilds (refresh_server.py mode=local) leave
# data files modified; stash them through the pull instead of failing.
git pull --rebase --autostash --quiet origin main
exec bash scripts/refresh_inner.sh
