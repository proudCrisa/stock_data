#!/bin/bash
# stockdata 每周完整性审计(launchd 承载,周日 09:07 触发)
# 内容:交易日历完整性审计 + 全库异常行扫描;任何失败非零退出并系统通知。
set -u

ROOT="/Users/cdzhangxueli/workspaces/stock_data"
LOG_DIR="$HOME/.stockdata/logs"
LOG="$LOG_DIR/weekly-audit.log"
mkdir -p "$LOG_DIR"

{
  echo "===== $(date '+%F %T') weekly-audit start ====="
  rc=0

  echo "--- update-calendar(交易日历刷新,范围至当年年底) ---"
  YEAR=$(date +%Y)
  "$ROOT/.venv/bin/stockdata-cli" update-calendar \
    --database "$HOME/.stockdata/cache.sqlite" \
    --start "$YEAR-01-01" --end "$YEAR-12-31" || rc=1

  echo "--- audit_cache_completeness ---"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/audit_cache_completeness.py" || rc=1

  echo "--- anomaly scan (全库 OHLC/volume 合理性) ---"
  "$ROOT/.venv/bin/python" - <<'PYEOF' || rc=1
import os, sqlite3, sys
db = os.path.expanduser("~/.stockdata/cache.sqlite")
con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
bad = con.execute("""
    SELECT COUNT(*) FROM daily
    WHERE open<=0 OR high<=0 OR low<=0 OR close<=0
       OR high<low OR high<open OR high<close OR low>open OR low>close
       OR volume<0
""").fetchone()[0]
print("anomaly rows:", bad)
sys.exit(1 if bad else 0)
PYEOF

  echo "===== $(date '+%F %T') weekly-audit exit=$rc ====="
  if [ "$rc" -ne 0 ]; then
    osascript -e 'display notification "周度审计发现异常,详见 ~/.stockdata/logs/weekly-audit.log" with title "stockdata" subtitle "weekly-audit 异常"' 2>/dev/null || true
  fi
  exit "$rc"
} >> "$LOG" 2>&1
