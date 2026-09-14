"""QMT 前复权日线增量同步:独立身份 qmt/qfq/qmt-front-v1,补充而非替代主链路。

默认滚动 30 天窗口(与 baostock 日更一致);可重复执行(同身份 upsert)。
前置通道不可达/凭据缺失时快速失败,不写库。

退出码:0 = 全部干净;1 = 无可用代码或一行未入库;2 = 部分失败/invalid/覆盖空洞;
3 = 通道不可达或凭据缺失(未尝试入库)。

用法:
    .venv/bin/python scripts/sync_qmt_daily.py [--codes-file config/panel-baostock.txt] \
        [--start YYYY-MM-DD] [--db PATH] [--timeout 120]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockdata.cache import Cache  # noqa: E402
from stockdata.fetch_qmt import (  # noqa: E402
    ADJ_MODE,
    ADJ_VERSION,
    SOURCE,
    QmtChannelClient,
    QmtChannelError,
    load_qmt_token,
    sync_qmt_daily,
)


def _default_db() -> Path:
    return Path(os.environ.get(
        "STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codes-file",
                        default=str(Path(__file__).resolve().parent.parent
                                    / "config" / "panel-baostock.txt"))
    parser.add_argument("--start",
                        default=(date.today() - timedelta(days=30)).isoformat())
    parser.add_argument("--db", default=str(_default_db()))
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    codes_path = Path(args.codes_file)
    if not codes_path.is_file():
        print(f"ERROR: codes file not found: {codes_path}")
        return 1
    codes = [line.strip() for line in codes_path.read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    if not codes:
        print(f"ERROR: no codes in {codes_path}")
        return 1

    try:
        token = load_qmt_token()
        client = QmtChannelClient(token=token)
    except QmtChannelError as exc:
        print(f"ERROR: {exc}")
        return 3

    cache = Cache(Path(args.db))
    try:
        result = sync_qmt_daily(cache, client, codes, start=args.start,
                                timeout=args.timeout)
    except QmtChannelError as exc:
        print(f"ERROR: {exc}")
        return 3
    finally:
        cache.close()

    for code, msg in list(result["errors"].items())[:20]:
        print(f"ERROR {code}: {msg}")
    if len(result["errors"]) > 20:
        print(f"ERROR: ... and {len(result['errors']) - 20} more")
    for row in result["invalid"][:20]:
        print("INVALID:", row)
    for code, hole in list(result["coverage_holes"].items())[:10]:
        print(f"COVERAGE-HOLE (coverage not recorded) {code}: {hole}")

    problems = bool(result["errors"] or result["invalid"]
                    or result["coverage_holes"])
    print(f"synced {result['rows']} rows for {len(result['codes_ok'])}/{len(codes)} "
          f"codes as {SOURCE}/{ADJ_MODE}/{ADJ_VERSION} (start={args.start}); "
          f"errors={len(result['errors'])}, invalid={len(result['invalid'])}, "
          f"coverage-holes={len(result['coverage_holes'])}")
    if result["rows"] == 0:
        return 1
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
