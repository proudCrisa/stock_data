"""SQLite 缓存层。

存日线 OHLCV，以行情来源和复权口径区分版本。先查缓存、仅拉缺口、断网离线可读。
缺口语义: 相对已覆盖区间 [min,max] 的左右日历延伸段；
停牌造成的中间空洞不视为缺口（那些日期本就无数据）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_SCHEMA_VERSION = 4
_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    code               TEXT NOT NULL,
    date               TEXT NOT NULL,
    open               REAL,
    high               REAL,
    low                REAL,
    close              REAL,
    volume             REAL,
    source             TEXT NOT NULL DEFAULT 'legacy_unknown',
    adjustment_mode    TEXT NOT NULL DEFAULT 'unknown',
    adjustment_version TEXT NOT NULL DEFAULT 'legacy_unknown',
    retrieved_at       TEXT NOT NULL DEFAULT '',
    is_final           INTEGER NOT NULL DEFAULT 1,
    receipt_id         INTEGER,
    PRIMARY KEY (code, date, source, adjustment_mode, adjustment_version)
);
"""

_SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_coverage (
    code               TEXT NOT NULL,
    source             TEXT NOT NULL,
    adjustment_mode    TEXT NOT NULL,
    adjustment_version TEXT NOT NULL,
    start_date         TEXT NOT NULL,
    end_date           TEXT NOT NULL,
    retrieved_at       TEXT NOT NULL,
    PRIMARY KEY (code, source, adjustment_mode, adjustment_version)
);
"""

_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_receipts (
    receipt_id          INTEGER PRIMARY KEY,
    observed_at         TEXT NOT NULL,
    source              TEXT NOT NULL,
    request_json        TEXT NOT NULL,
    response_json       TEXT NOT NULL,
    response_sha256     TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS collection_receipts_no_update
BEFORE UPDATE ON collection_receipts
BEGIN
    SELECT RAISE(ABORT, 'collection receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS collection_receipts_no_delete
BEFORE DELETE ON collection_receipts
BEGIN
    SELECT RAISE(ABORT, 'collection receipts are append-only');
END;
"""

_MIGRATION_COLUMNS = {
    "source": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
    "adjustment_mode": "TEXT NOT NULL DEFAULT 'unknown'",
    "adjustment_version": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
    "retrieved_at": "TEXT NOT NULL DEFAULT ''",
    "is_final": "INTEGER NOT NULL DEFAULT 1",
    "receipt_id": "INTEGER",
}

_COLS = (
    "date", "open", "high", "low", "close", "volume", "source",
    "adjustment_mode", "adjustment_version", "retrieved_at", "is_final",
    "receipt_id",
)

