#!/bin/bash
set -u

PROJECT_DIR="/Users/ara/Documents/新闻搜索"
LOG_DIR="$PROJECT_DIR/logs"
OUTPUT_DIR="$PROJECT_DIR/outputs"
MARKET_OUTPUT_DIR="$OUTPUT_DIR/market_content"
NOTIFICATION_LOG="$LOG_DIR/notification.log"
STATUS="${1:-}"

mkdir -p "$LOG_DIR"

timestamp() {
  TZ=Asia/Tokyo date "+%Y-%m-%d %H:%M Asia/Tokyo"
}

log_line() {
  printf '[%s] %s\n' "$(timestamp)" "$1" >> "$NOTIFICATION_LOG"
}

latest_image() {
  find "$MARKET_OUTPUT_DIR" -type f \( -name 'market_pack_*.png' -o -name 'market_pack_*.jpg' -o -name 'market_pack_*.jpeg' -o -name 'market_pack_*.webp' \) -print 2>/dev/null \
    | while IFS= read -r path; do
        printf '%s\t%s\n' "$(stat -f '%m' "$path" 2>/dev/null || stat -c '%Y' "$path" 2>/dev/null)" "$path"
      done \
    | sort -nr \
    | head -n 1 \
    | cut -f2-
}

image_count() {
  find "$MARKET_OUTPUT_DIR" -type f \( -name 'market_pack_*.png' -o -name 'market_pack_*.jpg' -o -name 'market_pack_*.jpeg' -o -name 'market_pack_*.webp' \) -print 2>/dev/null | wc -l | tr -d ' '
}

send_notification() {
  local title="$1"
  local body="$2"

  if command -v osascript >/dev/null 2>&1; then
    osascript - "$title" "$body" >/dev/null 2>&1 <<'APPLESCRIPT'
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
    return $?
  fi

  echo "osascript not available" >&2
  return 127
}

if [ "$STATUS" != "success" ] && [ "$STATUS" != "failure" ]; then
  log_line "type=invalid status=$STATUS notification_success=false latest_image="
  echo "usage: bash notify_market_result.sh success|failure" >&2
  exit 2
fi

NOW="$(timestamp)"
IMAGE_PATH="$(latest_image)"
IMAGE_COUNT="$(image_count)"
NOTIFY_OK="false"

if [ "$STATUS" = "success" ]; then
  TITLE="每日市场内容包生成成功"
  BODY="时间：$NOW
已生成 ${IMAGE_COUNT:-0} 张图片
路径：outputs/market_content/"
else
  TITLE="每日市场内容包生成失败"
  BODY="时间：$NOW
请查看 logs/scheduler_run.log 和 logs/market_content_errors.log"
fi

if send_notification "$TITLE" "$BODY"; then
  NOTIFY_OK="true"
else
  echo "[$NOW] notification failed for status=$STATUS" >&2
fi

log_line "type=$STATUS notification_success=$NOTIFY_OK image_count=${IMAGE_COUNT:-0} latest_image=${IMAGE_PATH:-}"

exit 0
