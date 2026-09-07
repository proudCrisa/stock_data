"""Bounded current-day source capture for A-share candidate admission.

The artifact records observed BaoStock responses. It is intentionally not an
execution-grade or historical replay authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .adjustment_identity import (
    SIGNAL_ADJUSTMENT_SCHEMA,
    verify_adjustment_identity,
)
from .fetch_tencent_history import TENCENT_HISTORY_SOURCE, fetch_tencent_history

SCHEMA_VERSION = "stockdata-a-share-candidate-admission-capture/2"
MAX_CODES = 20
SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE = re.compile(r"^(?:sh|sz|bj)\d{6}$")
_AUTHORITY = {
    "status": "observed_provider_response",
    "execution_grade": False,
    "decision_authority": False,
    "publisher_authenticated": False,
    "historical_replay_grade": False,
    "revision_complete": False,
}
_QUERIES = [
    "query_profit_data",
    "query_cash_flow_data",
    "query_stock_industry",
    "query_adjust_factor",
]
_QUERY_CONTRACT = {
    "finance": {
        "publish_date_cutoff": "strictly_before_asof",
        "period_selection": "latest_published_plus_ttm_dependencies",
    },
    "industry": {
        "availability": "provider_current_observation",
    },
    "corporate_actions": {
        "coverage_days": 90,
        "range": "inclusive_start_and_end",
    },
}
_METRIC_METHODS = {
    "net_profit_ytd": {"provider_netProfit"},
    "revenue_ytd": {
        "provider_MBRevenue",
        "derived_net_profit_div_np_margin",
    },
    "ocf_ytd": {"derived_net_profit_mul_CFOToNP"},
    "roe_ytd_pct": {"provider_roeAvg_ratio_mul_100"},
}
_SIGNAL_LOOKBACK_DAYS = 180


class CandidateAdmissionCaptureError(ValueError):
    """Raised when a candidate-admission source artifact is invalid."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise CandidateAdmissionCaptureError("capture is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _number(value: object) -> float | None:
    if value in (None, "", "-") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _day(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise CandidateAdmissionCaptureError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CandidateAdmissionCaptureError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise CandidateAdmissionCaptureError(f"{field} must be an ISO date")
    return parsed


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CandidateAdmissionCaptureError(f"{field} must be timezone-aware")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateAdmissionCaptureError(f"{field} must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateAdmissionCaptureError(f"{field} must be timezone-aware")
    return parsed


def _codes(values: Sequence[str]) -> list[str]:
    codes = list(values)
    if not codes or len(codes) > MAX_CODES or len(codes) != len(set(codes)):
        raise CandidateAdmissionCaptureError(
            f"codes must contain 1..{MAX_CODES} unique symbols"
        )
    if any(not isinstance(code, str) or not _CODE.fullmatch(code) for code in codes):
        raise CandidateAdmissionCaptureError("codes must use sh/sz/bj six-digit form")
    return codes


def _sdk_version() -> str:
    try:
        return version("baostock")
    except PackageNotFoundError as exc:
        raise CandidateAdmissionCaptureError("baostock package version unavailable") from exc


def _signal_history(code: str, asof: str) -> dict[str, object]:
    asof_day = _day(asof, "asof")
    now = datetime.now(SHANGHAI)
    if asof_day > now.date() or (asof_day == now.date() and now.time() < time(15, 1)):
        raise CandidateAdmissionCaptureError(
            "qfq signal history requires a finalized admission day"
        )
    start = (asof_day - timedelta(days=_SIGNAL_LOOKBACK_DAYS)).isoformat()
    captured = fetch_tencent_history(code, start, asof, adjustment_mode="qfq")
    rows = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in captured
    ]
    receipt = captured.capture_receipt
    if len(rows) < 25 or rows[-1].get("date") != asof:
        raise CandidateAdmissionCaptureError(
            "qfq signal history is incomplete at admission watermark"
        )
    identity = {
        "schema_version": SIGNAL_ADJUSTMENT_SCHEMA,
        "price_role": "signal",
        "source": TENCENT_HISTORY_SOURCE,
        "adjustment_mode": "qfq",
        "adjustment_version": f"{TENCENT_HISTORY_SOURCE}-qfq",
    }
    return {
        "identity": identity,
        "identity_sha256": _sha256(identity),
        "requested_start": start,
        "requested_end": asof,
        "watermark": asof,
        "rows_sha256": _sha256(rows),
        "receipt_sha256": _sha256(receipt),
        "rows": rows,
        "receipt": receipt,
    }


def _verify_signal_history(
    value: object, code: str, asof: str, *, knowledge_time: datetime | None = None
) -> None:
    if not isinstance(value, Mapping):
        raise CandidateAdmissionCaptureError("qfq signal history is missing")
    try:
        identity = verify_adjustment_identity(
            value.get("identity"), expected_price_role="signal"
        )
    except ValueError as exc:
        raise CandidateAdmissionCaptureError("qfq signal identity is invalid") from exc
    rows = value.get("rows")
    receipt = value.get("receipt")
    if identity.source != TENCENT_HISTORY_SOURCE \
            or identity.adjustment_mode != "qfq" \
            or identity.adjustment_version != f"{TENCENT_HISTORY_SOURCE}-qfq" \
            or value.get("identity_sha256") != _sha256(value.get("identity")) \
            or not isinstance(rows, list) or len(rows) < 25 \
            or not isinstance(receipt, Mapping) \
            or value.get("requested_end") != asof \
            or value.get("watermark") != asof \
            or value.get("rows_sha256") != _sha256(rows) \
            or value.get("receipt_sha256") != _sha256(receipt):
        raise CandidateAdmissionCaptureError("qfq signal history is invalid")
    try:
        requested_start = _day(value.get("requested_start"), "requested_start")
    except CandidateAdmissionCaptureError as exc:
        raise CandidateAdmissionCaptureError("qfq signal history is invalid") from exc
    asof_day = _day(asof, "asof")
    if requested_start > asof_day - timedelta(days=90):
        raise CandidateAdmissionCaptureError("qfq signal history is too short")
    dates: list[str] = []
    retrieved_times: list[datetime] = []
    for row in rows:
        if not isinstance(row, Mapping) \
                or row.get("source") != TENCENT_HISTORY_SOURCE \
                or row.get("adjustment_mode") != "qfq" \
                or row.get("adjustment_version") != f"{TENCENT_HISTORY_SOURCE}-qfq" \
                or row.get("is_final") is not True \
                or any((_number(row.get(field)) or 0) <= 0
                       for field in ("open", "close", "high", "low")) \
                or _number(row.get("volume")) is None \
                or float(row["volume"]) < 0:
            raise CandidateAdmissionCaptureError("qfq signal row is invalid")
        dates.append(_day(row.get("date"), "signal row date").isoformat())
        retrieved_times.append(
            _timestamp(row.get("retrieved_at"), "signal row retrieved_at")
        )
    request = receipt.get("request")
    response = receipt.get("response")
    if dates != sorted(set(dates)) or dates[-1] != asof \
            or receipt.get("source") != TENCENT_HISTORY_SOURCE \
            or not isinstance(request, Mapping) or request.get("code") != code \
            or request.get("start") != value.get("requested_start") \
            or request.get("end") != asof or request.get("adjustment_mode") != "qfq" \
            or not isinstance(response, Mapping) \
            or response.get("bar_count") != len(rows) \
            or response.get("coverage_start") != dates[0] \
            or response.get("coverage_end") != asof:
        raise CandidateAdmissionCaptureError("qfq signal receipt is invalid")
    signal_observed = _timestamp(
        receipt.get("observed_at"), "signal receipt observed_at"
    )
    if any(retrieved > signal_observed for retrieved in retrieved_times) \
            or knowledge_time is not None and signal_observed > knowledge_time:
        raise CandidateAdmissionCaptureError("qfq signal chronology is invalid")


def seal_capture(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["artifact_sha256"] = _sha256(result)
    return result


def build_capture(
    codes: Sequence[str],
    *,
    asof: str,
    capture_one: Callable[[str, str], Mapping[str, object]],
    observed_at: str | None = None,
) -> dict[str, object]:
    """Capture a bounded shortlist and seal both successes and blockers."""

    asof_day = _day(asof, "asof")
    requested = _codes(codes)

    records: list[dict[str, object]] = []
    blockers: list[dict[str, str]] = []
    for code in requested:
        try:
            record = dict(capture_one(code, asof))
            if record.get("code") != code:
                raise CandidateAdmissionCaptureError("provider record identity mismatch")
            records.append(record)
        except Exception as exc:  # noqa: BLE001 - close each provider failure as a blocker
            blockers.append({"code": code, "reason": f"{type(exc).__name__}: {exc}"})

    observed = observed_at or datetime.now(SHANGHAI).isoformat(timespec="seconds")
    observed_time = _timestamp(observed, "observed_at")
    if observed_time.astimezone(SHANGHAI).date() != asof_day:
        raise CandidateAdmissionCaptureError("capture must be observed on asof")
    for record in records:
        record["source"] = "baostock"
        record["observed_at"] = observed

    return seal_capture(
        {
            "schema_version": SCHEMA_VERSION,
            "asof": asof,
            "generated_at": observed,
            "source": "baostock",
            "source_receipt": {
                "provider": "baostock",
                "sdk_package_version": _sdk_version(),
                "requested_codes_sha256": _sha256(requested),
                "queries": list(_QUERIES),
                "query_contract": dict(_QUERY_CONTRACT),
            },
            "authority": dict(_AUTHORITY),
            "max_codes": MAX_CODES,
            "requested_codes": requested,
            "blockers": blockers,
            "records": records,
        }
    )


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateAdmissionCaptureError("capture has duplicate JSON keys")
        result[key] = value
    return result


def verify_capture(value: Mapping[str, object]) -> dict[str, object]:
    """Validate a decoded source capture without modifying it."""

    capture = dict(value)
    expected_hash = capture.pop("artifact_sha256", None)
    if not isinstance(expected_hash, str) or len(expected_hash) != 64 \
            or expected_hash != _sha256(capture):
        raise CandidateAdmissionCaptureError("capture identity mismatch")
    if capture.get("schema_version") != SCHEMA_VERSION \
            or capture.get("source") != "baostock" \
            or capture.get("max_codes") != MAX_CODES \
            or capture.get("authority") != _AUTHORITY:
        raise CandidateAdmissionCaptureError("capture manifest is invalid")
    asof = _day(capture.get("asof"), "asof")
    generated = _timestamp(capture.get("generated_at"), "generated_at")
    if generated.astimezone(SHANGHAI).date() != asof:
        raise CandidateAdmissionCaptureError("capture must be generated on asof")
    receipt = capture.get("source_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("provider") != "baostock" \
            or not isinstance(receipt.get("sdk_package_version"), str) \
            or not receipt.get("sdk_package_version") \
            or receipt.get("queries") != _QUERIES \
            or receipt.get("query_contract") != _QUERY_CONTRACT:
        raise CandidateAdmissionCaptureError("source receipt is invalid")
    requested = _codes(capture.get("requested_codes", []))  # type: ignore[arg-type]
    if receipt.get("requested_codes_sha256") != _sha256(requested):
        raise CandidateAdmissionCaptureError("source receipt scope is invalid")
    records = capture.get("records")
    blockers = capture.get("blockers")
    if not isinstance(records, list) or not isinstance(blockers, list):
        raise CandidateAdmissionCaptureError("capture coverage is invalid")
    record_codes: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise CandidateAdmissionCaptureError("capture record is invalid")
        code = record.get("code")
        observed = _timestamp(record.get("observed_at"), "record observed_at")
        if not isinstance(code, str) or record.get("source") != "baostock" \
                or observed != generated \
                or any(not isinstance(record.get(field), list) for field in (
                    "finance_rows", "industry_rows", "corporate_action_rows"
                )):
            raise CandidateAdmissionCaptureError("capture record is invalid")
        for row in record["finance_rows"]:
            methods = row.get("metric_methods") if isinstance(row, Mapping) else None
            if not isinstance(methods, Mapping) or set(methods) != set(_METRIC_METHODS) \
                    or any(methods.get(field) not in allowed
                           for field, allowed in _METRIC_METHODS.items()):
                raise CandidateAdmissionCaptureError(
                    "finance metric normalization is invalid"
                )
        if record["corporate_action_rows"] \
                or record.get("signal_price_history") is not None:
            _verify_signal_history(
                record.get("signal_price_history"), code, asof.isoformat(),
                knowledge_time=generated,
            )
        record_codes.append(code)
    blocker_codes: list[str] = []
    for blocker in blockers:
        if not isinstance(blocker, Mapping) \
                or not isinstance(blocker.get("code"), str) \
                or not isinstance(blocker.get("reason"), str) \
                or not blocker.get("reason"):
            raise CandidateAdmissionCaptureError("capture blocker is invalid")
        blocker_codes.append(str(blocker["code"]))
    covered = record_codes + blocker_codes
    if len(covered) != len(set(covered)) or set(covered) != set(requested):
        raise CandidateAdmissionCaptureError("capture coverage is invalid")
    return {**capture, "artifact_sha256": expected_hash}


def load_capture(path: str | Path) -> dict[str, object]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise CandidateAdmissionCaptureError("capture path must be a regular file")
    try:
        raw = candidate.read_bytes()
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateAdmissionCaptureError("capture is unreadable") from exc
    if not isinstance(value, Mapping) or raw != _canonical(value):
        raise CandidateAdmissionCaptureError("capture bytes are not canonical")
    return verify_capture(value)


def publish_capture(capture: Mapping[str, object], output_root: str | Path) -> Path:
    verified = verify_capture(capture)
    raw = _canonical(verified)
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{verified['asof']}-{verified['artifact_sha256']}.json"
    if target.exists():
        if target.read_bytes() != raw:
            raise CandidateAdmissionCaptureError("content-addressed capture conflicts")
        return target
    descriptor, temporary_name = tempfile.mkstemp(prefix=".admission-", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        temporary.unlink()
        parent = os.open(root, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


class BaoStockAdmissionSource:
    """One explicit BaoStock session for a bounded admission capture."""

    def __init__(self) -> None:
        self._bs: Any = None

    def __enter__(self) -> BaoStockAdmissionSource:  # noqa: PYI034
        import baostock as bs

        result = bs.login()
        if result.error_code != "0":
            raise CandidateAdmissionCaptureError(
                f"baostock login failed: {result.error_msg}"
            )
        self._bs = bs
        return self

    def __exit__(self, *_args: object) -> None:
        if self._bs is not None:
            self._bs.logout()
            self._bs = None

    @staticmethod
    def _rows(result: Any, label: str) -> list[dict[str, object]]:
        if result.error_code != "0":
            raise CandidateAdmissionCaptureError(f"{label} failed: {result.error_msg}")
        rows = []
        while result.next():
            rows.append(dict(zip(result.fields, result.get_row_data())))
        return rows

    def __call__(self, code: str, asof: str) -> Mapping[str, object]:
        if self._bs is None:
            raise CandidateAdmissionCaptureError("baostock session is not open")
        provider_code = f"{code[:2]}.{code[2:]}"
        asof_day = _day(asof, "asof")
        profit_cache: dict[tuple[int, int], list[dict[str, object]]] = {}

        def profits(year: int, quarter: int) -> list[dict[str, object]]:
            key = (year, quarter)
            if key not in profit_cache:
                profit_cache[key] = self._rows(
                    self._bs.query_profit_data(
                        code=provider_code, year=year, quarter=quarter
                    ),
                    f"profit {year}Q{quarter}",
                )
            return profit_cache[key]

        latest: tuple[int, int] | None = None
        for year in (asof_day.year, asof_day.year - 1):
            for quarter in range(4, 0, -1):
                if any(
                    isinstance(row.get("pubDate"), str)
                    and len(str(row["pubDate"])) == 10
                    and str(row["pubDate"]) < asof
                    for row in profits(year, quarter)
                ):
                    latest = (year, quarter)
                    break
            if latest is not None:
                break
        if latest is None:
            raise CandidateAdmissionCaptureError("no published finance period")

        year, quarter = latest
        needed = {
            (year, quarter),
            (year - 1, 4),
            (year - 1, quarter),
            (year - 2, 4),
            (year - 2, quarter),
        }
        finance_rows: list[dict[str, object]] = []
        for period_year, period_quarter in sorted(needed):
            cash_rows = self._rows(
                self._bs.query_cash_flow_data(
                    code=provider_code, year=period_year, quarter=period_quarter
                ),
                f"cash flow {period_year}Q{period_quarter}",
            )
            cash_by_date = {row.get("statDate"): row for row in cash_rows}
            for profit in profits(period_year, period_quarter):
                cash = cash_by_date.get(profit.get("statDate"), {})
                net_profit = _number(profit.get("netProfit"))
                revenue = _number(profit.get("MBRevenue"))
                margin = _number(profit.get("npMargin"))
                if revenue is None and net_profit is not None and margin not in (None, 0.0):
                    revenue = net_profit / margin
                cfo_to_np = _number(cash.get("CFOToNP"))
                roe = _number(profit.get("roeAvg"))
                finance_rows.append(
                    {
                        "stat_date": profit.get("statDate"),
                        "publish_date": profit.get("pubDate"),
                        "net_profit_ytd": net_profit,
                        "revenue_ytd": revenue,
                        "ocf_ytd": (
                            net_profit * cfo_to_np
                            if net_profit is not None and cfo_to_np is not None
                            else None
                        ),
                        "roe_ytd_pct": roe * 100.0 if roe is not None else None,
                        "metric_methods": {
                            "net_profit_ytd": "provider_netProfit",
                            "revenue_ytd": (
                                "provider_MBRevenue"
                                if _number(profit.get("MBRevenue")) is not None
                                else "derived_net_profit_div_np_margin"
                            ),
                            "ocf_ytd": "derived_net_profit_mul_CFOToNP",
                            "roe_ytd_pct": "provider_roeAvg_ratio_mul_100",
                        },
                        "profit_row": profit,
                        "cash_flow_row": cash,
                    }
                )

        industry_rows = [
            {
                "update_date": row.get("updateDate"),
                "code": code,
                "industry": row.get("industry"),
                "classification": row.get("industryClassification"),
                "raw": row,
            }
            for row in self._rows(
                self._bs.query_stock_industry(code=provider_code), "industry"
            )
        ]
        coverage_start = (asof_day - timedelta(days=90)).isoformat()
        action_rows = [
            {
                "event_date": row.get("dividOperateDate"),
                "fore_adjust_factor": row.get("foreAdjustFactor"),
                "back_adjust_factor": row.get("backAdjustFactor"),
                "adjust_factor": row.get("adjustFactor"),
                "raw": row,
            }
            for row in self._rows(
                self._bs.query_adjust_factor(
                    code=provider_code, start_date=coverage_start, end_date=asof
                ),
                "adjust factor",
            )
        ]
        record = {
            "code": code,
            "finance_rows": sorted(
                finance_rows, key=lambda row: str(row.get("stat_date"))
            ),
            "industry_rows": industry_rows,
            "corporate_action_rows": action_rows,
        }
        if action_rows:
            record["signal_price_history"] = _signal_history(code, asof)
        return record


__all__ = [
    "MAX_CODES",
    "SCHEMA_VERSION",
    "BaoStockAdmissionSource",
    "CandidateAdmissionCaptureError",
    "build_capture",
    "load_capture",
    "publish_capture",
    "seal_capture",
    "verify_capture",
]
