"""HistoryService —— 多源 + 缓存 + 增量编排。

流程：归一化 → 算缺口 → 缺口拉主源(失败兜底 mootdx) → 写缓存 →
读缓存区间 → 若 end≥today 用腾讯今日 bar 覆盖/追加 → 返回。

依赖注入 fetcher，便于无网络测试；生产用默认的 baostock/mootdx/腾讯。
"""
from __future__ import annotations

from .cache import Cache
from .ticker import normalize


class HistoryService:
    def __init__(self, cache: Cache, primary_fetch=None,
                 fallback_fetch=None, today_fetch=None):
        self.cache = cache
        self._primary = primary_fetch if primary_fetch is not None else _default_primary
        self._fallback = fallback_fetch if fallback_fetch is not None else _default_fallback
        self._today = today_fetch if today_fetch is not None else _default_today

    def get_history(self, code: str, start: str, end: str, today: str) -> list[dict]:
        code = normalize(code)

        # 1) 缺口增量拉取
        for gstart, gend in self.cache.missing_gaps(code, start, end):
            bars = self._fetch_gap(code, gstart, gend)
            if bars:
                self.cache.upsert(code, bars)

        # 2) 读缓存区间
        rows = self.cache.get_range(code, start, end)

        # 3) 今日 bar（仅当请求区间覆盖到今天）
        if end >= today and self._today is not None:
            bar = self._today(code)
            if bar and bar.get("date"):
                rows = _merge_today(rows, bar, start, end)

        return rows

    def _fetch_gap(self, code, gstart, gend) -> list[dict]:
        """主源拉缺口；失败则兜底 mootdx。两者皆失败返回 []（离线降级）。"""
        try:
            return self._primary(code, gstart, gend)
        except Exception:
            pass
        if self._fallback is not None:
            try:
                return self._fallback(code)
            except Exception:
                pass
        return []


def _merge_today(rows, bar, start, end):
    """把今日 bar 并入结果：同日覆盖、否则在区间内追加。"""
    if not (start <= bar["date"] <= end):
        return rows
    rows = [r for r in rows if r["date"] != bar["date"]]
    rows.append({k: bar[k] for k in ("date", "open", "high", "low", "close", "volume")})
    rows.sort(key=lambda r: r["date"])
    return rows


# ── 生产默认 fetcher（延迟 import，避免测试引网络依赖）──

def _default_primary(code, start, end):
    from .fetch_baostock import fetch_baostock
    return fetch_baostock(code, start, end)


def _default_fallback(code, offset=100):
    from .fetch_mootdx import fetch_mootdx
    return fetch_mootdx(code, offset=offset)


def _default_today(code):
    from .fetch_tencent import fetch_today_bar
    return fetch_today_bar(code)
