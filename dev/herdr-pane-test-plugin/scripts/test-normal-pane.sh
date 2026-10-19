#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="${HERDR_PLUGIN_STATE_DIR:-/tmp}/normal-pane-env.txt"

echo "=== Testing Normal Pane Environment ===" | tee "$OUTPUT_FILE"
echo "" | tee -a "$OUTPUT_FILE"
env | grep HERDR_ | sort | tee -a "$OUTPUT_FILE"
echo "" | tee -a "$OUTPUT_FILE"
echo "Output saved to: $OUTPUT_FILE"