_BAOSTOCK_VERSIONS = {
    "hfq": "baostock-adjustflag-1",
    "qfq": "baostock-adjustflag-2",
    "raw": "baostock-adjustflag-3",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _shift(day: str, days: int) -> str:
    d = date.fromisoformat(day) + timedelta(days=days)
    return d.isoformat()


class Cache:
    def __init__(self, db_path, *, writer_token: object | None = None):
        self.path = Path(db_path)
        self._collector_marked = False
        self._collector_writer_token = writer_token
        if self.path.exists():
            from .collector_continuity import is_collector_database

            self._collector_marked = is_collector_database(self.path)
        if self._collector_marked:
            self._require_collector_writer()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        try:
            if self._collector_marked:
                from .collector_continuity import verify_collector_authority_schema

                self._conn.execute("PRAGMA foreign_keys=ON")
                verify_collector_authority_schema(self._conn)
            else:
                with self._conn:
                    self._conn.execute(_SCHEMA)
                    self._migrate()
        except Exception:
            self._conn.close()
            raise

    @classmethod
    def open_authorized_collector(
        cls, db_path: str | Path, *, writer_token: object
    ) -> "Cache":
        """Open an already-prepared collector without schema creation or migration."""

        return cls(db_path, writer_token=writer_token)

    @property
    def is_collector_database(self) -> bool:
        return self._collector_marked

    def _require_collector_writer(
        self, *, step_id: str | None = None, session: str | None = None
    ) -> None:
        if not self._collector_marked:
            return
        from .collector_continuity import require_collector_writer

        require_collector_writer(
            self._collector_writer_token,
            database_path=self.path,
            step_id=step_id,
            session=session,
        )

    def _migrate(self) -> None:
        """Upgrade legacy caches without losing their existing daily rows."""
        if self._collector_marked:
            raise RuntimeError("collector cache migration is forbidden")
        current_version = int(
            self._conn.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current_version} is newer than supported "
                f"schema {_SCHEMA_VERSION}"
            )
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(daily)")
        }
        for name, declaration in _MIGRATION_COLUMNS.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE daily ADD COLUMN {name} {declaration}"
                )

        migrated_at = _utc_now()
        self._conn.execute(
            "UPDATE daily SET source='legacy_unknown' "
            "WHERE source IS NULL OR source=''"
        )
        self._conn.execute(
            "UPDATE daily SET adjustment_mode='unknown' "
            "WHERE adjustment_mode IS NULL OR adjustment_mode=''"
        )
        self._conn.execute(
            "UPDATE daily SET adjustment_version='legacy_unknown' "
            "WHERE adjustment_version IS NULL OR adjustment_version=''"
        )
        self._conn.execute(
            "UPDATE daily SET retrieved_at=? "
            "WHERE retrieved_at IS NULL OR retrieved_at=''",
            (migrated_at,),
        )
        self._conn.execute(
            "UPDATE daily SET is_final=1 WHERE is_final IS NULL"
        )
        if self._daily_primary_key() != (
            "code", "date", "source", "adjustment_mode", "adjustment_version"
        ):
            self._conn.execute("ALTER TABLE daily RENAME TO daily_legacy")
            self._conn.execute(_SCHEMA)
            self._conn.execute(
                "INSERT INTO daily "
                "(code,date,open,high,low,close,volume,source,adjustment_mode,"
                "adjustment_version,retrieved_at,is_final,receipt_id) "
                "SELECT code,date,open,high,low,close,volume,source,adjustment_mode,"
                "adjustment_version,retrieved_at,is_final,receipt_id FROM daily_legacy"
            )
            self._conn.execute("DROP TABLE daily_legacy")

        self._conn.executescript(_SYNC_SCHEMA)
        self._conn.executescript(_RECEIPT_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _daily_primary_key(self) -> tuple[str, ...]:
        primary_columns = sorted(
            (
                (int(row["pk"]), str(row["name"]))
                for row in self._conn.execute("PRAGMA table_info(daily)")
                if row["pk"]
            ),
        )
        return tuple(name for _, name in primary_columns)

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def upsert(
        self,
        code: str,
        bars: list[dict],
        source: str = "baostock",
        adjustment_mode: str = "qfq",
        adjustment_version: str | None = None,
        retrieved_at: str | None = None,
        is_final: bool = True,
        capture_receipts: list[dict] | None = None,
    ) -> int:
        """Write one price variant, replacing only matching-identity daily bars."""
        self._require_collector_writer()
        if adjustment_version is None:
            if source != "baostock" or adjustment_mode not in _BAOSTOCK_VERSIONS:
                raise ValueError("adjustment_version is required for this provenance")
            adjustment_version = _BAOSTOCK_VERSIONS[adjustment_mode]
        identity = (source, adjustment_mode, adjustment_version)
        for field, value in zip(
            ("source", "adjustment_mode", "adjustment_version"), identity
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} must be a non-empty string")

        for bar in bars:
            for field, expected in zip(
                ("source", "adjustment_mode", "adjustment_version"), identity
            ):
                if field in bar and bar[field] != expected:
                    raise ValueError(
                        f"bar {bar.get('date', '<unknown>')} {field} "
                        f"{bar[field]!r} conflicts with batch {expected!r}"
                    )

        batch_retrieved_at = retrieved_at or _utc_now()
        if not isinstance(is_final, bool):
            raise ValueError("is_final must be a bool")
        for bar in bars:
            if "is_final" in bar and not isinstance(bar["is_final"], bool):
                raise ValueError("bar is_final must be a bool")
        try:
            with self._conn:
                receipt_ids = {}
                for receipt in capture_receipts or []:
                    if receipt.get("source") != source:
                        raise ValueError("receipt source conflicts with batch source")
                    receipt_ids[id(receipt)] = self._record_capture_receipt(receipt)
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
                        adjustment_mode,
                        adjustment_version,
                        b.get("retrieved_at") or batch_retrieved_at,
                        int(bool(b.get("is_final", is_final))),
                        receipt_ids.get(id(b.get("_capture_receipt"))),
                    )
                    for b in bars
                ]
                self._conn.executemany(
                    "INSERT INTO daily "
                    "(code,date,open,high,low,close,volume,source,adjustment_mode,"
                    "adjustment_version,retrieved_at,is_final,receipt_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code,date,source,adjustment_mode,adjustment_version) "
                    "DO UPDATE SET open=excluded.open, high=excluded.high, "
                    "low=excluded.low, close=excluded.close, volume=excluded.volume, "
                    "retrieved_at=excluded.retrieved_at, is_final=excluded.is_final, "
                    "receipt_id=excluded.receipt_id",
                    rows,
                )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid capture receipt: {exc}") from exc
        return len(rows)

    def _record_capture_receipt(self, receipt: dict) -> int:
        self._require_collector_writer()
        required = {"observed_at", "source", "request", "response"}
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise ValueError("receipt must contain observed_at, source, request, response")
        observed_at = receipt["observed_at"]
        source = receipt["source"]
        if not isinstance(observed_at, str) or not observed_at:
            raise ValueError("receipt observed_at must be a non-empty string")
        if not isinstance(source, str) or not source:
            raise ValueError("receipt source must be a non-empty string")
        request_json = _canonical_json(receipt["request"])
        response_json = _canonical_json(receipt["response"])
        response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
        cursor = self._conn.execute(
            "INSERT INTO collection_receipts "
            "(observed_at,source,request_json,response_json,response_sha256,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (observed_at, source, request_json, response_json, response_sha256, _utc_now()),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _validate_identity_filters(
        source: str | None,
        adjustment_mode: str | None,
        adjustment_version: str | None,
    ) -> bool:
        provided = (source is not None, adjustment_mode is not None,
                    adjustment_version is not None)
        if any(provided) and not all(provided):
            raise ValueError(
                "source, adjustment_mode, and adjustment_version must be provided together"
            )
        return all(provided)

    def _assert_unambiguous_identity(self, code: str) -> None:
        identities = self._conn.execute(
            "SELECT DISTINCT source,adjustment_mode,adjustment_version "
            "FROM daily WHERE code=? LIMIT 2",
            (code,),
        ).fetchall()
        if len(identities) > 1:
            raise ValueError(
                "multiple price variants require source, adjustment_mode, and "
                f"adjustment_version filters for {code}"
            )

    def get_range(
        self,
        code: str,
        start: str,
        end: str,
        *,
        source: str | None = None,
        adjustment_mode: str | None = None,
        adjustment_version: str | None = None,
        finalized_only: bool = False,
    ) -> list[dict]:
        """读 [start,end] 区间日线，date 升序。"""
        has_identity = self._validate_identity_filters(
            source, adjustment_mode, adjustment_version
        )
        if not has_identity:
            self._assert_unambiguous_identity(code)
        where = ["code=?", "date>=?", "date<=?"]
        params: list[object] = [code, start, end]
        if source is not None:
            where.append("source=?")
            params.append(source)
        if adjustment_mode is not None:
            where.append("adjustment_mode=?")
            params.append(adjustment_mode)
        if adjustment_version is not None:
            where.append("adjustment_version=?")
            params.append(adjustment_version)
        if finalized_only:
            where.append("is_final=1")
        cur = self._conn.execute(
            f"SELECT {','.join(_COLS)} FROM daily WHERE {' AND '.join(where)} "
            "ORDER BY date ASC",
            params,
        )
        rows = [
            {**{k: row[k] for k in _COLS}, "is_final": bool(row["is_final"])}
            for row in cur.fetchall()
        ]
        return rows

    def covered_range(
        self,
        code: str,
        *,
        source: str | None = None,
        adjustment_mode: str | None = None,
        adjustment_version: str | None = None,
        finalized_only: bool = False,
    ) -> tuple[str, str] | None:
        """该标的已缓存的 (min_date, max_date)，无数据返回 None。"""
        has_identity = self._validate_identity_filters(
            source, adjustment_mode, adjustment_version
        )
        if not has_identity:
            self._assert_unambiguous_identity(code)
        where = ["code=?"]
        params: list[object] = [code]
        if source is not None:
            where.append("source=?")
            params.append(source)
        if adjustment_mode is not None:
            where.append("adjustment_mode=?")
            params.append(adjustment_mode)
        if adjustment_version is not None:
            where.append("adjustment_version=?")
            params.append(adjustment_version)
        if finalized_only:
            where.append("is_final=1")
        cur = self._conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM daily WHERE "
            + " AND ".join(where),
            params,
        )
        row = cur.fetchone()
        if row is None or row["lo"] is None:
            return None
        return (row["lo"], row["hi"])

    def missing_gaps(
        self,
        code: str,
        start: str,
        end: str,
        *,
        source: str | None = None,
        adjustment_mode: str | None = None,
        adjustment_version: str | None = None,
        finalized_only: bool = False,
    ) -> list[tuple[str, str]]:
        """请求区间相对已覆盖区间的左右延伸缺口。"""
        cov = self.covered_range(
            code,
            source=source,
            adjustment_mode=adjustment_mode,
            adjustment_version=adjustment_version,
            finalized_only=finalized_only,
        )
        if cov is None:
            return [(start, end)]
        lo, hi = cov
        gaps = []
        if start < lo:
            gaps.append((start, _shift(lo, -1)))
        if end > hi:
            gaps.append((_shift(hi, 1), end))
        return gaps

    def sync_coverage(
        self,
        code: str,
        source: str,
        adjustment_mode: str,
        adjustment_version: str,
    ) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT start_date,end_date FROM sync_coverage "
            "WHERE code=? AND source=? AND adjustment_mode=? "
            "AND adjustment_version=?",
            (code, source, adjustment_mode, adjustment_version),
        ).fetchone()
        if row is None:
            return None
        return (row["start_date"], row["end_date"])

    def record_sync_coverage(
        self,
        code: str,
        source: str,
        adjustment_mode: str,
        adjustment_version: str,
        start: str,
        end: str,
    ) -> None:
        """Record a successfully attempted range, including non-trading days."""
        self._require_collector_writer()
        retrieved_at = _utc_now()
        self._conn.execute(
            "INSERT INTO sync_coverage "
            "(code,source,adjustment_mode,adjustment_version,start_date,end_date,"
            "retrieved_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(code,source,adjustment_mode,adjustment_version) "
            "DO UPDATE SET start_date=MIN(start_date,excluded.start_date), "
            "end_date=MAX(end_date,excluded.end_date), "
            "retrieved_at=excluded.retrieved_at",
            (
                code, source, adjustment_mode, adjustment_version,
                start, end, retrieved_at,
            ),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()


def open_authorized_collector_cache(
    db_path: str | Path, *, writer_token: object
) -> Cache:
    """Factory for a token-authorized collector cache with no migration path."""

    return Cache.open_authorized_collector(db_path, writer_token=writer_token)
