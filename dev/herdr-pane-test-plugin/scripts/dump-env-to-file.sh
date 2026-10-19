#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${HERDR_PLUGIN_STATE_DIR:-/tmp}"
PLACEMENT="${1:-unknown}"
OUTPUT_FILE="$STATE_DIR/pane-env-$PLACEMENT.txt"

{
    echo "=== HERDR Environment Variables ($PLACEMENT pane) ==="
    echo "Timestamp: $(date -Iseconds)"
    echo ""
    env | grep HERDR_ | sort || echo "(No HERDR_* variables found)"
    echo ""
    echo "=== Key Variables ==="
    echo "HERDR_PANE_ID=${HERDR_PANE_ID:-<NOT SET>}"
    echo "HERDR_TAB_ID=${HERDR_TAB_ID:-<NOT SET>}"
    echo "HERDR_WORKSPACE_ID=${HERDR_WORKSPACE_ID:-<NOT SET>}"
} > "$OUTPUT_FILE"

echo "Environment captured to: $OUTPUT_FILE" >&2
cat "$OUTPUT_FILE"
