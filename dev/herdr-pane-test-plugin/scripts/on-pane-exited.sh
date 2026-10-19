#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${HERDR_PLUGIN_STATE_DIR:-/tmp}/pane-exited-events.log"

{
    echo "==================================="
    echo "pane.exited event triggered at $(date -Iseconds)"
    echo "HERDR_PLUGIN_EVENT_JSON=${HERDR_PLUGIN_EVENT_JSON:-<not set>}"
    echo ""
    echo "All HERDR_* variables:"
    env | grep HERDR_ | sort || echo "(none)"
    echo "==================================="
    echo ""
} >> "$LOG_FILE"

echo "pane.exited event logged to $LOG_FILE" >&2
