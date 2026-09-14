from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from stockdata import local_daily_snapshot as local
from stockdata import local_publisher
from stockdata import qmt_fulldata_offline as qmt
from stockdata.daily_bar_product import _hash
from stockdata.local_publisher import initialize_local_publisher


ASOF = "2026-09-11"
START = "2026-08-01"
OBSERVED = "2026-09-11T15:01:00+08:00"
CUTOFF = "2026-09-11T15:02:00+08:00"
SYMBOL = "561980.SH"


def _days():
    value, result = date.fromisoformat(ASOF), []
    while len(result) < 20:
        if value.weekday() < 5:
            result.append(value.strftime("%Y%m%d"))
        value -= timedelta(days=1)
    return list(reversed(result))


def _capture(adjustment="raw", *, symbol=SYMBOL, observed_at=OBSERVED,
             test_evidence=True):
    dividend = {"raw": "none", "qfq": "front"}[adjustment]
    request = {"type": "history_kline", "request_id": f"test-{adjustment}",
               "params": {"symbol": symbol, "period": "1d", "start": "",
                          "end": "", "count": 21, "dividend_type": dividend}}
    days = _days()
    response = {"id": request["request_id"], "status": "ok", "type": "history_kline",
                "dividend_type": dividend, "data": {symbol: {"dividend_type": dividend,
                "index": days, "columns": {"open": [10.0] * len(days),
                "high": [11.0] * len(days), "low": [9.0] * len(days),
                "close": [10.5] * len(days), "volume": [100.0] * len(days),
                "amount": [105000.0] * len(days)}}}}
    evidence = {"schema_version": qmt.VOLUME_EVIDENCE_SCHEMA,
                "evidence_id": qmt.TEST_ONLY_VOLUME_EVIDENCE_ID if test_evidence else "unapproved",
                "volume_unit": "hand", "request_sha256": _hash(request),
                "response_sha256": _hash(response)}
    evidence["evidence_sha256"] = _hash(evidence)
    return qmt.build_capture(request=request, response=response, observed_at=observed_at,
                             volume_unit_evidence=evidence)


def _approve(monkeypatch, capture, adjustment, *, symbol=SYMBOL):
    request_raw = json.dumps(capture["request"], separators=(",", ":"))
    response_raw = json.dumps(capture["response"], separators=(",", ":"))
    capture["request_raw"], capture["response_raw"] = request_raw, response_raw
    monkeypatch.setitem(qmt.APPROVED_VOLUME_PAIRS, (symbol, adjustment),
                        (__import__("hashlib").sha256(request_raw.encode()).hexdigest(),
                         __import__("hashlib").sha256(response_raw.encode()).hexdigest(),
                         capture["observed_at"]))
    evidence = capture["volume_unit_evidence"]
    evidence.update(evidence_id=qmt.PRODUCTION_VOLUME_EVIDENCE_ID, **qmt._CROSSPROOF)
    evidence["evidence_sha256"] = _hash({key: value for key, value in evidence.items()
                                          if key != "evidence_sha256"})


def _old_capture(source, symbol, start, asof, adjustment):
    days = [f"{day[:4]}-{day[4:6]}-{day[6:]}" for day in _days()]
    fields = "date,open,high,low,close,volume"
    return {"source": source, "observed_at": OBSERVED,
            "request": {"method": "query_history_k_data_plus",
                        "code": f"{symbol[-2:].lower()}.{symbol[:6]}",
                        "start_date": start, "end_date": asof, "frequency": "d",
                        "fields": fields, "adjustflag": "3" if adjustment == "raw" else "2"},
            "response": {"fields": fields,
                         "rows": [[day, "10", "11", "9", "10.5", "100"] for day in days]}}


