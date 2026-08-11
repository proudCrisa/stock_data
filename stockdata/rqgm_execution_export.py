"""Export one fixed RQGM research panel as execution-grade artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .availability import price_availability_error
from .execution_snapshot import ARTIFACT_SCHEMAS, create_execution_snapshot
from .historical_universe_attestation import load_historical_universe_attestation
from .ticker import normalize, to_baostock


_RULEBOOK_ID = "cn-a-share-daily-execution/2015-08-01..2026-v1"


def _receipt_covers_bar(response_json: str, row: sqlite3.Row) -> bool:
    try:
        payload = json.loads(response_json)
        fields = payload["fields"].split(",")
        positions = {name: index for index, name in enumerate(fields)}
        required = ("date", "open", "high", "low", "close", "volume")
        if not all(name in positions for name in required):
            return False
        for candidate in payload["rows"]:
            if candidate[positions["date"]] != row["date"]:
                continue
            if all(
                float(candidate[positions[name]]) == float(row[name])
                for name in required[1:]
            ):
                return True
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return False


def _timestamp_on(day: str) -> str:
    return datetime.combine(
        date.fromisoformat(day), time(15, 1), ZoneInfo("Asia/Shanghai")
    ).isoformat()


def _panel_from_overlay(
    path: Path,
    coverage_start: str,
    coverage_end: str,
    *,
    warmup_bars: int = 0,
) -> tuple[tuple[str, ...], tuple[str, ...], set[tuple[str, str]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = raw.get("splits", {}).get("search-validation")
    if not isinstance(values, list) or not values:
        raise ValueError("split overlay has no search-validation panel")
    keys: set[tuple[str, str]] = set()
    for value in values:
        symbol, separator, day = str(value).partition("@")
        if not separator or not coverage_start <= day <= coverage_end:
            raise ValueError("search-validation sample falls outside requested coverage")
        key = (normalize(symbol), date.fromisoformat(day).isoformat())
        if key in keys:
            raise ValueError("search-validation panel contains duplicate samples")
        keys.add(key)
    symbols = tuple(sorted({symbol for symbol, _ in keys}))
    search_dates = tuple(sorted({day for _, day in keys}))
    if warmup_bars < 0:
        raise ValueError("warmup_bars must be non-negative")
    if warmup_bars:
        train = raw.get("splits", {}).get("train")
        if not isinstance(train, list):
            raise ValueError("split overlay has no train panel for warmup")
        train_dates = sorted(
            {
                str(value).partition("@")[2]
                for value in train
                if str(value).partition("@")[0] in symbols
                and str(value).partition("@")[2] < search_dates[0]
            }
        )
        if len(train_dates) < warmup_bars:
            raise ValueError("train panel does not contain enough warmup bars")
        warmup_dates = tuple(train_dates[-warmup_bars:])
        keys.update((symbol, day) for symbol in symbols for day in warmup_dates)
    dates = tuple(sorted({day for _, day in keys}))
    expected = {(symbol, day) for symbol in symbols for day in dates}
    if keys != expected:
        raise ValueError("search-validation panel is not a complete symbol-date product")
    return symbols, dates, keys


def _price_rows(
    database: Path,
    panel: set[tuple[str, str]],
    *,
    source: str,
    adjustment_mode: str,
    adjustment_version: str,
) -> list[dict[str, object]]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT d.code,d.date,d.open,d.high,d.low,d.close,d.volume,d.retrieved_at,
                   d.receipt_id,r.observed_at,r.response_json,r.response_sha256
            FROM daily AS d
            JOIN collection_receipts AS r
              ON r.receipt_id=d.receipt_id AND r.source=d.source
            WHERE d.source=? AND d.adjustment_mode=? AND d.adjustment_version=?
              AND d.is_final=1
            ORDER BY d.code,d.date
            """,
            (source, adjustment_mode, adjustment_version),
        ).fetchall()
    finally:
        connection.close()
    selected = {
        (normalize(str(row["code"])), str(row["date"])): row
        for row in rows
        if (normalize(str(row["code"])), str(row["date"])) in panel
    }
    if set(selected) != panel:
        missing = sorted(panel - set(selected))
        raise ValueError(f"{adjustment_mode} prices do not cover panel: {missing[:3]}")
    result = []
    session_days = sorted({day for _, day in selected})
    next_session = dict(zip(session_days, session_days[1:]))
    for (symbol, day), row in sorted(selected.items()):
        actual_digest = hashlib.sha256(
            str(row["response_json"]).encode("utf-8")
        ).hexdigest()
        if actual_digest != row["response_sha256"] or not _receipt_covers_bar(
            str(row["response_json"]), row
        ):
            raise ValueError(
                f"{source} price has no valid immutable collection receipt: {symbol}@{day}"
            )
        retrieved_at = str(row["retrieved_at"])
        available_at = datetime.fromisoformat(
            str(row["observed_at"]).replace("Z", "+00:00")
        )
        row_retrieved_at = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        if available_at.tzinfo is None or row_retrieved_at.tzinfo is None:
            raise ValueError(
                f"{adjustment_mode} price has invalid receipt time: {symbol}@{day}"
            )
        if available_at != row_retrieved_at:
            raise ValueError(
                f"{adjustment_mode} price receipt timestamp mismatch: {symbol}@{day}"
            )
        availability_error = price_availability_error(
            day, available_at, next_session.get(day)
        )
        if availability_error:
            raise ValueError(
                f"{adjustment_mode} price has no point-in-time availability "
                f"({availability_error}): {symbol}@{day}"
            )
        result.append(
        {
            "effective_date": day,
            "available_at": str(row["observed_at"]),
            "receipt_id": int(row["receipt_id"]),
            "symbol": symbol,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        )
    return result


def _historical_universe_rows(
    path: Path,
    authority_manifest: Path,
    panel: set[tuple[str, str]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows, manifest = load_historical_universe_attestation(path, authority_manifest)
    selected = {
        (normalize(str(row.get("symbol", ""))), str(row.get("effective_date", ""))): row
        for row in rows
        if (normalize(str(row.get("symbol", ""))), str(row.get("effective_date", ""))) in panel
    }
    if set(selected) != panel:
        raise ValueError("historical universe authority does not cover the frozen panel")
    # The full signed universe, rather than a self-selected projection, is part
    # of the snapshot so RQGM can bind the artifact hash to the signed payload.
    return rows, manifest


def _query_rows(result: object, label: str) -> list[dict[str, str]]:
    if getattr(result, "error_code", None) != "0":
        raise RuntimeError(f"BaoStock {label} failed: {getattr(result, 'error_msg', '')}")
    fields = list(getattr(result, "fields"))
    rows: list[dict[str, str]] = []
    while result.next():
        rows.append(dict(zip(fields, result.get_row_data())))
    return rows


def _number(value: str) -> float:
    return float(value) if value.strip() else 0.0


def _board(symbol: str) -> str:
    digits = symbol.split(".", 1)[0]
    if digits.startswith(("300", "301")):
        return "CHINEXT"
    if digits.startswith(("688", "689")):
        return "STAR"
    if digits.startswith(("4", "8", "92")):
        return "BSE"
    return "MAIN"


def _announcement_timestamp(row: Mapping[str, str]) -> str:
    candidates = sorted(
        value
        for key in (
            "dividPreNoticeDate",
            "dividAgmPumDate",
            "dividPlanAnnounceDate",
            "dividPlanDate",
        )
        if (value := row.get(key, ""))
    )
    if not candidates:
        raise ValueError("corporate action has no historical announcement date")
    return _timestamp_on(candidates[0])


def _collect_baostock_history(
    symbols: Iterable[str], dates: tuple[str, ...]
) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    import baostock as bs

    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status_rows: list[dict[str, object]] = []
    action_by_identity: dict[tuple[object, ...], dict[str, object]] = {}
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        for symbol in symbols:
            code = to_baostock(symbol)
            basics = _query_rows(bs.query_stock_basic(code=code), f"basic {symbol}")
            if len(basics) != 1:
                raise ValueError(f"BaoStock basic identity is ambiguous for {symbol}")
            basic = basics[0]
            ipo_date = basic["ipoDate"]
            out_date = basic["outDate"]
            history = _query_rows(
                bs.query_history_k_data_plus(
                    code,
                    "date,tradestatus,isST",
                    start_date=dates[0],
                    end_date=dates[-1],
                    frequency="d",
                    adjustflag="3",
                ),
                f"status {symbol}",
            )
            by_date = {row["date"]: row for row in history}
            if set(by_date) != set(dates):
                missing = sorted(set(dates) - set(by_date))
                raise ValueError(f"historical status does not cover {symbol}: {missing[:3]}")
            for day in dates:
                row = by_date[day]
                listed = ipo_date <= day and (not out_date or day < out_date)
                status_rows.append(
                    {
                        "effective_date": day,
                        "available_at": retrieved_at,
                        "symbol": symbol,
                        "listing_status": "listed" if listed else "unlisted",
                        "listing_date": ipo_date,
                        "board": _board(symbol),
                        "is_st": row["isST"] == "1",
                        "is_suspended": row["tradestatus"] != "1",
                    }
                )
            for year in range(date.fromisoformat(dates[0]).year, date.fromisoformat(dates[-1]).year + 1):
                dividends = _query_rows(
                    bs.query_dividend_data(
                        code=code, year=str(year), yearType="report"
                    ),
                    f"dividend {symbol} {year}",
                )
                for row in dividends:
                    effective_date = row.get("dividOperateDate", "")
                    if not dates[0] <= effective_date <= dates[-1]:
                        continue
                    cash = _number(row.get("dividCashPsBeforeTax", ""))
                    stock = _number(row.get("dividStocksPs", ""))
                    reserve = _number(row.get("dividReserveToStockPs", ""))
                    multiplier = 1.0 + stock + reserve
                    payload = {
                        "cash_dividend_per_pre_action_share": cash,
                        "share_multiplier": multiplier,
                        "source": "baostock.query_dividend_data",
                    }
                    identity = (symbol, effective_date, cash, multiplier)
                    action_by_identity[identity] = {
                        "effective_date": effective_date,
                        "available_at": _announcement_timestamp(row),
                        "symbol": symbol,
                        "action_type": (
                            "cash_dividend"
                            if multiplier == 1.0
                            else "dividend_and_share_distribution"
                        ),
                        "payload": payload,
                    }
    finally:
        bs.logout()
    return status_rows, list(action_by_identity.values()), retrieved_at


def export_rqgm_execution_snapshot(
    *,
    raw_database: Path,
    historical_universe: Path | None = None,
    historical_universe_manifest: Path | None = None,
    split_overlay: Path,
    output_root: Path,
    coverage_start: str,
    coverage_end: str,
    warmup_bars: int = 0,
) -> dict[str, object]:
    if historical_universe is None or historical_universe_manifest is None:
        raise ValueError("historical universe authority is required; fixed panels are not a universe")
    symbols, dates, panel = _panel_from_overlay(
        split_overlay,
        coverage_start,
        coverage_end,
        warmup_bars=warmup_bars,
    )
    if dates[0] != coverage_start or dates[-1] != coverage_end:
        raise ValueError("coverage boundaries must equal the frozen panel boundaries")
    execution_prices = _price_rows(
        raw_database,
        panel,
        source="baostock",
        adjustment_mode="raw",
        adjustment_version="baostock-adjustflag-3",
    )
    signal_prices = _price_rows(
        raw_database,
        panel,
        source="baostock",
        adjustment_mode="raw",
        adjustment_version="baostock-adjustflag-3",
    )
    instrument_status, corporate_actions, bound_at = _collect_baostock_history(
        symbols, dates
    )
    if not corporate_actions:
        raise ValueError("panel requires a non-empty authoritative corporate-action ledger")

    universe, universe_manifest = _historical_universe_rows(
        historical_universe, historical_universe_manifest, panel
    )
    market_rules = [
        {
            "effective_date": coverage_start,
            "available_at": bound_at,
            "rule_id": "cn-a-share-fees-by-exchange/2015-08-01..2026-v2",
            "rule_type": "fee_schedule",
            "parameters": {
                "commission_rate": 0.0003,
                "minimum_commission": 5.0,
                "stamp_duty_before_2023_08_28": 0.001,
                "stamp_duty_from_2023_08_28": 0.0005,
                "transfer_fee_from_2022_04_29": 0.00001,
            },
        },
        {
            "effective_date": coverage_start,
            "available_at": bound_at,
            "rule_id": "cn-a-share-price-limits/1996-12-16..2026-v1",
            "rule_type": "price_limit_and_lot_rules",
            "parameters": {
                "lot_size": 100,
                "settlement": "T+1",
                "main": 0.10,
                "st": 0.05,
                "chinext_from_2020_08_24": 0.20,
                "star": 0.20,
                "bse": 0.30,
            },
        },
    ]
    artifacts = {
        "execution_prices": execution_prices,
        "signal_prices": signal_prices,
        "corporate_actions": corporate_actions,
        "instrument_status": instrument_status,
        "universe": universe,
        "market_rules": market_rules,
    }
    if set(artifacts) != set(ARTIFACT_SCHEMAS):
        raise AssertionError("execution artifact set drifted")
    authorities = {
        "execution_prices": "baostock.query_history_k_data_plus:adjustflag=3",
        "signal_prices": "baostock.query_history_k_data_plus:adjustflag=3:signal-close",
        "corporate_actions": "baostock.query_dividend_data:report",
        "instrument_status": "baostock.query_history_k_data_plus:tradestatus,isST+query_stock_basic",
        "universe": str(universe_manifest["payload"]["issuer"]),
        "market_rules": _RULEBOOK_ID,
    }
    return create_execution_snapshot(
        output_root,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        artifacts=artifacts,
        authorities=authorities,
        signal_price_basis="raw",
        selection_policy_id=str(universe_manifest["payload"]["selection_policy_id"]),
        rulebook_id=_RULEBOOK_ID,
        universe_attestation=universe_manifest,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stockdata.rqgm_execution_export")
    parser.add_argument("--raw-database", required=True, type=Path)
    parser.add_argument("--historical-universe", required=True, type=Path)
    parser.add_argument("--historical-universe-manifest", required=True, type=Path)
    parser.add_argument("--split-overlay", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--warmup-bars", type=int, default=0)
    args = parser.parse_args(argv)
    manifest = export_rqgm_execution_snapshot(
        raw_database=args.raw_database,
        historical_universe=args.historical_universe,
        historical_universe_manifest=args.historical_universe_manifest,
        split_overlay=args.split_overlay,
        output_root=args.output,
        coverage_start=args.start,
        coverage_end=args.end,
        warmup_bars=args.warmup_bars,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
