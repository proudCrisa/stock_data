"""SQLite 缓存层。

存日线 OHLCV，(code, date) 唯一。先查缓存、仅拉缺口、断网离线可读。
缺口语义: 相对已覆盖区间 [min,max] 的左右日历延伸段；
停牌造成的中间空洞不视为缺口（那些日期本就无数据）。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    code    TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  REAL,
    source  TEXT,
    PRIMARY KEY (code, date)
);
"""

_COLS = ("date", "open", "high", "low", "close", "volume")


def _shift(day: str, days: int) -> str:
    d = date.fromisoformat(day) + timedelta(days=days)
    return d.isoformat()


class Cache:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def upsert(self, code: str, bars: list[dict], source: str | None = None) -> int:
        """写入日线，(code,date) 冲突时覆盖更新。返回写入行数。"""
        rows = [
            (
                code,
                b["date"],
                b.get("open"),
                b.get("high"),
                b.get("low"),
                b.get("close"),
                b.get("volume"),
                source,
            )
            for b in bars
        ]
        self._conn.executemany(
            "INSERT INTO daily (code,date,open,high,low,close,volume,source) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code,date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume, source=excluded.source",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def get_range(self, code: str, start: str, end: str) -> list[dict]:
        """读 [start,end] 区间日线，date 升序。"""
        cur = self._conn.execute(
            "SELECT date,open,high,low,close,volume FROM daily "
            "WHERE code=? AND date>=? AND date<=? ORDER BY date ASC",
            (code, start, end),
        )
        return [{k: row[k] for k in _COLS} for row in cur.fetchall()]

    def covered_range(self, code: str) -> tuple[str, str] | None:
        """该标的已缓存的 (min_date, max_date)，无数据返回 None。"""
        cur = self._conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM daily WHERE code=?",
            (code,),
        )
        row = cur.fetchone()
        if row is None or row["lo"] is None:
            return None
        return (row["lo"], row["hi"])

    def missing_gaps(self, code: str, start: str, end: str) -> list[tuple[str, str]]:
        """请求区间相对已覆盖区间的左右延伸缺口。"""
        cov = self.covered_range(code)
        if cov is None:
            return [(start, end)]
        lo, hi = cov
        gaps = []
        if start < lo:
            gaps.append((start, _shift(lo, -1)))
        if end > hi:
            gaps.append((_shift(hi, 1), end))
        return gaps

    def close(self):
        self._conn.close()
