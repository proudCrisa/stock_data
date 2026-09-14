"""QMT 前复权日线全量刷新:独立身份 qmt/qfq/qmt-front-v1,补充而非替代主链路。

每次把库内序列对齐到通道当前返回的完整历史(前复权在除权后会重述全部
历史,滚动窗口会造成新旧因子混排);通道够不到的更早日行被删除,保证
库内序列永远是单一复权因子版本。前置通道不可达/凭据缺失/快照停跳时
快速失败,不写库。

退出码:0 = 全部干净;1 = 无可用代码或一行未入库;2 = 部分失败/invalid/覆盖空洞;
3 = 通道不可达或凭据缺失(未尝试入库)。

用法:
    .venv/bin/python scripts/sync_qmt_daily.py [--codes-file config/panel-baostock.txt] \
        [--db PATH] [--fulldata] [--timeout 60]
"""
from __future__ import annotations

import argparse
import os
import sys
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
    sync_qmt_daily_from_snapshot,
)


def _default_db() -> Path:
    return Path(os.environ.get(
        "STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codes-file",
                        default=str(Path(__file__).resolve().parent.parent
                                    / "config" / "panel-baostock.txt"))
    parser.add_argument("--db", default=str(_default_db()))
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单标的 fulldata 等待上限(秒);仅 --fulldata 模式")
    parser.add_argument("--fulldata", action="store_true",
                        help="逐标的走 /fulldata 按需通道(池外标的/回填用);"
                             "缺省为快照主路径:一次 /latest 覆盖全池")
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
        if args.fulldata:
            result = sync_qmt_daily(cache, client, codes,
                                    timeout=args.timeout)
        else:
            result = sync_qmt_daily_from_snapshot(cache, client, codes)
    except QmtChannelError as exc:
        print(f"ERROR: {exc}")
        return 3
    finally:
        cache.close()

    not_in_pool = result.get("not_in_pool") or []
    if not_in_pool:
        print(f"NOT-IN-POOL: {len(not_in_pool)} codes 不在 QMT 常驻池"
              f"(仍由 baostock 主链路覆盖): {not_in_pool[:10]}"
              + (" ..." if len(not_in_pool) > 10 else ""))
    for code, msg in list(result["errors"].items())[:20]:
        print(f"ERROR {code}: {msg}")
    if len(result["errors"]) > 20:
        print(f"ERROR: ... and {len(result['errors']) - 20} more")
    for row in result["invalid"][:20]:
        print("INVALID:", row)
    for code, hole in list(result["coverage_holes"].items())[:10]:
        print(f"COVERAGE-HOLE (coverage not recorded) {code}: {hole}")

    nonpositive = sum(result.get("nonpositive", {}).values())
    problems = bool(result["errors"] or result["invalid"]
                    or result["coverage_holes"])
    print(f"synced {result['rows']} rows for {len(result['codes_ok'])}/{len(codes)} "
          f"codes as {SOURCE}/{ADJ_MODE}/{ADJ_VERSION}; "
          f"errors={len(result['errors'])}, invalid={len(result['invalid'])}, "
          f"nonpositive={nonpositive}(复权口径产物,观测但不入库), "
          f"coverage-holes={len(result['coverage_holes'])}")
    if result["rows"] == 0:
        return 1
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
