#!/usr/bin/env python3
"""口径一致性验证：本地 stockdata vs super-trader-rqgm 的 findesk 缓存。

对齐重叠交易日，比对 close 相对偏差。偏差应表现为"同一时段内近似恒定"
（前复权基准日不同导致的整体缩放），逐日相对形态一致 → 对回测（收益率序列）无实质影响。

用法:
    .venv/bin/python scripts/validate_against_findesk.py
"""
import glob
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stockdata.cache import Cache
from stockdata.service import HistoryService

FFD_DIR = Path.home() / "workspaces" / "super-trader-rqgm" / "data"
PERIOD = ("2024-01-01", "2024-06-30")


def load_findesk() -> dict:
    """findesk 缓存 → {code: {date: close}}。"""
    out = defaultdict(dict)
    for f in glob.glob(str(FFD_DIR / "ffd_2024H1_batch*.json")):
        d = json.load(open(f))
        for i, code in enumerate(d["code"]):
            out[code][d["time"][i]] = d["close"][i]
    return out


def main() -> int:
    findesk = load_findesk()
    if not findesk:
        print("未找到 findesk 缓存，跳过")
        return 0

    db = Path(tempfile.mkdtemp()) / "validate.sqlite"
    svc = HistoryService(Cache(db))

    print(f"{'标的':<12} {'共同日':>6} {'均值偏差':>8} {'最大偏差':>8} {'时段内标准差':>10} 判定")
    print("-" * 60)
    all_ok = True
    for code in sorted(findesk):
        out = svc.get_history(code, PERIOD[0], PERIOD[1], today="2026-07-08")
        local = {b["date"]: b["close"] for b in out}
        common = sorted(set(findesk[code]) & set(local))
        if not common:
            print(f"{code:<12} {'0':>6}  无重叠")
            continue
        diffs = [abs(findesk[code][d] - local[d]) / findesk[code][d]
                 for d in common if findesk[code][d]]
        mean_d = statistics.mean(diffs)
        max_d = max(diffs)
        std_d = statistics.pstdev(diffs)
        # 判定：时段内偏差标准差小（形态一致，仅整体缩放）
        ok = std_d < 0.02
        all_ok = all_ok and ok
        print(f"{code:<12} {len(common):>6} {mean_d*100:>7.2f}% {max_d*100:>7.2f}% "
              f"{std_d*100:>9.3f}% {'✅' if ok else '⚠️'}")

    print("-" * 60)
    print("判定标准: 时段内偏差标准差 < 2% → 逐日相对形态一致（仅前复权基准缩放）")
    print("结论:", "✅ 全部通过" if all_ok else "⚠️ 存在形态不一致，需人工核查")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
