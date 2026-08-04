"""Append-only pre-open and post-close market-context observations."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, time
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import requests

from .cache import Cache
from .ticker import normalize


SCHEMA_VERSION = "stockdata-forward-context/2"
SOURCE = "sina-market-center-hs-a-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PREOPEN_START = time(8, 30)
_DECISION_CUTOFF = time(9, 25)
_POST_CLOSE_START = time(15, 0)
_COUNT_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
_PAGE_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_PAGE_SIZE = 80

_TRIGGER_SQL = {
    "forward_context_observations_no_update": """
        CREATE TRIGGER forward_context_observations_no_update
        BEFORE UPDATE ON forward_context_observations BEGIN
            SELECT RAISE(ABORT, 'forward context observations are append-only');
        END
    """,
    "forward_context_observations_no_delete": """
        CREATE TRIGGER forward_context_observations_no_delete
        BEFORE DELETE ON forward_context_observations BEGIN
            SELECT RAISE(ABORT, 'forward context observations are append-only');
        END
    """,
    "forward_universe_observations_no_update": """
        CREATE TRIGGER forward_universe_observations_no_update
        BEFORE UPDATE ON forward_universe_observations BEGIN
            SELECT RAISE(ABORT, 'forward universe observations are append-only');
        END
    """,
    "forward_universe_observations_no_delete": """
        CREATE TRIGGER forward_universe_observations_no_delete
        BEFORE DELETE ON forward_universe_observations BEGIN
            SELECT RAISE(ABORT, 'forward universe observations are append-only');
        END
    """,
    "forward_status_observations_no_update": """
        CREATE TRIGGER forward_status_observations_no_update
        BEFORE UPDATE ON forward_status_observations BEGIN
            SELECT RAISE(ABORT, 'forward status observations are append-only');
        END
    """,
    "forward_status_observations_no_delete": """
        CREATE TRIGGER forward_status_observations_no_delete
        BEFORE DELETE ON forward_status_observations BEGIN
            SELECT RAISE(ABORT, 'forward status observations are append-only');
        END
    """,
    "collection_receipts_no_update": """
        CREATE TRIGGER collection_receipts_no_update
        BEFORE UPDATE ON collection_receipts BEGIN
            SELECT RAISE(ABORT, 'collection receipts are append-only');
        END
    """,
    "collection_receipts_no_delete": """
        CREATE TRIGGER collection_receipts_no_delete
        BEFORE DELETE ON collection_receipts BEGIN
            SELECT RAISE(ABORT, 'collection receipts are append-only');
        END
    """,
}


class CapturedMarketRows(list[dict[str, object]]):
    """Parsed full-market rows bound to the exact provider response."""

    def __init__(self, rows: Iterable[dict[str, object]], receipt: dict[str, object]):
        super().__init__(rows)
        self.capture_receipt = receipt


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";").lower()


def _parse_raw_pages(raw_pages: object) -> list[dict[str, object]]:
    if not isinstance(raw_pages, list) or not raw_pages:
        raise ValueError("Sina receipt has no raw market pages")
    rows: list[dict[str, object]] = []
    for raw in raw_pages:
        if not isinstance(raw, str):
            raise ValueError("Sina raw market page must be text")
        page = json.loads(raw)
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ValueError("Sina raw market page is not a row list")
        rows.extend(dict(row) for row in page)
    return rows


def _market_symbol(value: object) -> str:
    raw = str(value).strip().lower()
    if len(raw) != 8 or raw[:2] not in {"sh", "sz", "bj"} or not raw[2:].isdigit():
        raise ValueError(f"invalid Sina A-share symbol: {value!r}")
    return f"{raw[2:]}.{raw[:2].upper()}"


def _board(symbol: str) -> str:
    digits, market = symbol.split(".")
    if market == "BJ":
        return "BSE"
    if digits.startswith(("300", "301")):
        return "CHINEXT"
    if digits.startswith(("688", "689")):
        return "STAR"
    return "MAIN"


def _number(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric market field: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric market field: {value!r}")
    return result


def fetch_sina_market_rows(*, timeout: float = 15.0) -> CapturedMarketRows:
    """Fetch one complete Sina ``hs_a`` snapshot and retain every raw page."""
    count_response = requests.get(_COUNT_URL, params={"node": "hs_a"}, timeout=timeout)
    count_response.raise_for_status()
    advertised_count = int(count_response.text.strip().strip('"'))
    if advertised_count <= 0:
        raise ValueError("Sina A-share universe count is empty")
    page_count = math.ceil(advertised_count / _PAGE_SIZE)
    raw_pages: list[str] = []
    rows: list[dict[str, object]] = []
    for page in range(1, page_count + 1):
        params = {
            "page": str(page),
            "num": str(_PAGE_SIZE),
            "sort": "symbol",
            "asc": "1",
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "page",
        }
        response = requests.get(_PAGE_URL, params=params, timeout=timeout)
        response.raise_for_status()
        raw_pages.append(response.text)
        page_rows = json.loads(response.text)
        if not isinstance(page_rows, list):
            raise ValueError("Sina market page is not a list")
        rows.extend(page_rows)
    if len(rows) != advertised_count:
        raise ValueError(
            f"Sina market count drift: expected {advertised_count}, received {len(rows)}"
        )
    observed_at = datetime.now(_SHANGHAI).isoformat(timespec="seconds")
    receipt = {
        "observed_at": observed_at,
        "source": SOURCE,
        "request": {
            "count_url": _COUNT_URL,
            "page_url": _PAGE_URL,
            "node": "hs_a",
            "page_size": _PAGE_SIZE,
        },
        "response": {
            "advertised_count": advertised_count,
            "count_raw": count_response.text,
            "raw_pages": raw_pages,
        },
    }
    return CapturedMarketRows(rows, receipt)


def _ensure_schema(cache: Cache) -> None:
    cache._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS forward_context_observations (
            effective_date TEXT NOT NULL,
            observation_phase TEXT NOT NULL CHECK (observation_phase IN ('pre_open','post_close')),
            decision_available_at TEXT,
            outcome_observed_at TEXT,
            finalized_at TEXT,
            source TEXT NOT NULL,
            receipt_id INTEGER NOT NULL,
            CHECK (
                (observation_phase='pre_open' AND decision_available_at IS NOT NULL
                 AND outcome_observed_at IS NULL AND finalized_at IS NULL)
                OR
                (observation_phase='post_close' AND decision_available_at IS NULL
                 AND outcome_observed_at IS NOT NULL AND finalized_at IS NOT NULL)
            ),
            PRIMARY KEY (effective_date,observation_phase,source),
            FOREIGN KEY (receipt_id) REFERENCES collection_receipts(receipt_id)
        );
        CREATE TABLE IF NOT EXISTS forward_universe_observations (
            effective_date TEXT NOT NULL,
            observation_phase TEXT NOT NULL,
            symbol TEXT NOT NULL,
            is_member INTEGER NOT NULL CHECK (is_member IN (0,1)),
            source TEXT NOT NULL,
            receipt_id INTEGER NOT NULL,
            PRIMARY KEY (effective_date,observation_phase,symbol,source),
            FOREIGN KEY (receipt_id) REFERENCES collection_receipts(receipt_id)
        );
        CREATE TABLE IF NOT EXISTS forward_status_observations (
            effective_date TEXT NOT NULL,
            observation_phase TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            listing_status TEXT NOT NULL,
            board TEXT NOT NULL,
            is_st INTEGER NOT NULL CHECK (is_st IN (0,1)),
            is_suspended INTEGER NOT NULL CHECK (is_suspended IN (0,1)),
            source TEXT NOT NULL,
            receipt_id INTEGER NOT NULL,
            PRIMARY KEY (effective_date,observation_phase,symbol,source),
            FOREIGN KEY (receipt_id) REFERENCES collection_receipts(receipt_id)
        );
        """
    )
    for sql in _TRIGGER_SQL.values():
        cache._conn.execute(sql.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1))


