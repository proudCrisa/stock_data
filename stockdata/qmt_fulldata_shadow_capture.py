"""Bounded shadow capture for one QMT ``/fulldata`` history-kline response.

This module is deliberately capture-only.  Its artifacts are unverified,
have an unknown volume unit, and cannot be used for decisions or replay.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .qmt_transport_capture import (
    QmtTransportCaptureError, _NoRedirect, _canonical, _loopback_base_url,
    _write_content_addressed,
)


SCHEMA_VERSION = "qmt-fulldata-shadow-capture/1"
SOURCE = "qmt.fulldata"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_COUNT = 1300
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MODES = {"raw": "none", "qfq": "front"}
_RESPONSE_FIELDS = {"data", "dividend_type", "id", "status", "type"}
_BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")


class QmtFulldataShadowCaptureError(RuntimeError):
    """The QMT response cannot be preserved as bounded shadow evidence."""


class QmtFulldataShadowCaptureTimeout(QmtFulldataShadowCaptureError):
    """The submitted QMT fulldata capture was still pending at the deadline."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QmtFulldataShadowCaptureError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise QmtFulldataShadowCaptureError(f"non-finite JSON number: {value}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QmtFulldataShadowCaptureError(f"{field} must be timezone aware")
    return value.astimezone(timezone.utc).isoformat()


