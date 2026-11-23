#!/usr/bin/env bash
# Muse hook: wake the worker agent only when bridge jobs are pending.
# Poll every 5 seconds. The agent then runs worker-prompt.md.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

QUEUE_DIR="$HOME/muse-bridge/queue/pending"
mkdir -p "$QUEUE_DIR"

count=$(find "$QUEUE_DIR" -maxdepth 1 -name '*.json' -type f 2>/dev/null | wc -l | tr -d ' ')

if [ "$count" -gt 0 ]; then
  log "pending_jobs" "{\"count\":$count}"
  wake "muse-bridge has pending jobs" "{\"count\":$count}"
else
  silent "no pending muse-bridge jobs" "{\"count\":0}"
fi
