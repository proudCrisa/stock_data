"""Wind CSV 批量入库：把 datasource(wind_get_price, price_adj=F) 拉取的日线 CSV 写入本地缓存。

以独立价格身份入库（wind/qfq/wind-fwd-v1），绝不混入 baostock/tonghuashun
身份（避免复权混源）。可重复执行：同身份同日期按 upsert 覆盖。

防御规则（经三轮 Codex 交叉审查加固）：
  - 表头必须包含 trade_date/wind_code/open/high/low/close/volume，否则整文件拒绝；
  - 代码必须能被 ticker.normalize 规范化；日期必须是合法 ISO 日期；
  - 价格必须有限且为正，成交量有限且非负，且满足 low <= open/close <= high；
  - 停牌行（成交量为空）跳过并单独计数；其余解析失败计入 invalid 并逐条列出；
  - 同代码同日期跨文件数值冲突：该日整日拒收并报告；
  - sync_coverage 只在「库内交易日历 + 已观测停牌日」能完整解释 [min,max] 时记录；
    库内交易日历为空时无法验证，一律不记录（防止覆盖过度声明）；
  - 按文件归档：无任何问题的文件移入 <csv_dir>/ingested/；
    涉及 invalid/冲突/覆盖空洞代码的文件留在原目录待人工处理。

退出码：0 = 全部干净；1 = 无 CSV 或全部文件表头不可用；
2 = 存在表头错误（有可用文件时）/invalid 行/冲突/覆盖空洞。

用法：
    .venv/bin/python scripts/ingest_wind_csv.py [csv_dir]   # 默认 /tmp/wind_fill
"""
from __future__ import annotations

import csv
import math
import os
import shutil
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockdata.cache import Cache  # noqa: E402
from stockdata.ticker import normalize  # noqa: E402

SOURCE = "wind"
ADJ_MODE = "qfq"
ADJ_VERSION = "wind-fwd-v1"

_REQUIRED_COLUMNS = {"trade_date", "wind_code", "open", "high", "low", "close", "volume"}


def _default_db() -> Path:
    return Path(os.environ.get(
        "STOCKDATA_DB", str(Path.home() / ".stockdata" / "cache.sqlite")))


def _parse_bar(row: dict) -> tuple[str, dict]:
    """解析并校验一行；返回 (规范代码, bar)。非法行抛 ValueError。"""
    code = normalize(row["wind_code"])
    day = date.fromisoformat(row["trade_date"].strip()).isoformat()
    o, h, l, c, v = (float(row[k]) for k in ("open", "high", "low", "close", "volume"))
    if not all(math.isfinite(x) for x in (o, h, l, c, v)):
        raise ValueError("non-finite value")
    if min(o, h, l, c) <= 0 or v < 0:
        raise ValueError("non-positive price or negative volume")
    if not (l <= o <= h and l <= c <= h):
        raise ValueError("OHLC relation violated")
    return code, {"date": day, "open": o, "high": h, "low": l, "close": c, "volume": v}


