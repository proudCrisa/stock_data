#!/bin/bash
set -eu
unset QMT_TOKEN

MODE="${1:-}"
case "$MODE" in
  --check|pre_open|post_close) ;;
  *) echo "usage: $0 --check|pre_open|post_close" >&2; exit 2 ;;
esac

RUNTIME="/Users/cdzhangxueli/.stockdata/rqgm-forward-capture-runtime-v6/sha256-3e184d2d8171bea03f97fe9333bb1d980163d810c5bfc1fa9f4524c3dad8ba85"
MANIFEST="$RUNTIME/manifest.json"
PYTHON="/opt/homebrew/Cellar/python@3.12/3.12.13_4/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
REGISTRATION="/Users/cdzhangxueli/.stockdata/rqgm-forward-registration-v7-20260907.json"
DATABASE="/Users/cdzhangxueli/.stockdata/rqgm-forward-evidence-v7-20260907.sqlite"
REGISTRATION_SHA256="8151b97148c2b5bafab7965aa4c9bc87489fdc21a6ea348f58a77ba857b97cca"
MANIFEST_SHA256="3e184d2d8171bea03f97fe9333bb1d980163d810c5bfc1fa9f4524c3dad8ba85"
PYTHON_SHA256="e5369ecfc7f066315f0a2f27566da459f2e0838bfc0deef6232fd73e67c47fc2"
SCHEDULE_SHA256="4069e537adf938ff2a59445e6cc60fddbdc3588290d8736dff2c07c5373212f5"

if [ "$MODE" != "--check" ]; then
  EFFECTIVE_DATE="$(TZ=Asia/Shanghai /bin/date +%F)"
  case "$EFFECTIVE_DATE" in
    2026-09-07|2026-09-08|2026-09-09) ;;
    *) exit 0 ;;
  esac
fi

verify_sha256() {
  path="$1"
  expected="$2"
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  actual="$(/usr/bin/shasum -a 256 "$path" | /usr/bin/awk '{print $1}')"
  [ "$actual" = "$expected" ]
}

verify_sha256 "$PYTHON" "$PYTHON_SHA256"
verify_sha256 "$MANIFEST" "$MANIFEST_SHA256"
verify_sha256 "$REGISTRATION" "$REGISTRATION_SHA256"

PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= "$PYTHON" - \
  "$RUNTIME" "$MANIFEST" "$REGISTRATION" "$SCHEDULE_SHA256" <<'PY'
from hashlib import sha256
import json
from pathlib import Path
import sys

runtime = Path(sys.argv[1]).resolve()
manifest = json.loads(Path(sys.argv[2]).read_text())
for item in manifest["files"]:
    relative = Path(item["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("runtime manifest path is unsafe")
    candidate = (runtime / relative).resolve()
    if runtime not in candidate.parents or not candidate.is_file():
        raise SystemExit(f"runtime file is unavailable: {relative}")
    raw = candidate.read_bytes()
    if len(raw) != item["size"] or sha256(raw).hexdigest() != item["sha256"]:
        raise SystemExit(f"runtime file drifted: {relative}")

sys.path.insert(0, str(runtime))
from stockdata.collector_continuity import freeze_collector_step_schedule

specs = freeze_collector_step_schedule(registration_file=sys.argv[3])
if len(specs) != 12 or specs[0].schedule_sha256 != sys.argv[4]:
    raise SystemExit("registered collector schedule drifted")
PY

if [ "$MODE" = "--check" ]; then
  exit 0
fi

exec env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH="$RUNTIME" "$PYTHON" \
  -m stockdata.cli registered-panel-capture \
  --registration-file "$REGISTRATION" \
  --database "$DATABASE" \
  --date "$EFFECTIVE_DATE" \
  --phase "$MODE"