def _cohort(cache: Cache) -> tuple[tuple[str, ...], str]:
    row = cache._conn.execute(
        "SELECT spec_json,spec_sha256 FROM forward_capture_cohort WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise ValueError("forward capture cohort is not bound")
    spec_json = str(row[0])
    if hashlib.sha256(spec_json.encode("ascii")).hexdigest() != str(row[1]):
        raise ValueError("forward capture cohort identity mismatch")
    spec = json.loads(spec_json)
    return tuple(normalize(item) for item in spec["symbols"]), str(row[1])


def _status_values(symbol: str, row: dict[str, object]) -> tuple[str, str, str, int, int]:
    name = str(row.get("name", "")).strip()
    if not name:
        raise ValueError(f"missing name: {symbol}")
    trade = _number(row.get("trade", 0))
    volume = _number(row.get("volume", 0))
    return (
        name,
        "listed",
        _board(symbol),
        int("ST" in name.upper()),
        int(trade <= 0 or volume <= 0),
    )


def _phase(local: datetime) -> str:
    if _PREOPEN_START <= local.time() < _DECISION_CUTOFF:
        return "pre_open"
    if local.time() >= _POST_CLOSE_START:
        return "post_close"
    raise ValueError("forward context capture is outside an allowed evidence window")


def capture_forward_context(
    cache: Cache,
    effective_date: str,
    *,
    fetcher: Callable[[], CapturedMarketRows] = fetch_sina_market_rows,
    now: datetime | None = None,
) -> dict[str, object]:
    """Capture one immutable phase without preventing the other phase."""
    day = date.fromisoformat(effective_date)
    current = now or datetime.now(_SHANGHAI)
    if current.tzinfo is None:
        raise ValueError("capture time must be timezone-aware")
    local = current.astimezone(_SHANGHAI)
    if day != local.date():
        raise ValueError("forward context capture cannot backfill another date")
    if day.weekday() >= 5:
        raise ValueError("forward context capture requires a weekday session candidate")
    phase = _phase(local)
    symbols, cohort_sha256 = _cohort(cache)
    with cache._conn:
        _ensure_schema(cache)
        existing = cache._conn.execute(
            "SELECT decision_available_at,outcome_observed_at,finalized_at "
            "FROM forward_context_observations "
            "WHERE effective_date=? AND observation_phase=? AND source=?",
            (effective_date, phase, SOURCE),
        ).fetchone()
        if existing is not None:
            return {
                "schema_version": SCHEMA_VERSION,
                "captured": False,
                "effective_date": effective_date,
                "cohort_sha256": cohort_sha256,
                "cohort_size": len(symbols),
                "source": SOURCE,
                "capture_phase": phase,
                "decision_available_at": existing[0],
                "outcome_observed_at": existing[1],
                "finalized_at": existing[2],
            }

    captured = fetcher()
    receipt = captured.capture_receipt
    if receipt.get("source") != SOURCE:
        raise ValueError("forward context source identity mismatch")
    observed_at = str(receipt.get("observed_at", ""))
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("forward context receipt must be timezone-aware")
    observed_local = observed.astimezone(_SHANGHAI)
    if observed_local.date() != day or _phase(observed_local) != phase:
        raise ValueError("forward context receipt phase/date mismatch")
    verification_now = now or datetime.now(_SHANGHAI)
    if observed > verification_now.astimezone(observed.tzinfo):
        raise ValueError("forward context receipt is future-dated")
    response = receipt.get("response")
    if not isinstance(response, dict):
        raise ValueError("forward context receipt response is invalid")
    raw_rows = _parse_raw_pages(response.get("raw_pages"))
    if response.get("rows") is not None and response["rows"] != raw_rows:
        raise ValueError("parsed market rows do not match raw pages")
    if list(captured) != raw_rows:
        raise ValueError("captured market rows do not match raw pages")
    if response.get("advertised_count", len(raw_rows)) != len(raw_rows):
        raise ValueError("market receipt count does not match raw pages")
    by_symbol: dict[str, dict[str, object]] = {}
    for source_row in raw_rows:
        symbol = _market_symbol(source_row.get("symbol"))
        if symbol in by_symbol:
            raise ValueError(f"duplicate full-market symbol: {symbol}")
        by_symbol[symbol] = dict(source_row)
    missing = sorted(set(symbols) - set(by_symbol))
    if missing:
        raise ValueError(f"full-market snapshot misses cohort symbols: {missing[:3]}")

    decision_at = observed_at if phase == "pre_open" else None
    outcome_at = observed_at if phase == "post_close" else None
    finalized_at = observed_at if phase == "post_close" else None
    with cache._conn:
        receipt_id = cache._record_capture_receipt(receipt)
        cache._conn.execute(
            "INSERT INTO forward_context_observations VALUES (?,?,?,?,?,?,?)",
            (effective_date, phase, decision_at, outcome_at, finalized_at, SOURCE, receipt_id),
        )
        cache._conn.executemany(
            "INSERT INTO forward_universe_observations VALUES (?,?,?,?,?,?)",
            [
                (effective_date, phase, symbol, 1, SOURCE, receipt_id)
                for symbol in sorted(by_symbol)
            ],
        )
        status_rows = []
        for symbol in symbols:
            status_rows.append(
                (effective_date, phase, symbol, *_status_values(symbol, by_symbol[symbol]), SOURCE, receipt_id)
            )
        cache._conn.executemany(
            "INSERT INTO forward_status_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            status_rows,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "captured": True,
        "effective_date": effective_date,
        "cohort_sha256": cohort_sha256,
        "cohort_size": len(symbols),
        "universe_size": len(by_symbol),
        "source": SOURCE,
        "observed_at": observed_at,
        "capture_phase": phase,
        "decision_available_at": decision_at,
        "outcome_observed_at": outcome_at,
        "finalized_at": finalized_at,
    }


def check_forward_context_readiness(
    database: str,
    panel: Iterable[tuple[str, str]],
) -> dict[str, object]:
    """Require verified pre-open decisions and post-close final observations."""
    expected = {(normalize(symbol), date.fromisoformat(day).isoformat()) for symbol, day in panel}
    blockers: list[dict[str, object]] = []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "forward_context_observations",
            "forward_universe_observations",
            "forward_status_observations",
            "collection_receipts",
        }
        missing_tables = sorted(required - tables)
        if missing_tables:
            return {
                "ready": False,
                "integrity_ready": False,
                "blockers": [{"code": "missing_context_phase_tables", "examples": missing_tables}],
            }
        triggers = {str(row[0]): str(row[1]) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        invalid_triggers = sorted(
            name for name, sql in _TRIGGER_SQL.items()
            if name not in triggers or _normalized_sql(triggers[name]) != _normalized_sql(sql)
        )
        if invalid_triggers:
            blockers.append({"code": "invalid_context_append_only_triggers", "count": len(invalid_triggers), "examples": invalid_triggers})
        rows = connection.execute(
            """
            SELECT s.*,u.is_member,o.decision_available_at,o.outcome_observed_at,
                   o.finalized_at,r.observed_at,r.source AS receipt_source,
                   r.response_json,r.response_sha256
            FROM forward_status_observations AS s
            JOIN forward_universe_observations AS u
              ON u.effective_date=s.effective_date
             AND u.observation_phase=s.observation_phase
             AND u.symbol=s.symbol AND u.source=s.source AND u.receipt_id=s.receipt_id
            JOIN forward_context_observations AS o
              ON o.effective_date=s.effective_date
             AND o.observation_phase=s.observation_phase
             AND o.source=s.source AND o.receipt_id=s.receipt_id
            JOIN collection_receipts AS r ON r.receipt_id=s.receipt_id
            WHERE s.source=?
            """,
            (SOURCE,),
        ).fetchall()
        selected = {
            (str(row["symbol"]), str(row["effective_date"]), str(row["observation_phase"])): row
            for row in rows
            if (str(row["symbol"]), str(row["effective_date"])) in expected
        }
        for phase, code in (
            ("pre_open", "missing_decision_context_rows"),
            ("post_close", "missing_finalized_context_rows"),
        ):
            missing = sorted(expected - {(symbol, day) for symbol, day, item_phase in selected if item_phase == phase})
            if missing:
                blockers.append({"code": code, "count": len(missing), "examples": missing[:5]})
        failures: list[str] = []
        checked_receipts: set[tuple[str, str, int]] = set()
        for key, row in selected.items():
            response_json = str(row["response_json"])
            if row["receipt_source"] != SOURCE or hashlib.sha256(response_json.encode("utf-8")).hexdigest() != row["response_sha256"]:
                failures.append("@".join(key))
                continue
            try:
                response = json.loads(response_json)
                raw_rows = _parse_raw_pages(response.get("raw_pages"))
                observed = datetime.fromisoformat(
                    str(row["observed_at"]).replace("Z", "+00:00")
                )
                if observed.tzinfo is None:
                    raise ValueError("naive context observation")
                observed_local = observed.astimezone(_SHANGHAI)
                if (
                    observed_local.date().isoformat() != key[1]
                    or _phase(observed_local) != key[2]
                ):
                    raise ValueError("context phase timestamp mismatch")
            except (TypeError, ValueError, json.JSONDecodeError):
                failures.append("@".join(key))
                continue
            response_rows = {_market_symbol(item.get("symbol")): dict(item) for item in raw_rows}
            actual_status = (
                row["name"], row["listing_status"], row["board"],
                int(row["is_st"]), int(row["is_suspended"]),
            )
            expected_times = (
                (str(row["observed_at"]), None, None)
                if key[2] == "pre_open"
                else (None, str(row["observed_at"]), str(row["observed_at"]))
            )
            actual_times = (
                row["decision_available_at"], row["outcome_observed_at"], row["finalized_at"]
            )
            if (
                key[0] not in response_rows
                or response.get("advertised_count", len(raw_rows)) != len(raw_rows)
                or response.get("rows") is not None and response["rows"] != raw_rows
                or _status_values(key[0], response_rows[key[0]]) != actual_status
                or int(row["is_member"]) != 1
                or actual_times != expected_times
            ):
                failures.append("@".join(key))
            receipt_key = (key[1], key[2], int(row["receipt_id"]))
            if receipt_key not in checked_receipts:
                observed_universe = {
                    str(item[0])
                    for item in connection.execute(
                        "SELECT symbol FROM forward_universe_observations "
                        "WHERE effective_date=? AND observation_phase=? AND receipt_id=?",
                        receipt_key,
                    )
                }
                if observed_universe != set(response_rows):
                    failures.append(f"universe@{key[1]}@{key[2]}")
                checked_receipts.add(receipt_key)
        for symbol, day in expected:
            decision_row = selected.get((symbol, day, "pre_open"))
            final_row = selected.get((symbol, day, "post_close"))
            if decision_row is None or final_row is None:
                continue
            try:
                decision = datetime.fromisoformat(
                    str(decision_row["decision_available_at"]).replace("Z", "+00:00")
                )
                outcome = datetime.fromisoformat(
                    str(final_row["outcome_observed_at"]).replace("Z", "+00:00")
                )
                finalized = datetime.fromisoformat(
                    str(final_row["finalized_at"]).replace("Z", "+00:00")
                )
                if (
                    decision.tzinfo is None
                    or outcome.tzinfo is None
                    or finalized.tzinfo is None
                    or not decision < outcome <= finalized
                ):
                    raise ValueError("invalid context timestamp ordering")
            except (TypeError, ValueError):
                failures.append(f"timeline@{symbol}@{day}")
        if failures:
            blockers.append({"code": "invalid_context_receipts", "count": len(failures), "examples": failures[:5]})
        blockers.append({"code": "signed_session_calendar_not_enrolled", "count": 1})
        integrity_ready = not any(
            blocker["code"]
            not in {
                "missing_decision_context_rows",
                "missing_finalized_context_rows",
                "signed_session_calendar_not_enrolled",
            }
            for blocker in blockers
        )
        return {
            "ready": not blockers,
            "integrity_ready": integrity_ready,
            "selected_rows": len(selected),
            "decision_rows": sum(key[2] == "pre_open" for key in selected),
            "finalized_rows": sum(key[2] == "post_close" for key in selected),
            "blockers": blockers,
        }
    finally:
        connection.close()
