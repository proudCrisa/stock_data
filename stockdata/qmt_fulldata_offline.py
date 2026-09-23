"""Offline replay of saved QMT full-data daily-bar evidence.

This module never opens a network connection.  Production volume-unit evidence
is limited to six sealed M1 request/response pairs and is not a general QMT
daily-bar unit declaration.
"""
from __future__ import annotations

import hashlib
import json
import math

from .daily_bar_product import _hash, _validate_bar
from .liquidity_amount_product import _timestamp


SOURCE = "qmt.fulldata"
REQUEST_TYPE = "history_kline"
RESPONSE_STATUS = "ok"
VOLUME_EVIDENCE_SCHEMA = "stockdata-qmt-fulldata-volume-unit-evidence/1"
PRODUCTION_VOLUME_EVIDENCE_ID = "approved-m1-qmt-fulldata-volume-hand/1"
TEST_ONLY_VOLUME_EVIDENCE_ID = "test-only-qmt-fulldata-volume-hand/1"
_MODES = {"raw": "none", "qfq": "front"}
_REQUEST_PARAMS = {"symbol", "period", "start", "end", "count", "dividend_type"}
_RESPONSE_FIELDS = {"data", "dividend_type", "id", "status", "type"}
_BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_CROSSPROOF = {
    "summary_sha256": "3a678e2a80e1eee60cfdcf18f8988a945ed587518be50317549a7d178310828a",
    "comparison_sha256": "99b3c17e206bdd3e2bf75cce591aed4a89ea965d47ed16d5635897b306eeaaae",
}
APPROVED_VOLUME_PAIRS = {
    ("511010.SH", "raw"): ("4f2b1fd3ac6bac8c48c80fce4d86aa4f7d33edbb2f4f63425116b886b6164028", "ed2136b249e72ebaced6a32a4881d0e46c516cd00333b70b4ca62cac354ab763", "2026-09-11T18:11:37.423604+08:00"),
    ("511010.SH", "qfq"): ("3c13bbae66928ef3d2888817242d6ca49b2977e6dee4858fc919066b8a3f7344", "ce805541aa986edd2256f105039c995675dead27a6ba78a48d6d5c9738e8f065", "2026-09-11T18:13:35.195796+08:00"),
    ("518880.SH", "raw"): ("4810880e47f09de6da119e7ae31dbe93787bf55e2c7fa4a455486eac94e81a59", "f6a4c9eaec54d076debde625469fa6d82ead95776ef66047ee6b9e2d528a98fc", "2026-09-11T18:22:30+08:00"),
    ("518880.SH", "qfq"): ("1d0b34a71ddca557cd395fee3cf22328265af6c23173879a18202466f36cd967", "eb79a7ae08ff0486228cd123e4f0367854a73cc7355c558b939cb0382c401052", "2026-09-11T18:17:33.283953+08:00"),
    ("561980.SH", "raw"): ("0da2750a822460042a60c4892194f68ad628473129ad66fc86207d77729486f3", "890ec165793d2e95169d520e49a508b75ae44b304600cabf997700be662ba428", "2026-09-11T18:19:39.394620+08:00"),
    ("561980.SH", "qfq"): ("dbc6ee2ce9daa937490d6127254192826f1a2dcdc42d4facdaedb9474f994fe7", "365d8c1359b4c812614b8623fa71c1833b2000d5153dc0d6b7cf69c5466706f1", "2026-09-11T18:21:38.818907+08:00"),
}


def build_capture(*, request, response, observed_at, volume_unit_evidence,
                  request_raw=None, response_raw=None):
    """Keep the original canonical wire objects with the external observation time."""
    return {"source": SOURCE, "observed_at": observed_at, "request": request,
            "response": response, "volume_unit_evidence": volume_unit_evidence,
            "request_raw": request_raw, "response_raw": response_raw}


def _verify_volume_evidence(evidence, *, request, response, request_raw,
                            response_raw, observed_at, symbol, adjustment,
                            allow_test_volume_evidence):
    base = {"schema_version", "evidence_id", "volume_unit", "request_sha256",
            "response_sha256", "evidence_sha256"}
    expected = base | set(_CROSSPROOF) if isinstance(evidence, dict) \
        and evidence.get("evidence_id") == PRODUCTION_VOLUME_EVIDENCE_ID else base
    if not isinstance(evidence, dict) or set(evidence) != expected \
            or evidence.get("schema_version") != VOLUME_EVIDENCE_SCHEMA \
            or evidence.get("volume_unit") != "hand" \
            or evidence.get("request_sha256") != _hash(request) \
            or evidence.get("response_sha256") != _hash(response):
        raise ValueError("QMT full-data volume-unit evidence is invalid")
    body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    if evidence.get("evidence_sha256") != _hash(body):
        raise ValueError("QMT full-data volume-unit evidence hash differs")
    identifier = evidence["evidence_id"]
    if allow_test_volume_evidence and identifier == TEST_ONLY_VOLUME_EVIDENCE_ID:
        return
    if identifier != PRODUCTION_VOLUME_EVIDENCE_ID or not isinstance(request_raw, str) \
            or not isinstance(response_raw, str) or (symbol, adjustment) not in APPROVED_VOLUME_PAIRS:
        raise ValueError("QMT full-data volume unit lacks approved source evidence")
    if any(evidence.get(key) != value for key, value in _CROSSPROOF.items()):
        raise ValueError("QMT full-data volume crossproof differs")
    request_digest, response_digest, completed_at = APPROVED_VOLUME_PAIRS[(symbol, adjustment)]
    if _timestamp(observed_at) != _timestamp(completed_at):
        raise ValueError("QMT full-data observation differs approved completion")
    try:
        if json.loads(request_raw) != request or json.loads(response_raw) != response:
            raise ValueError("QMT full-data approved bytes differ structured capture")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("QMT full-data approved bytes are invalid") from exc
    if hashlib.sha256(request_raw.encode("utf-8")).hexdigest() != request_digest \
            or hashlib.sha256(response_raw.encode("utf-8")).hexdigest() != response_digest:
        raise ValueError("QMT full-data approved pair bytes differ")