@pytest.mark.parametrize("adjustment", ["raw", "qfq"])
def test_replays_actual_qmt_fulldata_wire_shape_with_test_only_unit_evidence(adjustment):
    capture = _capture(adjustment)
    rows, mode = qmt.replay(capture, symbol=SYMBOL, start=START,
                            asof=ASOF, adjustment=adjustment,
                            cutoff=local._timestamp(CUTOFF),
                            allow_test_volume_evidence=True)
    assert mode == adjustment
    assert len(rows) == 20 and rows[-1]["date"] == ASOF
    assert rows[0]["volume"] == 10_000.0
    again, _ = qmt.replay(capture, symbol=SYMBOL, start=START, asof=ASOF,
                          adjustment=adjustment, cutoff=local._timestamp(CUTOFF),
                          allow_test_volume_evidence=True)
    wire = capture["response"]["data"][SYMBOL]["columns"]
    assert again == rows
    assert rows[0]["open"] == wire["open"][0]
    assert rows[0]["close"] == wire["close"][0]
    assert rows[0]["amount"] == wire["amount"][0]


@pytest.mark.parametrize("mutation", ["id", "dividend", "unit", "time"])
def test_qmt_fulldata_fails_closed_on_unbound_identity_or_time(mutation):
    capture = _capture()
    if mutation == "id":
        capture["response"]["id"] = "other"
    elif mutation == "dividend":
        capture["response"]["dividend_type"] = "front"
    elif mutation == "unit":
        capture["volume_unit_evidence"]["response_sha256"] = "0" * 64
    else:
        capture["observed_at"] = "2026-09-11T15:00:00+08:00"
    with pytest.raises(ValueError):
        qmt.replay(capture, symbol=SYMBOL, start=START, asof=ASOF, adjustment="raw",
                   cutoff=local._timestamp(CUTOFF), allow_test_volume_evidence=True)


def test_local_price_manifest_rejects_real_shape_without_approved_volume_evidence():
    capture = _capture(test_evidence=False)
    attempts = [{"source": qmt.SOURCE, "observed_at": OBSERVED,
                 "capture": capture, "error": None}]
    with pytest.raises(ValueError, match="selected price source cannot replay"):
        local.build_price_manifest(
            attempts, symbol=SYMBOL, role="execution", start=START, asof=ASOF,
            decision_cutoff=CUTOFF)


def test_approved_raw_bytes_cannot_be_paired_with_replaced_structured_response(monkeypatch):
    capture = _capture(test_evidence=False)
    _approve(monkeypatch, capture, "raw")
    evidence = capture["volume_unit_evidence"]
    capture["response"]["data"][SYMBOL]["columns"]["close"][0] = 10.6
    evidence["response_sha256"] = _hash(capture["response"])
    evidence["evidence_sha256"] = _hash({key: value for key, value in evidence.items()
                                          if key != "evidence_sha256"})
    with pytest.raises(ValueError, match="approved bytes"):
        qmt.replay(capture, symbol=SYMBOL, start=START, asof=ASOF, adjustment="raw",
                   cutoff=local._timestamp(CUTOFF))


@pytest.mark.parametrize("adjustment", ["raw", "qfq"])
def test_production_evidence_builds_qmt_price_manifest(monkeypatch, adjustment):
    capture = _capture(adjustment, test_evidence=False)
    _approve(monkeypatch, capture, adjustment)
    manifest = local.build_price_manifest(
        [{"source": qmt.SOURCE, "observed_at": OBSERVED, "capture": capture, "error": None}],
        symbol=SYMBOL, role="signal" if adjustment == "qfq" else "execution",
        start=START, asof=ASOF, decision_cutoff=CUTOFF)
    product = manifest["products"][0]
    assert product["price_identity"] == {
        "source": qmt.SOURCE, "adjustment_mode": adjustment,
        "adjustment_version": f"{qmt.SOURCE}-{adjustment}", "volume_unit": "share"}
    assert product["rows"][-1]["date"] == ASOF
    assert product["rows"][0]["volume"] == 10_000.0


