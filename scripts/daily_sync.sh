#!/bin/bash
# stockdata 每日增量同步(launchd 承载,交易日 17:35 触发;非交易日空跑无害)
# 守卫:单实例锁;end 缺省 = latest_finalized_date();errors>0 时 CLI 非零退出并系统通知。
set -u

ROOT="/Users/cdzhangxueli/workspaces/stock_data"
LOG_DIR="$HOME/.stockdata/logs"
LOCK="$HOME/.stockdata/daily-sync.lock"
LOG="$LOG_DIR/daily-sync.log"
START=$(date -v-30d +%F)

mkdir -p "$LOG_DIR"

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') another daily-sync instance is running, exit" >> "$LOG"
  exit 1
fi
trap 'rmdir "$LOCK"' EXIT

{
  echo "===== $(date '+%F %T') daily-sync start (from $START) ====="
  "$ROOT/.venv/bin/stockdata-cli" update \
    --codes-file "$ROOT/config/panel-baostock.txt" \
    --start "$START"
  rc=$?
  echo "===== $(date '+%F %T') daily-sync exit=$rc ====="
  if [ "$rc" -ne 0 ]; then
    osascript -e 'display notification "daily-sync 失败,详见 ~/.stockdata/logs/daily-sync.log" with title "stockdata" subtitle "每日同步异常"' 2>/dev/null || true
  fi
  exit "$rc"
} >> "$LOG" 2>&1
