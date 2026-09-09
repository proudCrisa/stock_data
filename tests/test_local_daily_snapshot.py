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


def _candidate_profile(*, symbol="512480.SH", asof="2026-09-04"):
    code = f"{symbol[-2:].lower()}{symbol[:6]}"
    body = {
        "schema_version": local.DYNAMIC_PROFILE_SCHEMA,
        "asof": asof,
        "decision_authority": False,
        "purpose": "formal-validation-input",
        "required_symbols": ["000300.SH", symbol],
        "benchmark_symbols": ["000300.SH"],
        "candidates": [{
            "code": code, "symbol": symbol, "instrument_class": "etf",
            "promoted_asof": asof,
            "observation_contract": local.OBSERVATION_CONTRACT,
            "promotion_rule_version": local.PROMOTION_RULE_VERSION,
            "promotion_evidence_id": "promotion_" + "1" * 64,
            "promotion_content_hash": "1" * 64,
        }],
        "source_authorization": {
            "producer": "trading-agent", "code_revision": "2" * 40,
            "candidate_state_sha256": "3" * 64,
            "scan_asof": asof, "scan_sha256": "4" * 64,
            "universe_sha256": "5" * 64,
        },
    }
    return {**body, "profile_sha256": local._hash(body)}


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


def test_candidate_profile_captures_exact_dynamic_etf_and_binds_snapshot(
        tmp_path, monkeypatch):
    anchor = initialize_local_publisher(tmp_path / "publisher")
    monkeypatch.setattr(local, "_capture", _capture)
    profile = _candidate_profile()
    symbols = profile["required_symbols"]

    result = local.capture_local_daily_snapshot(
        symbols=symbols, asof=profile["asof"],
        publisher_dir=tmp_path / "publisher",
        expected_registry_sha256=anchor["registry_sha256"],
        output_dir=tmp_path / "prices", candidate_profile=profile,
        expected_candidate_profile_sha256=profile["profile_sha256"])

    value = json.loads((tmp_path / "prices" / "snapshot.json").read_text())
    assert result["candidate_profile_sha256"] == profile["profile_sha256"]
    assert value["artifact"]["capture_profile"] == {
        "profile": local.DYNAMIC_PROFILE,
        "profile_sha256": profile["profile_sha256"]}
    assert local.verify_local_daily_snapshot(
        value, expected_registry_sha256=anchor["registry_sha256"],
        expected_symbols=symbols, asof=profile["asof"],
        decision_cutoff=value["decision_cutoff"],
        expected_candidate_profile_sha256=profile["profile_sha256"]) == value


def test_candidate_profile_allows_existing_fixed_holding_in_exact_panel():
    profile = _candidate_profile()
    profile["required_symbols"].append("561980.SH")
    profile["required_symbols"].sort()
    profile["profile_sha256"] = local._hash({
        key: value for key, value in profile.items()
        if key != "profile_sha256"})

    assert local.verify_candidate_profile(
        profile, expected_sha256=profile["profile_sha256"],
        symbols=profile["required_symbols"], asof=profile["asof"]) == profile


def test_candidate_profile_allows_only_exact_sector_scan_extension():
    profile = _candidate_profile()
    symbols = sorted(set(profile["required_symbols"]) | local.SECTOR_SCAN_SYMBOLS)

    assert local.verify_candidate_profile(
        profile, expected_sha256=profile["profile_sha256"],
        symbols=symbols, asof=profile["asof"]) == profile

    with pytest.raises(ValueError, match="scope differs"):
        local.verify_candidate_profile(
            profile, expected_sha256=profile["profile_sha256"],
            symbols=symbols[:-1], asof=profile["asof"])


def test_candidate_profile_cannot_reclassify_index_as_dynamic_etf():
    profile = _candidate_profile()
    profile["required_symbols"] = ["000300.SH"]
    profile["candidates"][0].update(code="sh000300", symbol="000300.SH")
    profile["profile_sha256"] = local._hash({
        key: value for key, value in profile.items()
        if key != "profile_sha256"})

    with pytest.raises(ValueError, match="exact symbols differ"):
        local.verify_candidate_profile(
            profile, expected_sha256=profile["profile_sha256"],
            symbols=profile["required_symbols"], asof=profile["asof"])


@pytest.mark.parametrize("mutation", ["hash", "asof", "rule", "marker",
                                       "extra_symbol", "missing_benchmark"])
def test_candidate_profile_rejects_unbound_scope_before_capture(
        tmp_path, monkeypatch, mutation):
    profile = _candidate_profile()
    symbols = list(profile["required_symbols"])
    expected_hash = profile["profile_sha256"]
    if mutation == "hash":
        expected_hash = "f" * 64
    elif mutation == "asof":
        profile["asof"] = "2026-09-03"
    elif mutation == "rule":
        profile["candidates"][0]["promotion_rule_version"] = "legacy"
    elif mutation == "marker":
        profile["candidates"][0]["observation_contract"] = "legacy"
    elif mutation == "extra_symbol":
        symbols.append("588730.SH")
    else:
        profile["benchmark_symbols"] = []
    if mutation not in {"hash", "extra_symbol"}:
        profile["profile_sha256"] = local._hash({
            key: value for key, value in profile.items()
            if key != "profile_sha256"})
        expected_hash = profile["profile_sha256"]
    calls = []
    monkeypatch.setattr(local, "_capture", lambda *args: calls.append(args))

    with pytest.raises(ValueError, match="candidate local daily"):
        local.capture_local_daily_snapshot(
            symbols=symbols, asof="2026-09-04",
            publisher_dir=tmp_path / "publisher",
            expected_registry_sha256="a" * 64,
            output_dir=tmp_path / "prices", candidate_profile=profile,
            expected_candidate_profile_sha256=expected_hash)
    assert calls == []


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


def test_etf_baostock_price_fallback_is_not_in_local_profile():
    start, asof = "2025-07-11", "2026-09-04"
    attempts = [{"source": source, "capture": _capture(source, "561980.SH", start, asof, "qfq"),
                 "observed_at": datetime.now(timezone.utc).isoformat(), "error": None}
                for source in ("tencent.ifzq", "baostock")]
    with pytest.raises(ValueError, match="fixed route"):
        local.build_price_manifest(attempts, symbol="561980.SH", role="signal", start=start, asof=asof,
                                    decision_cutoff=datetime.now(timezone.utc).isoformat())


def test_index_eligible_primary_cannot_be_displaced_by_fallback():
    start, asof = "2025-07-11", "2026-09-04"
    attempts = [{"source": source, "capture": _capture(source, "000300.SH", start, asof, "raw"),
                 "observed_at": datetime.now(timezone.utc).isoformat(), "error": None}
                for source in ("baostock", "tencent.ifzq")]
    with pytest.raises(ValueError, match="higher-priority"):
        local.build_price_manifest(attempts, symbol="000300.SH", role="signal", start=start, asof=asof,
                                    decision_cutoff=datetime.now(timezone.utc).isoformat())