def main() -> int:
    csv_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/wind_fill")
    files = sorted(p for p in csv_dir.glob("*.csv") if p.is_file())
    if not files:
        print("no csv files in", csv_dir)
        return 1

    bars_by_code: dict[str, dict[str, dict]] = defaultdict(dict)
    suspended: dict[str, set[str]] = defaultdict(set)  # 观测到的停牌日（空成交量）
    invalid_rows: list[str] = []
    conflict_days: set[tuple[str, str]] = set()  # (code, day) 结构化记录
    header_bad: list[str] = []
    # 每个可用文件：出现的代码、invalid 行数（用于按文件归档判定）
    file_stats: dict[Path, dict] = {}

    for path in files:
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or not _REQUIRED_COLUMNS <= set(reader.fieldnames):
                print(f"ERROR {path.name}: bad header {reader.fieldnames}")
                header_bad.append(path.name)
                continue
            stats = file_stats[path] = {"codes": set(), "invalid": 0}
            for row in reader:
                if not (row.get("volume") or "").strip():
                    # 停牌日：Wind 返回平推价 + 空成交量，不入库（停牌空洞不视为缺口）
                    try:
                        susp_code = normalize(row["wind_code"])
                        suspended[susp_code].add(
                            date.fromisoformat(row["trade_date"].strip()).isoformat())
                        # 停牌证据也是归档判定的依据：该代码若涉覆盖空洞，
                        # 本文件须留在原地保留证据
                        stats["codes"].add(susp_code)
                    except (KeyError, ValueError):
                        invalid_rows.append(f"{path.name}: {row!r}")
                        stats["invalid"] += 1
                    continue
                try:
                    code, bar = _parse_bar(row)
                except (KeyError, ValueError):
                    invalid_rows.append(f"{path.name}: {row!r}")
                    stats["invalid"] += 1
                    continue
                stats["codes"].add(code)
                old = bars_by_code[code].get(bar["date"])
                if old is not None and old != bar:
                    conflict_days.add((code, bar["date"]))
                    conflicts_msg = f"{code} {bar['date']}: {old} vs {bar} ({path.name})"
                    print("CONFLICT:", conflicts_msg)
                    continue
                bars_by_code[code][bar["date"]] = bar

    if not file_stats:
        print("no usable csv files (all headers invalid)")
        return 1

    for r in invalid_rows[:20]:
        print("INVALID:", r)
    if len(invalid_rows) > 20:
        print(f"INVALID: ... and {len(invalid_rows) - 20} more")

    # 冲突日整日拒收
    for code, day in conflict_days:
        bars_by_code.get(code, {}).pop(day, None)

    cache = Cache(_default_db())
    # 交易日历只取其他来源的行：本身份（wind）的行不能用来
    # 自证覆盖完整（防止重跑时用上次写入的行充当日历证据）。
    calendar = {r[0] for r in cache._conn.execute(
        "SELECT DISTINCT date FROM daily WHERE source != ?", (SOURCE,))}

    total = 0
    hole_details: list[str] = []
    hole_codes: set[str] = set()
    for code in sorted(bars_by_code):
        days = bars_by_code[code]
        if not days:
            continue
        bars = [days[d] for d in sorted(days)]
        total += cache.upsert(
            code, bars, source=SOURCE, adjustment_mode=ADJ_MODE,
            adjustment_version=ADJ_VERSION,
        )
        # 覆盖声明前验证：[min,max] 内每个库内交易日都要有 bar 或已观测停牌；
        # 日历为空时无法验证，一律不记录（宁可缺 coverage 也不过度声明）
        lo, hi = bars[0]["date"], bars[-1]["date"]
        if not calendar:
            hole_codes.add(code)
            hole_details.append(f"{code}: trading calendar empty, coverage skipped")
            continue
        have = set(days) | suspended.get(code, set())
        unexplained = [d for d in calendar if lo <= d <= hi and d not in have]
        if unexplained:
            hole_codes.add(code)
            hole_details.append(f"{code}: {unexplained[:5]}")
        else:
            cache.record_sync_coverage(
                code, SOURCE, ADJ_MODE, ADJ_VERSION, lo, hi)
    cache.close()

    for hole in hole_details[:10]:
        print("COVERAGE-HOLE (coverage not recorded):", hole)
    if len(hole_details) > 10:
        print(f"COVERAGE-HOLE: ... and {len(hole_details) - 10} more")

    problems = bool(header_bad or invalid_rows or conflict_days or hole_codes)

    # 按文件归档：只有完全干净（无 invalid、不涉及冲突/空洞代码）的文件才移走
    archived = 0
    if file_stats:
        done_dir = csv_dir / "ingested"
        dirty_codes = {c for c, _ in conflict_days} | hole_codes
        for path, stats in file_stats.items():
            if stats["invalid"] == 0 and not (stats["codes"] & dirty_codes):
                done_dir.mkdir(exist_ok=True)
                shutil.move(str(path), str(done_dir / path.name))
                archived += 1

    print(f"ingested {total} rows for {len(bars_by_code)} codes "
          f"as {SOURCE}/{ADJ_MODE}/{ADJ_VERSION}; "
          f"suspension-skips={sum(len(v) for v in suspended.values())}, "
          f"invalid={len(invalid_rows)}, conflicts={len(conflict_days)}, "
          f"coverage-holes={len(hole_codes)}, bad-headers={len(header_bad)}, "
          f"archived={archived}/{len(file_stats)}")
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