def _date(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise QmtFulldataShadowCaptureError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise QmtFulldataShadowCaptureError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise QmtFulldataShadowCaptureError(f"{field} must be an ISO date")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QmtFulldataShadowCaptureError(f"{field} must be finite numeric data")
    number = float(value)
    if not math.isfinite(number):
        raise QmtFulldataShadowCaptureError(f"{field} must be finite numeric data")
    return number


def build_qmt_fulldata_request(*, symbol: str, start: str, end: str, count: int,
                               adjustment: str, request_id: str | None = None) -> dict[str, object]:
    """Build the sole supported single-symbol QMT fulldata request."""
    symbol = str(symbol).upper()
    if not _SYMBOL.fullmatch(symbol):
        raise QmtFulldataShadowCaptureError("symbol must use 000000.SH/SZ/BJ format")
    start, end = _date(start, "start"), _date(end, "end")
    if start > end:
        raise QmtFulldataShadowCaptureError("start must not follow end")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise QmtFulldataShadowCaptureError(f"count must be an integer in 1..{MAX_COUNT}")
    if adjustment not in _MODES:
        raise QmtFulldataShadowCaptureError("adjustment must be raw or qfq")
    identifier = request_id or str(uuid.uuid4())
    if not isinstance(identifier, str) or not identifier:
        raise QmtFulldataShadowCaptureError("request_id must be non-empty")
    return {"type": "history_kline", "request_id": identifier,
            "params": {"symbol": symbol, "period": "1d", "start": start,
                       "end": end, "count": count,
                       "dividend_type": _MODES[adjustment]}}


def _json(raw: bytes, field: str) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_keys,
                           parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QmtFulldataShadowCaptureError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise QmtFulldataShadowCaptureError(f"{field} root must be an object")
    return value


def _bound_response(response: object, request: dict[str, object]) -> dict[str, object]:
    params = request["params"]
    assert isinstance(params, dict)
    if not isinstance(response, dict) or set(response) != _RESPONSE_FIELDS \
            or response.get("id") != request["request_id"] \
            or response.get("status") != "ok" \
            or response.get("type") != "history_kline" \
            or response.get("dividend_type") != params["dividend_type"]:
        raise QmtFulldataShadowCaptureError("QMT terminal response binding is invalid")
    data = response.get("data")
    symbol = params["symbol"]
    if not isinstance(data, dict) or set(data) != {symbol}:
        raise QmtFulldataShadowCaptureError("QMT terminal response symbol closure differs")
    record = data[symbol]
    if not isinstance(record, dict) or set(record) != {"columns", "dividend_type", "index"} \
            or record.get("dividend_type") != params["dividend_type"]:
        raise QmtFulldataShadowCaptureError("QMT terminal response record differs")
    index, columns = record.get("index"), record.get("columns")
    if not isinstance(index, list) or not index or len(index) > params["count"] \
            or not isinstance(columns, dict) or set(columns) != set(_BAR_FIELDS):
        raise QmtFulldataShadowCaptureError("QMT terminal response rows are malformed")
    if any(not isinstance(day, str) or len(day) != 8 or not day.isdigit() for day in index) \
            or index != sorted(set(index)):
        raise QmtFulldataShadowCaptureError("QMT terminal response dates are invalid")
    try:
        dates = [date(int(day[:4]), int(day[4:6]), int(day[6:])).isoformat() for day in index]
    except ValueError as exc:
        raise QmtFulldataShadowCaptureError("QMT terminal response dates are invalid") from exc
    if dates[0] < params["start"] or dates[-1] > params["end"]:
        raise QmtFulldataShadowCaptureError("QMT terminal response dates exceed request range")
    if any(not isinstance(columns[field], list) or len(columns[field]) != len(index)
           for field in _BAR_FIELDS):
        raise QmtFulldataShadowCaptureError("QMT terminal response columns are malformed")
    for offset, day in enumerate(index):
        values = {field: _number(columns[field][offset], f"{day}.{field}")
                  for field in _BAR_FIELDS}
        if any(values[field] <= 0 for field in ("open", "high", "low", "close")) \
                or values["volume"] < 0 or values["amount"] < 0 \
                or values["high"] < max(values["open"], values["close"]) \
                or values["low"] > min(values["open"], values["close"]) \
                or values["low"] > values["high"]:
            raise QmtFulldataShadowCaptureError("QMT terminal response OHLCVA is invalid")
    return response


class QmtFulldataShadowCaptureClient:
    """Submit and poll exactly one QMT fulldata request on a loopback endpoint."""

    def __init__(self, *, token: str | None = None,
                 base_url: str = "http://127.0.0.1:8000", request_timeout: float = 3.0):
        token = token if token is not None else os.environ.get("QMT_TOKEN")
        if not isinstance(token, str) or not token:
            raise QmtFulldataShadowCaptureError("QMT_TOKEN is required")
        if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) \
                or not math.isfinite(float(request_timeout)) or not 0 < float(request_timeout) <= 30:
            raise QmtFulldataShadowCaptureError("request timeout is invalid")
        try:
            self._base_url = _loopback_base_url(base_url)
        except QmtTransportCaptureError as exc:
            raise QmtFulldataShadowCaptureError(str(exc)) from exc
        self._token = token
        self._request_timeout = float(request_timeout)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        )

    def _request(self, path: str, *, method: str, body: bytes | None = None) -> tuple[int, bytes]:
        request = urllib.request.Request(self._base_url + path, data=body, method=method,
                                         headers={"Accept": "application/json", "X-Token": self._token})
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self._request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return 404, b""
            raise QmtFulldataShadowCaptureError(f"QMT HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise QmtFulldataShadowCaptureError("QMT loopback channel is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QmtFulldataShadowCaptureError("QMT response exceeds the memory limit")
        if status != 200:
            raise QmtFulldataShadowCaptureError(f"QMT HTTP {status}")
        return status, raw

    def capture(self, *, symbol: str, start: str, end: str, count: int,
                adjustment: str, wait_timeout: float = 90.0, poll_interval: float = 1.0) -> dict[str, object]:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(float(value)) for value in (wait_timeout, poll_interval)) \
                or not 0 < float(wait_timeout) <= 300 or not 0 < float(poll_interval) <= 30:
            raise QmtFulldataShadowCaptureError("poll limits are invalid")
        request = build_qmt_fulldata_request(symbol=symbol, start=start, end=end,
                                             count=count, adjustment=adjustment)
        submit_raw = _canonical(request)
        submitted_at = _now()
        submitted_iso = _iso(submitted_at, "submitted_at")
        _, ack_raw = self._request("/fulldata", method="POST", body=submit_raw)
        ack = _json(ack_raw, "QMT acknowledgement")
        identifier = ack.get("id")
        if not isinstance(identifier, str) or not _SAFE_ID.fullmatch(identifier):
            raise QmtFulldataShadowCaptureError("QMT acknowledgement id is unsafe")
        deadline = time.monotonic() + float(wait_timeout)
        path = "/fulldata/" + urllib.parse.quote(identifier, safe="")
        while True:
            status, terminal_raw = self._request(path, method="GET")
            if status == 200:
                received_at = _now()
                received_iso = _iso(received_at, "received_at")
                if received_at < submitted_at:
                    raise QmtFulldataShadowCaptureError("QMT capture timestamps are unordered")
                response = _bound_response(_json(terminal_raw, "QMT terminal response"), request)
                unsigned = {
                    "schema_version": SCHEMA_VERSION, "authority_grade": "shadow",
                    "decision_eligible": False, "decision_authority": False, "actions": [],
                    "source": SOURCE, "finality": "unverified", "volume_unit": "unknown",
                    "submitted_at": submitted_iso, "received_at": received_iso,
                    "submit": {"request_raw_base64": base64.b64encode(submit_raw).decode("ascii"),
                               "request_sha256": _sha256(submit_raw),
                               "ack_raw_base64": base64.b64encode(ack_raw).decode("ascii"),
                               "ack_sha256": _sha256(ack_raw), "ack": ack},
                    "derived_bound_request": request,
                    "terminal": {"response_raw_base64": base64.b64encode(terminal_raw).decode("ascii"),
                                 "response_sha256": _sha256(terminal_raw), "response": response},
                }
                return {**unsigned, "capture_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest()}
            if time.monotonic() >= deadline:
                raise QmtFulldataShadowCaptureTimeout("QMT fulldata capture remained pending")
            time.sleep(float(poll_interval))


def write_qmt_fulldata_shadow_capture(output_root: str | Path,
                                      capture: dict[str, object]) -> Path:
    """Write a content-addressed shadow-only artifact under a safe output root."""
    if not isinstance(capture, dict) or not isinstance(capture.get("capture_sha256"), str):
        raise QmtFulldataShadowCaptureError("QMT shadow capture is malformed")
    unsigned = {key: value for key, value in capture.items() if key != "capture_sha256"}
    digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    if capture["capture_sha256"] != digest:
        raise QmtFulldataShadowCaptureError("QMT shadow capture digest differs")
    return _write_content_addressed(output_root, digest, _canonical(capture),
                                    QmtFulldataShadowCaptureError)


__all__ = ["MAX_COUNT", "MAX_RESPONSE_BYTES", "QmtFulldataShadowCaptureClient",
           "QmtFulldataShadowCaptureError", "QmtFulldataShadowCaptureTimeout",
           "build_qmt_fulldata_request", "write_qmt_fulldata_shadow_capture"]
