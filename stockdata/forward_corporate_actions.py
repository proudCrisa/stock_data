"""Forward-only BaoStock dividend observations for a fixed research cohort.

This is supporting evidence, not a complete corporate-action authority.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, time
from typing import Callable, Iterable, Protocol, cast
from zoneinfo import ZoneInfo

from .cache import Cache
from .forward_context import _cohort
from .ticker import normalize, to_baostock


SCHEMA_VERSION = "stockdata-forward-corporate-actions/1"
SOURCE = "baostock-query-dividend-data-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PREOPEN_START = time(8, 30)
_DECISION_CUTOFF = time(9, 25)

_TRIGGER_SQL = {
    "forward_corporate_action_coverage_no_update": """
        CREATE TRIGGER forward_corporate_action_coverage_no_update
        BEFORE UPDATE ON forward_corporate_action_coverage BEGIN
            SELECT RAISE(ABORT, 'corporate-action coverage is append-only');
        END
    """,
    "forward_corporate_action_coverage_no_delete": """
        CREATE TRIGGER forward_corporate_action_coverage_no_delete
        BEFORE DELETE ON forward_corporate_action_coverage BEGIN
            SELECT RAISE(ABORT, 'corporate-action coverage is append-only');
        END
    """,
    "forward_corporate_actions_no_update": """
        CREATE TRIGGER forward_corporate_actions_no_update
        BEFORE UPDATE ON forward_corporate_actions BEGIN
            SELECT RAISE(ABORT, 'corporate actions are append-only');
        END
    """,
    "forward_corporate_actions_no_delete": """
        CREATE TRIGGER forward_corporate_actions_no_delete
        BEFORE DELETE ON forward_corporate_actions BEGIN
            SELECT RAISE(ABORT, 'corporate actions are append-only');
        END
    """,
}


class CapturedCorporateActions(dict[str, list[dict[str, object]]]):
    """Exact per-symbol provider rows plus their collection receipt."""

    def __init__(
        self,
        rows: dict[str, list[dict[str, object]]],
        receipt: dict[str, object],
    ) -> None:
        super().__init__(rows)
        self.capture_receipt = receipt


class _BaoStockQueryResult(Protocol):
    def next(self) -> bool: ...

    def get_row_data(self) -> Iterable[object]: ...


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).rstrip(";").lower()


def _ensure_schema(cache: Cache) -> None:
    cache._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS forward_corporate_action_coverage (
            observation_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            available_at TEXT NOT NULL,
            source TEXT NOT NULL,
            receipt_id INTEGER NOT NULL,
            event_count INTEGER NOT NULL CHECK (event_count >= 0),
            PRIMARY KEY (observation_date,symbol,source),
            FOREIGN KEY (receipt_id) REFERENCES collection_receipts(receipt_id)
        );
        CREATE TABLE IF NOT EXISTS forward_corporate_actions (
            observation_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            event_id TEXT NOT NULL,
            effective_date TEXT,
            announcement_date TEXT,
            payload_json TEXT NOT NULL,
            available_at TEXT NOT NULL,
            source TEXT NOT NULL,
            receipt_id INTEGER NOT NULL,
            PRIMARY KEY (observation_date,symbol,event_id,source),
            FOREIGN KEY (receipt_id) REFERENCES collection_receipts(receipt_id)
        );
        """
    )
    for sql in _TRIGGER_SQL.values():
        cache._conn.execute(sql.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1))


def _query_rows(result: object, label: str) -> tuple[list[str], list[list[str]]]:
    query_result = cast(_BaoStockQueryResult, result)
    if str(getattr(result, "error_code", "")) != "0":
        raise RuntimeError(f"BaoStock {label} failed: {getattr(result, 'error_msg', '')}")
    fields = [str(item) for item in getattr(result, "fields", [])]
    rows: list[list[str]] = []
    while query_result.next():
        rows.append([str(item) for item in query_result.get_row_data()])
    return fields, rows


