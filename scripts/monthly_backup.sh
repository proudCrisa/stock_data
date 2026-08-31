#!/bin/bash
# stockdata 每月加密备份(launchd 承载,每月 1 日 06:13 触发)
# 密钥从 macOS Keychain 条目 stockdata-backup 经 fd 传入,不落盘、不进日志。
set -u

ROOT="/Users/cdzhangxueli/workspaces/stock_data"
LOG_DIR="$HOME/.stockdata/logs"
BACKUP_DIR="$HOME/.stockdata/backups"
LOCK="$HOME/.stockdata/monthly-backup.lock"
LOG="$LOG_DIR/monthly-backup.log"

mkdir -p "$LOG_DIR" "$BACKUP_DIR"

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') another monthly-backup instance is running, exit" >> "$LOG"
  exit 1
fi
trap 'rmdir "$LOCK"' EXIT

{
  echo "===== $(date '+%F %T') monthly-backup start ====="
  pw=$(security find-generic-password -s stockdata-backup -a stockdata -w 2>/dev/null)
  if [ -z "${pw:-}" ]; then
    echo "Keychain 条目 stockdata-backup 不存在,无法无人值守备份"
    osascript -e 'display notification "Keychain 缺少 stockdata-backup 条目" with title "stockdata" subtitle "每月备份失败"' 2>/dev/null || true
    exit 1
  fi
  cd "$BACKUP_DIR"
  STOCKDATA_BACKUP_PASSWORD_FD=3 "$ROOT/.venv/bin/python" "$ROOT/scripts/backup_encrypt.py" \
    "$HOME/.stockdata/cache.sqlite" 3<<<"$pw"
  rc=$?
  unset pw
  if [ "$rc" -eq 0 ]; then
    # 轮转:只保留最近 3 份
    ls -t stockdata-backup-*.zip 2>/dev/null | tail -n +4 | while read -r f; do
      echo "rotate: remove $f"; rm -f "$f"
    done
  else
    osascript -e 'display notification "加密备份失败,详见 ~/.stockdata/logs/monthly-backup.log" with title "stockdata" subtitle "每月备份异常"' 2>/dev/null || true
  fi
  echo "===== $(date '+%F %T') monthly-backup exit=$rc ====="
  exit "$rc"
} >> "$LOG" 2>&1
