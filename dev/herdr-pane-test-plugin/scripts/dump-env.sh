#!/usr/bin/env bash
set -euo pipefail

echo "=== HERDR Environment Variables ==="
env | grep HERDR_ | sort || echo "(No HERDR_* variables found)"
echo ""
echo "=== All Environment Variables ==="
env | sort
echo ""
echo "Press Enter to exit..."
read