def fetch_baostock_corporate_actions(
    symbols: tuple[str, ...],
    observation_date: str,
) -> CapturedCorporateActions:
    """Query every cohort symbol and preserve zero-event responses."""
    import baostock as bs

    observed_at = datetime.now(_SHANGHAI).isoformat(timespec="seconds")
    day = date.fromisoformat(observation_date)
    years = (day.year - 1, day.year)
    response: dict[str, list[dict[str, object]]] = {}
    parsed: dict[str, list[dict[str, object]]] = {}
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        login = bs.login()
    if str(getattr(login, "error_code", "")) != "0":
        raise RuntimeError(f"BaoStock login failed: {getattr(login, 'error_msg', '')}")
    try:
        for symbol in symbols:
            provider_batches: list[dict[str, object]] = []
            provider_rows: list[dict[str, object]] = []
            for year in years:
                result = bs.query_dividend_data(
                    code=to_baostock(symbol), year=str(year), yearType="report"
                )
                fields, rows = _query_rows(result, f"corporate actions {symbol}/{year}")
                provider_batches.append({"year": year, "fields": fields, "rows": rows})
                provider_rows.extend(dict(zip(fields, row)) for row in rows)
            response[symbol] = provider_batches
            parsed[symbol] = provider_rows
    finally:
        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
            bs.logout()
    receipt: dict[str, object] = {
        "observed_at": observed_at,
        "source": SOURCE,
        "request": {"symbols": list(symbols), "observation_date": observation_date, "years": list(years)},
        "response": {"symbols": response},
    }
    return CapturedCorporateActions(parsed, receipt)


def _source_rows(response: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(response, dict) or not isinstance(response.get("symbols"), dict):
        raise ValueError("corporate-action receipt response is invalid")
    parsed: dict[str, list[dict[str, object]]] = {}
    for raw_symbol, batches in response["symbols"].items():
        symbol = normalize(str(raw_symbol))
        if not isinstance(batches, list):
            raise ValueError("corporate-action provider batches must be a list")
        rows: list[dict[str, object]] = []
        for batch in batches:
            if not isinstance(batch, dict):
                raise ValueError("corporate-action provider batch is invalid")
            fields = batch.get("fields")
            raw_rows = batch.get("rows")
            if not isinstance(fields, list) or not isinstance(raw_rows, list):
                raise ValueError("corporate-action provider fields/rows are invalid")
            for raw_row in raw_rows:
                if not isinstance(raw_row, list) or len(raw_row) != len(fields):
                    raise ValueError("corporate-action provider row width mismatch")
                rows.append(dict(zip((str(item) for item in fields), raw_row)))
        parsed[symbol] = rows
    return parsed


def _event_fields(row: dict[str, object]) -> tuple[str | None, str | None]:
    effective = str(row.get("dividOperateDate", "")).strip() or None
    announcements = sorted(
        value
        for key in ("dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate")
        if (value := str(row.get(key, "")).strip())
    )
    for value in ([effective] if effective else []) + announcements:
        date.fromisoformat(value)
    return effective, announcements[0] if announcements else None


def _request_matches(
    request_json: str,
    source_rows: dict[str, list[dict[str, object]]],
    observation_date: str,
) -> bool:
    try:
        request = json.loads(request_json)
        request_symbols = {
            normalize(str(item)) for item in request.get("symbols", [])
        }
        year = date.fromisoformat(observation_date).year
        request_years = {int(item) for item in request.get("years", [])}
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        request.get("observation_date") == observation_date
        and request_symbols == set(source_rows)
        and request_years == {year - 1, year}
    )


