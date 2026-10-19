#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${HERDR_PLUGIN_STATE_DIR:-/tmp}"
OUTPUT_FILE="$STATE_DIR/long-running-pane.txt"

echo "Long-running pane started with HERDR_PANE_ID=$HERDR_PANE_ID" | tee "$OUTPUT_FILE"
echo "Sleeping for 300 seconds..." | tee -a "$OUTPUT_FILE"

# Sleep for 5 minutes (or until killed)
sleep 300

echo "Sleep completed" | tee -a "$OUTPUT_FILE"
