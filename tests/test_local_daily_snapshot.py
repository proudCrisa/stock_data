from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from stockdata import local_daily_snapshot as local
from stockdata.local_publisher import initialize_local_publisher


def _capture(source, symbol, start, asof, adjustment):
    days, day = [], date.fromisoformat(asof)
    while len(days) < 20:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    days.reverse()
    observed = datetime.now(timezone.utc).isoformat()
    if source == "baostock":
        fields = "date,open,high,low,close,volume"
        return {"source": source, "observed_at": observed,
                "request": {"method": "query_history_k_data_plus", "code": f"{symbol[-2:].lower()}.{symbol[:6]}",
                            "start_date": start, "end_date": asof, "frequency": "d", "fields": fields,
                            "adjustflag": "2" if adjustment == "qfq" else "3"},
                "response": {"fields": fields, "rows": [[day, "10", "11", "9", "10.5", "100"] for day in days]}}
    vendor = local.to_tencent(symbol).replace(".", "")
    body = {"code": 0, "data": {vendor: {"qfqday" if adjustment == "qfq" else "day":
            [[day, "10", "10.5", "11", "9", "1"] for day in days]}}}
    return {"source": source, "observed_at": observed,
            "request": {"code": symbol, "start_date": start, "end_date": asof, "adjustment_mode": adjustment},
            "response": {"pages": [{"observed_at": observed,
                "request": {"url": local.TENCENT_URL, "params": {"param": f"{vendor},day,{start},{asof},800,{'qfq' if adjustment == 'qfq' else ''}"}},
                "response": {"status_code": 200, "raw": json.dumps(body)}}]}}


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    anchor = initialize_local_publisher(tmp_path / "publisher")
    monkeypatch.setattr(local, "_capture", _capture)
    result = local.capture_local_daily_snapshot(symbols=["561980.SH", "000300.SH"], asof="2026-09-04",
        publisher_dir=tmp_path / "publisher", expected_registry_sha256=anchor["registry_sha256"],
        output_dir=tmp_path / "prices")
    return json.loads((tmp_path / "prices" / "snapshot.json").read_text()), anchor, result


def _verify(value, anchor):
    return local.verify_local_daily_snapshot(value, expected_registry_sha256=anchor["registry_sha256"],
        expected_symbols=["561980.SH", "000300.SH"], asof="2026-09-04", decision_cutoff=value["decision_cutoff"])


def test_signed_local_prices_replay_without_io(snapshot, monkeypatch):
    value, anchor, _ = snapshot
    monkeypatch.setattr("pathlib.Path.read_bytes", lambda *args: pytest.fail("filesystem read during replay"))
    assert _verify(value, anchor) == value
    records = value["artifact"]["records"]
    assert records[0]["payload"]["signal"]["products"][0]["price_identity"]["source"] == "baostock"
    etf = records[1]["payload"]
    assert etf["execution"]["products"][0]["price_identity"]["adjustment_mode"] == "raw"
    assert etf["signal"]["products"][0]["price_identity"]["adjustment_mode"] == "qfq"
    assert etf["signal"]["products"][0]["rows"][0]["volume"] == 100


@pytest.mark.parametrize("field", ["rows", "raw", "symbol", "signature", "pin"])
def test_snapshot_rejects_tampering_even_with_rehashed_outer_content(snapshot, field):
    value, anchor, _ = snapshot
    value = deepcopy(value)
    product = value["artifact"]["records"][1]["payload"]["signal"]["products"][0]
    if field == "rows":
        product["rows"][0]["close"] = 2
    elif field == "raw":
        product["source_receipts"][0]["response"]["pages"][0]["response"]["raw"] = "{}"
    elif field == "symbol":
        value["symbols"] = ["588730.SH"]
    elif field == "signature":
        value["authority_envelope"]["signature_base64"] = "A" * 88
    else:
        anchor = {**anchor, "registry_sha256": "f" * 64}
    value["snapshot_sha256"] = local._hash({k: v for k, v in value.items() if k != "snapshot_sha256"})
    with pytest.raises(ValueError):
        _verify(value, anchor)


def test_eligible_primary_cannot_be_displaced_by_fallback():
    start, asof = "2025-07-11", "2026-09-04"
    attempts = [{"source": source, "capture": _capture(source, "561980.SH", start, asof, "qfq"),
                 "observed_at": datetime.now(timezone.utc).isoformat(), "error": None}
                for source in ("tencent.ifzq", "baostock")]
    with pytest.raises(ValueError, match="higher-priority"):
        local.build_price_manifest(attempts, symbol="561980.SH", role="signal", start=start, asof=asof,
                                    decision_cutoff=datetime.now(timezone.utc).isoformat())