def test_sealed_qmt_panel_signs_and_replays_through_local_snapshot(tmp_path, monkeypatch):
    symbols = sorted({symbol for symbol, _ in qmt.APPROVED_VOLUME_PAIRS})
    sealed = {}
    for symbol in symbols:
        for role, adjustment in (("execution", "raw"), ("signal", "qfq")):
            capture = _capture(adjustment, symbol=symbol, test_evidence=False)
            _approve(monkeypatch, capture, adjustment, symbol=symbol)
            sealed[(symbol, role)] = capture

    class Clock:
        calls = 0

        @staticmethod
        def now(_tz=None):
            Clock.calls += 1
            return datetime(2026, 9, 11, 20, tzinfo=timezone.utc) + timedelta(
                seconds=Clock.calls)

    monkeypatch.setattr(local_publisher, "datetime", Clock)
    anchor = initialize_local_publisher(tmp_path / "publisher")
    monkeypatch.setattr(local, "datetime", Clock)
    result = local.capture_local_daily_snapshot(
        symbols=symbols, asof=ASOF, publisher_dir=tmp_path / "publisher",
        expected_registry_sha256=anchor["registry_sha256"], output_dir=tmp_path / "prices",
        sealed_qmt_captures=sealed)
    snapshot = json.loads((tmp_path / "prices" / "snapshot.json").read_text())
    assert local.verify_local_daily_snapshot(
        snapshot, expected_registry_sha256=anchor["registry_sha256"],
        expected_symbols=symbols, asof=ASOF,
        decision_cutoff=result["decision_cutoff"]) == snapshot


def test_sealed_qmt_captures_mix_with_existing_profile_sources(tmp_path, monkeypatch):
    qmt_symbols = sorted({symbol for symbol, _ in qmt.APPROVED_VOLUME_PAIRS})
    symbols = ["000300.SH", *qmt_symbols]
    sealed = {}
    for symbol in qmt_symbols:
        for role, adjustment in (("execution", "raw"), ("signal", "qfq")):
            capture = _capture(adjustment, symbol=symbol, test_evidence=False)
            _approve(monkeypatch, capture, adjustment, symbol=symbol)
            sealed[(symbol, role)] = capture
    calls = []

    def capture_old(source, symbol, start, asof, adjustment):
        calls.append((source, symbol, adjustment))
        return _old_capture(source, symbol, start, asof, adjustment)

    class Clock:
        calls = 0

        @staticmethod
        def now(_tz=None):
            Clock.calls += 1
            return datetime(2026, 9, 11, 20, tzinfo=timezone.utc) + timedelta(
                seconds=Clock.calls)

    monkeypatch.setattr(local_publisher, "datetime", Clock)
    anchor = initialize_local_publisher(tmp_path / "publisher")
    monkeypatch.setattr(local, "_capture", capture_old)
    monkeypatch.setattr(local, "datetime", Clock)
    result = local.capture_local_daily_snapshot(
        symbols=symbols, asof=ASOF, publisher_dir=tmp_path / "publisher",
        expected_registry_sha256=anchor["registry_sha256"], output_dir=tmp_path / "prices",
        sealed_qmt_captures=sealed)
    snapshot = json.loads((tmp_path / "prices" / "snapshot.json").read_text())
    assert calls == [("baostock", "000300.SH", "raw")]
    assert local.verify_local_daily_snapshot(
        snapshot, expected_registry_sha256=anchor["registry_sha256"],
        expected_symbols=symbols, asof=ASOF,
        decision_cutoff=result["decision_cutoff"]) == snapshot


@pytest.mark.parametrize("sealed", [{}, {(SYMBOL, "execution"): _capture()}])
def test_sealed_qmt_panel_requires_exact_six_roles(tmp_path, sealed):
    anchor = initialize_local_publisher(tmp_path / "publisher")
    with pytest.raises(ValueError, match="exact approved six-role"):
        local.capture_local_daily_snapshot(
            symbols=["511010.SH", "518880.SH", "561980.SH"], asof=ASOF,
            publisher_dir=tmp_path / "publisher", expected_registry_sha256=anchor["registry_sha256"],
            output_dir=tmp_path / "prices", sealed_qmt_captures=sealed)


@pytest.mark.parametrize("symbol,role,attempts", [
    ("000300.SH", "execution", [{"source": qmt.SOURCE}]),
    (SYMBOL, "other", [{"source": qmt.SOURCE}]),
    (SYMBOL, "execution", [{"source": qmt.SOURCE}, {"source": "baostock"}]),
])
def test_qmt_manifest_route_rejects_foreign_wrong_role_and_mixed_attempts(
        symbol, role, attempts):
    with pytest.raises(ValueError, match="sealed profile"):
        local._manifest_route(attempts, symbol, role, frozenset())
