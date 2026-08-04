"""HistoryService —— 多源 + 缓存 + 增量编排。

流程：归一化 → 算缺口 → 缺口拉同口径主源 → 写缓存 →
读缓存区间 → 若 end≥today 用腾讯今日 bar 覆盖/追加 → 返回。

依赖注入 fetcher，便于无网络测试；生产历史只用 baostock，避免复权混源。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .cache import Cache
from .finalization import latest_finalized_date
from .ticker import normalize


class HistoryService:
    def __init__(self, cache: Cache, primary_fetch=None,
                 fallback_fetch=None, today_fetch=None, *,
                 source: str = "baostock",
                 adjustment_mode: str = "qfq",
                 adjustment_version: str = "baostock-adjustflag-2",
                 fallback_source: str | None = None,
                 fallback_adjustment_mode: str | None = None,
                 fallback_adjustment_version: str | None = None):
        self.cache = cache
        self._primary = primary_fetch if primary_fetch is not None else _default_primary
        self._fallback = fallback_fetch
        self._today = today_fetch if today_fetch is not None else _default_today
        self.source = source
        self.adjustment_mode = adjustment_mode
        self.adjustment_version = adjustment_version
        self.fallback_source = fallback_source
        self.fallback_adjustment_mode = fallback_adjustment_mode
        self.fallback_adjustment_version = fallback_adjustment_version

    def get_history(
        self,
        code: str,
        start: str,
        end: str,
        today: str,
        *,
        finalized_only: bool = False,
    ) -> list[dict]:
        code = normalize(code)
        fetch_end = end
        finalized_through = latest_finalized_date()
        if finalized_only:
            fetch_end = min(end, finalized_through)

        # 1) 缺口增量拉取
        gaps = []
        if start <= fetch_end:
            gaps = self.cache.missing_gaps(
                code,
                start,
                fetch_end,
                source=self.source,
                adjustment_mode=self.adjustment_mode,
                adjustment_version=self.adjustment_version,
                finalized_only=finalized_only,
            )
        for gstart, gend in gaps:
            fetched = self._fetch_gap(code, gstart, gend)
            capture_receipt = getattr(fetched, "capture_receipt", None)
            bars = fetched
            if bars:
                bars = [
                    bar for bar in bars
                    if gstart <= bar.get("date", "") <= gend
                ]
                for bar in bars:
                    bar.setdefault("is_final", bar["date"] <= finalized_through)
                if bars:
                    self.cache.upsert(
                        code,
                        bars,
                        source=self.source,
                        adjustment_mode=self.adjustment_mode,
                        adjustment_version=self.adjustment_version,
                        capture_receipts=[capture_receipt] if capture_receipt else None,
                    )
            elif capture_receipt:
                self.cache.upsert(
                    code,
                    [],
                    source=self.source,
                    adjustment_mode=self.adjustment_mode,
                    adjustment_version=self.adjustment_version,
                    capture_receipts=[capture_receipt],
                )

        # 2) 读缓存区间
        rows = self.cache.get_range(
            code,
            start,
            fetch_end if finalized_only else end,
            source=self.source,
            adjustment_mode=self.adjustment_mode,
            adjustment_version=self.adjustment_version,
            finalized_only=finalized_only,
        )

        # 3) 今日 bar（仅当请求区间覆盖到今天）
        if not finalized_only and end >= today and self._today is not None:
            bar = self._today(code)
            if bar and bar.get("date"):
                bar.setdefault("source", "tencent")
                bar.setdefault("adjustment_mode", "intraday")
                bar.setdefault("adjustment_version", "tencent-realtime-v1")
                bar.setdefault(
                    "retrieved_at",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
                bar.setdefault("is_final", False)
                rows = _merge_today(rows, bar, start, end)

        return rows

    def _fetch_gap(self, code, gstart, gend) -> list[dict]:
        """Fetch one gap without crossing adjustment modes or versions."""
        try:
            return self._primary(code, gstart, gend)
        except Exception:
            pass
        fallback_matches = (
            self._fallback is not None
            and self.fallback_source == self.source
            and self.fallback_adjustment_mode == self.adjustment_mode
            and self.fallback_adjustment_version == self.adjustment_version
        )
        if fallback_matches:
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
    rows.append(dict(bar))
    rows.sort(key=lambda r: r["date"])
    return rows


# ── 生产默认 fetcher（延迟 import，避免测试引网络依赖）──

def _default_primary(code, start, end):
    from .fetch_baostock import fetch_baostock
    return fetch_baostock(code, start, end)


def _default_today(code):
    from .fetch_tencent import fetch_today_bar
    return fetch_today_bar(code)