def replay(capture, *, symbol, start, asof, adjustment, cutoff,
           allow_test_volume_evidence=False):
    """Validate saved wire evidence and return exact rows without filling gaps."""
    if not isinstance(capture, dict) or set(capture) != {
            "source", "observed_at", "request", "response", "volume_unit_evidence",
            "request_raw", "response_raw"} \
            or capture.get("source") != SOURCE:
        raise ValueError("QMT full-data capture identity differs")
    observed = _timestamp(capture["observed_at"])
    if observed <= _timestamp(f"{asof}T15:00:00+08:00") or observed >= cutoff:
        raise ValueError("QMT full-data observation is outside final pre-cutoff window")
    request, response = capture["request"], capture["response"]
    if not isinstance(request, dict) or set(request) != {"params", "request_id", "type"} \
            or request.get("type") != REQUEST_TYPE or not isinstance(request.get("request_id"), str) \
            or not request["request_id"]:
        raise ValueError("QMT full-data request schema differs")
    params = request.get("params")
    expected_dividend = _MODES.get(adjustment)
    if not isinstance(params, dict) or set(params) != _REQUEST_PARAMS \
            or params.get("symbol") != symbol or params.get("period") != "1d" \
            or params.get("dividend_type") != expected_dividend \
            or not isinstance(params.get("count"), int) or isinstance(params["count"], bool) \
            or params["count"] <= 0 or any(not isinstance(params[key], str) for key in ("start", "end")):
        raise ValueError("QMT full-data request binding differs")
    if (params["start"] and params["start"] != start.replace("-", "")) \
            or (params["end"] and params["end"] != asof.replace("-", "")):
        raise ValueError("QMT full-data request range differs")
    if not isinstance(response, dict) or set(response) != _RESPONSE_FIELDS \
            or response.get("id") != request["request_id"] \
            or response.get("type") != REQUEST_TYPE or response.get("status") != RESPONSE_STATUS \
            or response.get("dividend_type") != expected_dividend:
        raise ValueError("QMT full-data response binding differs")
    _verify_volume_evidence(capture["volume_unit_evidence"], request=request, response=response,
                            request_raw=capture["request_raw"], response_raw=capture["response_raw"],
                            observed_at=capture["observed_at"],
                            symbol=symbol, adjustment=adjustment,
                            allow_test_volume_evidence=allow_test_volume_evidence)
    data = response.get("data")
    if not isinstance(data, dict) or set(data) != {symbol}:
        raise ValueError("QMT full-data response symbol closure differs")
    record = data[symbol]
    if not isinstance(record, dict) or set(record) != {"columns", "dividend_type", "index"} \
            or record.get("dividend_type") != expected_dividend:
        raise ValueError("QMT full-data response record differs")
    index, columns = record.get("index"), record.get("columns")
    if not isinstance(index, list) or not index or not isinstance(columns, dict) \
            or set(columns) != set(_BAR_FIELDS) or len(index) > params["count"]:
        raise ValueError("QMT full-data response rows are malformed")
    if any(not isinstance(day, str) or len(day) != 8 or not day.isdigit() for day in index) \
            or index != sorted(set(index)):
        raise ValueError("QMT full-data response dates are invalid")
    if any(not isinstance(columns[field], list) or len(columns[field]) != len(index)
           for field in _BAR_FIELDS):
        raise ValueError("QMT full-data response columns are malformed")
    rows = []
    for offset, day in enumerate(index):
        row = {"date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
               **{field: columns[field][offset] for field in _BAR_FIELDS}}
        try:
            row = {key: value if key == "date" else float(value) for key, value in row.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError("QMT full-data row is nonnumeric") from exc
        if any(not math.isfinite(row[field]) for field in _BAR_FIELDS):
            raise ValueError("QMT full-data row is nonfinite")
        _validate_bar(row)
        rows.append(row)
    selected = [row for row in rows if start <= row["date"] <= asof]
    if len(selected) < 20 or selected[-1]["date"] != asof:
        raise ValueError("QMT full-data lacks history or exact final watermark")
    return [{**row, "volume": row["volume"] * 100.0} for row in selected], adjustment
