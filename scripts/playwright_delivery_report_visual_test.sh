#!/usr/bin/env bash
set -euo pipefail

# Capture the four supported delivery-report viewport/theme combinations.
BASE_URL="${1:?usage: $0 http://127.0.0.1:8777 /absolute/output-dir}"
OUTPUT_DIR="${2:?usage: $0 http://127.0.0.1:8777 /absolute/output-dir}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
PWCLI="$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh"

mkdir -p "$OUTPUT_DIR"

"$PWCLI" open "${BASE_URL%/}/delivery_report_light.html"
"$PWCLI" resize 1440 1000
"$PWCLI" screenshot --filename="$OUTPUT_DIR/delivery_report_desktop_light.png"
"$PWCLI" resize 390 844
"$PWCLI" screenshot --filename="$OUTPUT_DIR/delivery_report_mobile_light.png"

"$PWCLI" open "${BASE_URL%/}/delivery_report_dark.html"
"$PWCLI" resize 1440 1000
"$PWCLI" screenshot --filename="$OUTPUT_DIR/delivery_report_desktop_dark.png"
"$PWCLI" resize 390 844
"$PWCLI" screenshot --filename="$OUTPUT_DIR/delivery_report_mobile_dark.png"

printf 'visual_screenshots=%s\n' "$OUTPUT_DIR"
