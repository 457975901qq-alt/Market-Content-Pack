#!/bin/bash
set -euo pipefail

PROJECT_DIR="/Users/ara/Documents/新闻搜索"
LOG_DIR="$PROJECT_DIR/logs"
TMP_DIR="$PROJECT_DIR/tmp"
LOG_FILE="$LOG_DIR/scheduler_run.log"
LOCK_DIR="$TMP_DIR/market_content.lock"
ENV_FILE="$PROJECT_DIR/.env"
NOTIFY_SCRIPT="$PROJECT_DIR/notify_market_result.sh"
TELEGRAM_SCRIPT="$PROJECT_DIR/send_market_image_telegram.py"

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
  if [ -f "$TELEGRAM_SCRIPT" ]; then
    echo "[$(timestamp)] sending latest image to Telegram"
    set +e
    "$PYTHON_BIN" "$TELEGRAM_SCRIPT"
    TELEGRAM_STATUS=$?
    set -e
    if [ "$TELEGRAM_STATUS" -ne 0 ]; then
      echo "[$(timestamp)] Telegram send failed with status $TELEGRAM_STATUS"
      if [ -x "$NOTIFY_SCRIPT" ]; then
        bash "$NOTIFY_SCRIPT" failure || echo "[$(timestamp)] Telegram failure notification failed"
      fi
      exit "$TELEGRAM_STATUS"
    fi
    echo "[$(timestamp)] Telegram send completed successfully"
  else
    echo "[$(timestamp)] Telegram script not found: $TELEGRAM_SCRIPT"
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