def capture_forward_corporate_actions(
    cache: Cache,
    observation_date: str,
    *,
    fetcher: Callable[[tuple[str, ...], str], CapturedCorporateActions] = fetch_baostock_corporate_actions,
    now: datetime | None = None,
) -> dict[str, object]:
    """Capture one same-day pre-open complete cohort response without backfill."""
    day = date.fromisoformat(observation_date)
    current = now or datetime.now(_SHANGHAI)
    if current.tzinfo is None:
        raise ValueError("capture time must be timezone-aware")
    local = current.astimezone(_SHANGHAI)
    if local.date() != day:
        raise ValueError("corporate-action capture cannot backfill another date")
    if day.weekday() >= 5:
        raise ValueError("corporate-action capture requires a weekday session candidate")
    if not (_PREOPEN_START <= local.time() < _DECISION_CUTOFF):
        raise ValueError("corporate-action capture requires the pre-open evidence window")

    cache._require_collector_writer(
        step_id="pre_open_corporate_actions", session=observation_date
    )
    symbols, cohort_sha256 = _cohort(cache)
    with cache._conn:
        _ensure_schema(cache)
        existing = int(
            cache._conn.execute(
                "SELECT COUNT(*) FROM forward_corporate_action_coverage "
                "WHERE observation_date=? AND source=?",
                (observation_date, SOURCE),
            ).fetchone()[0]
        )
        if existing == len(symbols):
            return {
                "schema_version": SCHEMA_VERSION,
                "captured": False,
                "observation_date": observation_date,
                "cohort_sha256": cohort_sha256,
                "cohort_size": len(symbols),
                "source": SOURCE,
            }
        if existing:
            raise ValueError("partial corporate-action coverage already exists")

    captured = fetcher(symbols, observation_date)
    receipt = captured.capture_receipt
    if receipt.get("source") != SOURCE:
        raise ValueError("corporate-action source identity mismatch")
    observed_at = str(receipt.get("observed_at", ""))
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("corporate-action receipt must be timezone-aware")
    observed_local = observed.astimezone(_SHANGHAI)
    if observed_local.date() != day:
        raise ValueError("corporate-action receipt date mismatch")
    if not (_PREOPEN_START <= observed_local.time() < _DECISION_CUTOFF):
        raise ValueError("corporate-action receipt is outside the pre-open window")
    verification_now = now or datetime.now(_SHANGHAI)
    if observed > verification_now.astimezone(observed.tzinfo):
        raise ValueError("corporate-action receipt is future-dated")

    raw = _source_rows(receipt.get("response"))
    expected_symbols = set(symbols)
    if set(raw) != expected_symbols or set(captured) != expected_symbols:
        raise ValueError("corporate-action receipt does not cover the exact cohort")
    if any(list(captured[symbol]) != raw[symbol] for symbol in symbols):
        raise ValueError("corporate-action parsed rows differ from provider response")

    with cache._conn:
        receipt_id = cache._record_capture_receipt(receipt)
        coverage_rows = []
        event_rows = []
        for symbol in symbols:
            rows = raw[symbol]
            coverage_rows.append(
                (observation_date, symbol, observed_at, SOURCE, receipt_id, len(rows))
            )
            for row in rows:
                payload_json = _canonical_json(row)
                event_id = hashlib.sha256(payload_json.encode("ascii")).hexdigest()
                effective, announcement = _event_fields(row)
                event_rows.append(
                    (
                        observation_date,
                        symbol,
                        event_id,
                        effective,
                        announcement,
                        payload_json,
                        observed_at,
                        SOURCE,
                        receipt_id,
                    )
                )
        cache._conn.executemany(
            "INSERT INTO forward_corporate_action_coverage VALUES (?,?,?,?,?,?)",
            coverage_rows,
        )
        cache._conn.executemany(
            "INSERT INTO forward_corporate_actions VALUES (?,?,?,?,?,?,?,?,?)",
            event_rows,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "captured": True,
        "observation_date": observation_date,
        "decision_available_at": observed_at,
        "cohort_sha256": cohort_sha256,
        "cohort_size": len(symbols),
        "event_count": len(event_rows),
        "zero_event_symbols": sum(not raw[symbol] for symbol in symbols),
        "source": SOURCE,
    }


