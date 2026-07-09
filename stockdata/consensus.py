"""多源共识交叉验证。

可靠性来自多源交叉，而非信任单一源。给定多个数据源对同一标的的日线收盘价，
逐交易日取共识值（中位数）、标出偏离共识的离群源、并与实时价比对最新日。

拉取失败的源以字符串（如 "ERR ..."）传入，会被自动忽略，不参与共识。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DayConsensus:
    date: str
    consensus: float
    n_sources: int
    agreement: bool
    sources: dict = field(default_factory=dict)
    outliers: list = field(default_factory=list)
    realtime_match: Optional[bool] = None


def cross_validate(bars_by_source: dict, tol: float = 0.01,
                   realtime: Optional[dict] = None) -> dict:
    by_date: dict = {}
    for source, series in bars_by_source.items():
        if not isinstance(series, dict):
            continue
        for date, close in series.items():
            by_date.setdefault(date, {})[source] = float(close)

    out: dict = {}
    for date, src_vals in by_date.items():
        values = list(src_vals.values())
        consensus = statistics.median(values)
        outliers = [
            s for s, v in src_vals.items()
            if consensus == 0 or abs(v - consensus) / abs(consensus) > tol
        ]
        agreement = len(outliers) == 0
        rt_match = None
        if realtime and realtime.get("date") == date and consensus:
            rt_price = float(realtime["price"])
            rt_match = abs(rt_price - consensus) / abs(consensus) <= tol
        out[date] = DayConsensus(
            date=date,
            consensus=round(consensus, 4),
            n_sources=len(src_vals),
            agreement=agreement,
            sources=src_vals,
            outliers=outliers,
            realtime_match=rt_match,
        )
    return out
