#!/bin/bash
set -euo pipefail

PROJECT_DIR="/Users/ara/Documents/新闻搜索"
LOG_DIR="$PROJECT_DIR/logs"
TMP_DIR="$PROJECT_DIR/tmp"
LOG_FILE="$LOG_DIR/scheduler_run.log"
LOCK_DIR="$TMP_DIR/market_content.lock"
ENV_FILE="$PROJECT_DIR/.env"
NOTIFY_SCRIPT="$PROJECT_DIR/notify_market_result.sh"

mkdir -p "$LOG_DIR" "$TMP_DIR"
exec >> "$LOG_FILE" 2>&1

timestamp() {
  TZ=Asia/Tokyo date "+%Y-%m-%d %H:%M:%S %Z"
}

echo "[$(timestamp)] market content pack run requested"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(timestamp)] lock exists at $LOCK_DIR; another run is still active. exiting."
  exit 0
fi

cleanup() {
  rm -rf "$LOCK_DIR"
  echo "[$(timestamp)] lock cleared"
}
trap cleanup EXIT INT TERM

cd "$PROJECT_DIR"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  echo "[$(timestamp)] loaded environment from $ENV_FILE"
else
  echo "[$(timestamp)] no .env file found; using launchd/shell environment"
fi

PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
echo "[$(timestamp)] python: $("$PYTHON_BIN" --version 2>&1)"
echo "[$(timestamp)] starting build_daily_market_pack.py"

set +e
"$PYTHON_BIN" "$PROJECT_DIR/build_daily_market_pack.py"
BUILD_STATUS=$?
set -e

if [ "$BUILD_STATUS" -eq 0 ]; then
  echo "[$(timestamp)] market content pack completed successfully"
  if [ -x "$NOTIFY_SCRIPT" ]; then
    bash "$NOTIFY_SCRIPT" success || echo "[$(timestamp)] success notification failed"
  else
    echo "[$(timestamp)] notification script not executable: $NOTIFY_SCRIPT"
  fi
  exit 0
fi

echo "[$(timestamp)] market content pack failed with status $BUILD_STATUS"
if [ -x "$NOTIFY_SCRIPT" ]; then
  bash "$NOTIFY_SCRIPT" failure || echo "[$(timestamp)] failure notification failed"
else
  echo "[$(timestamp)] notification script not executable: $NOTIFY_SCRIPT"
fi
exit "$BUILD_STATUS"