def check_forward_corporate_action_readiness(
    database: str,
    panel: Iterable[tuple[str, str]],
) -> dict[str, object]:
    """Verify forward completeness; authority enrollment remains a hard blocker."""
    expected = {
        (normalize(symbol), date.fromisoformat(day).isoformat()) for symbol, day in panel
    }
    blockers: list[dict[str, object]] = []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {
            "forward_corporate_action_coverage",
            "forward_corporate_actions",
            "collection_receipts",
        }
        missing_tables = sorted(required - tables)
        if missing_tables:
            return {
                "ready": False,
                "integrity_ready": False,
                "blockers": [{"code": "missing_corporate_action_tables", "examples": missing_tables}],
            }
        triggers = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
        }
        invalid_triggers = sorted(
            name
            for name, sql in _TRIGGER_SQL.items()
            if name not in triggers or _normalized_sql(triggers[name]) != _normalized_sql(sql)
        )
        if invalid_triggers:
            blockers.append(
                {"code": "invalid_corporate_action_triggers", "count": len(invalid_triggers), "examples": invalid_triggers}
            )
        rows = connection.execute(
            """
            SELECT c.*,r.observed_at,r.source AS receipt_source,
                   r.request_json,r.response_json,r.response_sha256
            FROM forward_corporate_action_coverage AS c
            JOIN collection_receipts AS r ON r.receipt_id=c.receipt_id
            WHERE c.source=?
            """,
            (SOURCE,),
        ).fetchall()
        selected = {
            (str(row["symbol"]), str(row["observation_date"])): row
            for row in rows
            if (str(row["symbol"]), str(row["observation_date"])) in expected
        }
        missing = sorted(expected - set(selected))
        if missing:
            blockers.append({"code": "missing_corporate_action_coverage", "count": len(missing), "examples": missing[:5]})
        failures: list[str] = []
        checked_requests: set[int] = set()
        for key, row in selected.items():
            response_json = str(row["response_json"])
            if (
                row["receipt_source"] != SOURCE
                or hashlib.sha256(response_json.encode("utf-8")).hexdigest() != row["response_sha256"]
                or row["observed_at"] != row["available_at"]
            ):
                failures.append(f"{key[0]}@{key[1]}")
                continue
            try:
                source_rows = _source_rows(json.loads(response_json))
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                failures.append(f"response@{key[1]}")
                continue
            receipt_id = int(row["receipt_id"])
            if receipt_id not in checked_requests:
                if not _request_matches(
                    str(row["request_json"]), source_rows, key[1]
                ):
                    failures.append(f"request@{key[1]}")
                checked_requests.add(receipt_id)
            expected_events = set()
            for source_row in source_rows.get(key[0], []):
                payload_json = _canonical_json(source_row)
                effective, announcement = _event_fields(source_row)
                expected_events.add(
                    (
                        hashlib.sha256(payload_json.encode("ascii")).hexdigest(),
                        effective,
                        announcement,
                        payload_json,
                        str(row["available_at"]),
                    )
                )
            actual_events = {
                (
                    str(item["event_id"]),
                    item["effective_date"],
                    item["announcement_date"],
                    str(item["payload_json"]),
                    str(item["available_at"]),
                )
                for item in connection.execute(
                    "SELECT event_id,effective_date,announcement_date,payload_json,available_at "
                    "FROM forward_corporate_actions "
                    "WHERE observation_date=? AND symbol=? AND source=? AND receipt_id=?",
                    (key[1], key[0], SOURCE, receipt_id),
                )
            }
            if (
                key[0] not in source_rows
                or len(expected_events) != int(row["event_count"])
                or actual_events != expected_events
            ):
                failures.append(f"{key[0]}@{key[1]}")
        if failures:
            blockers.append({"code": "invalid_corporate_action_receipts", "count": len(failures), "examples": failures[:5]})
        cutoff_failures = []
        for key, row in selected.items():
            try:
                available = datetime.fromisoformat(
                    str(row["available_at"]).replace("Z", "+00:00")
                )
                if available.tzinfo is None:
                    raise ValueError("naive dividend observation")
                local = available.astimezone(_SHANGHAI)
                valid = (
                    local.date().isoformat() == key[1]
                    and _PREOPEN_START <= local.time() < _DECISION_CUTOFF
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                cutoff_failures.append(f"{key[0]}@{key[1]}")
        if cutoff_failures:
            blockers.append({"code": "corporate_actions_outside_preopen_cutoff", "count": len(cutoff_failures), "examples": cutoff_failures[:5]})
        integrity_ready = not blockers
        blockers.extend(
            [
                {
                    "code": "dividend_observation_not_full_corporate_action_ledger",
                    "count": 1,
                },
                {"code": "corporate_action_revisions_not_supported", "count": 1},
                {"code": "corporate_action_publisher_key_not_enrolled", "count": 1},
            ]
        )
        return {
            "ready": False,
            "integrity_ready": integrity_ready,
            "usable_for_execution_export": False,
            "selected_rows": len(selected),
            "zero_event_rows": sum(int(row["event_count"]) == 0 for row in selected.values()),
            "blockers": blockers,
        }
    finally:
        connection.close()
