from datetime import datetime, timedelta, timezone
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from stockdata import local_main_buy_publisher as publisher
from stockdata.liquidity_amount_product import _canonical
from stockdata.main_buy_supplement import verify_main_buy_supplement
from test_main_buy_supplement import make_supplement
from test_provider_authority_publisher import _private_raw


def test_publisher_freezes_after_signing_without_backdating_source_facts(tmp_path, monkeypatch):
    fixture, pin, signer = make_supplement(asof="2026-09-04")
    identity = tmp_path / "identity"
    identity.mkdir()
    (identity / "registry.json").write_bytes(_canonical(fixture["registry"]))
    key = identity / "publisher.key"
    key.write_bytes(_private_raw(signer))
    key.chmod(0o600)
    capture = fixture["liquidity"]["product"]["source_receipts"][0]
    capture_path = tmp_path / "capture.json"
    capture_path.write_bytes(_canonical(capture))
    global_path = tmp_path / "global.json"
    global_path.write_bytes(_canonical(fixture["global_signals"]["snapshot"]))
    references = {component: {k: v for k, v in inputs.items() if k != "authority_envelope"}
                  for component, inputs in fixture["references"].items()}
    global_inputs = {k: v for k, v in fixture["global_signals"].items() if k != "authority_envelope"}
    monkeypatch.setattr(publisher, "prepare_reference_inputs", lambda **kwargs: (references, global_inputs))
    class Clock:
        tick = datetime(2026, 9, 5, 4, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz):
            cls.tick += timedelta(microseconds=1)
            return cls.tick
    monkeypatch.setattr(publisher, "datetime", Clock)
    result = publisher.publish_main_buy_supplement(evidence_dir=tmp_path,
        liquidity_capture_file=capture_path, global_snapshot_file=global_path,
        publisher_dir=identity, registry_sha256=pin, provider_manifest_sha256="a" * 64,
        output_dir=tmp_path / "output")
    payload = json.loads((tmp_path / "output" / "supplement.json").read_bytes())
    assert verify_main_buy_supplement(payload, expected_registry_sha256=pin,
        provider_manifest_sha256="a" * 64, asof="2026-09-04", decision_cutoff=result["decision_cutoff"]) == payload
    assert payload["liquidity"]["product"]["source_receipts"][0]["observed_at"] == capture["observed_at"]
    freeze = datetime.fromisoformat(payload["decision_cutoff"])
    inputs = [payload["liquidity"], payload["global_signals"], *payload["references"].values()]
    assert all(datetime.fromisoformat(item["authority_envelope"]["payload"]["available_at"]) < freeze for item in inputs)


def test_status_cannot_relabel_another_etfs_same_day_response():
    captures = [
        {"request": {"method": "query_stock_basic", "code": "sh.588730"},
         "observed_at": "2026-09-05T04:00:00Z", "response": {"error_code": "0",
            "fields": ["code", "type", "status"], "rows": [["sh.588730", "5", "1"]]}},
        {"request": {"method": "query_history_k_data_plus", "code": "sh.588730",
            "fields": "date,tradestatus,isST", "start_date": "2026-09-04", "end_date": "2026-09-04",
            "frequency": "d", "adjustflag": "3"}, "observed_at": "2026-09-05T04:00:00Z",
         "response": {"error_code": "0", "fields": ["date", "tradestatus", "isST"],
            "rows": [["2026-09-04", "1", "1"]]}},
    ]
    args = ("588730.SH", "2026-09-04", "2026-09-05T05:00:00Z")
    assert publisher._validate_status_capture(captures, *args)["isST"] == "1"
    changed = deepcopy(captures)
    changed[1]["request"]["code"] = "sh.560900"
    with pytest.raises(ValueError, match="request identity"):
        publisher._validate_status_capture(changed, *args)
    changed = deepcopy(captures)
    changed[1]["observed_at"] = args[-1]
    with pytest.raises(ValueError, match="observation"):
        publisher._validate_status_capture(changed, *args)


def test_official_announcement_completeness_is_rebuilt_from_receipt(tmp_path):
    import hashlib
    name = "159350.SZ-szse-announcements.raw.json"
    data = {"announceCount": 1, "data": [{"secCode": ["159350"], "attachPath": "/disc/fixture.pdf", "title": "fixture"}]}
    raw = _canonical(data)
    receipt = {"observed_at": "2026-09-05T04:00:00Z", "request": {"method": "POST",
        "url": "https://www.szse.cn/api/disc/announcement/annList", "body": {
            "stock": ["159350"], "channelCode": ["fundinfoNotice_disc"],
            "seDate": ["2025-07-11", "2026-09-05"], "pageSize": 50, "pageNum": 1}},
        "response": {"status_code": 200, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}
    (tmp_path / name).write_bytes(raw)
    (tmp_path / (name + ".receipt.json")).write_bytes(_canonical(receipt))
    args = (tmp_path, "159350.SZ", [name, name + ".receipt.json"], "2026-09-05T05:00:00Z")
    publisher._validate_announcement_capture(*args, reviewed_non_action_urls=["https://disc.static.szse.cn/disc/fixture.pdf"])
    del data["data"][0]["attachPath"]
    raw = _canonical(data)
    receipt["response"].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    (tmp_path / name).write_bytes(raw)
    (tmp_path / (name + ".receipt.json")).write_bytes(_canonical(receipt))
    with pytest.raises(ValueError, match="page coverage"):
        publisher._validate_announcement_capture(*args, reviewed_non_action_urls=[])
    data["announceCount"] = 51
    raw = _canonical(data)
    receipt["response"].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    (tmp_path / name).write_bytes(raw)
    (tmp_path / (name + ".receipt.json")).write_bytes(_canonical(receipt))
    with pytest.raises(ValueError, match="page coverage"):
        publisher._validate_announcement_capture(*args)


def test_index_cannot_omit_reviewed_cash_dividends_or_invent_an_etf_event():
    with pytest.raises(ValueError, match="corporate-action set"):
        publisher._validate_reviewed_events({"events": [], "cash_dividend_identities": []}, "511010.SH")
    with pytest.raises(ValueError, match="corporate-action set"):
        publisher._validate_reviewed_events({"events": [{"event_type": "split"}]}, "588730.SH")
    publisher._validate_reviewed_events({"events": []}, "588730.SH")


def test_universe_is_recomputed_from_retained_config_bytes(tmp_path):
    import hashlib
    from stockdata.market_rules import ETF_RULE_SCOPES
    symbols = sorted(ETF_RULE_SCOPES)
    raw = _canonical({"holdings": [{"code": s[-2:].lower() + s[:6]} for s in symbols]})
    universe = {"symbols": symbols, "source_sha256": hashlib.sha256(raw).hexdigest()}
    (tmp_path / "local-main-config.json").write_bytes(raw)
    (tmp_path / "local-main-universe.json").write_bytes(_canonical(universe))
    args = (tmp_path, "588730.SH", ["local-main-config.json", "local-main-universe.json"])
    assert publisher._validate_universe(*args) == universe
    (tmp_path / "local-main-config.json").write_bytes(_canonical({"holdings": []}))
    with pytest.raises(ValueError, match="source hash"):
        publisher._validate_universe(*args)


@pytest.mark.parametrize("asof", ["2026-09-04", "2026-08-14"])
def test_extended_market_rules_bind_final_combined_source_receipt(tmp_path, monkeypatch, asof):
    from stockdata.market_rules import ETF_RULE_SCOPES
    fixture, _, _ = make_supplement(asof=asof)
    symbols = sorted(ETF_RULE_SCOPES)
    references = {component: {**value, "source_evidence": {"files": []}}
                  for component, value in fixture["references"].items()}
    global_inputs = {**fixture["global_signals"], "source_evidence": {"files": []}}
    instruments = {}
    for symbol in symbols:
        if symbol == "561980.SH":
            continue
        (tmp_path / f"{symbol}-status-ca.json").write_bytes(b"[]")
        instruments[symbol] = {"events": [], "corporate_actions_coverage": {
            "start_date": "2025-07-11", "end_date": asof, "complete": True},
            "source_files": {component: [f"{symbol}-status-ca.json"] for component in references},
            "assessments": {component: "fixture" for component in references}}
    (tmp_path / "evidence-index.json").write_bytes(_canonical({"schema_version": "stockdata-main-etf-reviewed-facts/1",
        "asof": asof, "instruments": instruments}))
    monkeypatch.setattr(publisher, "_evidence", lambda *args: {"files": []})
    monkeypatch.setattr(publisher, "_validate_reviewed_events", lambda *args: None)
    monkeypatch.setattr(publisher, "_validate_status_capture", lambda *args: {"isST": "1"})
    monkeypatch.setattr(publisher, "_validate_announcement_capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "_validate_universe", lambda *args: {"source_sha256": "a" * 64})
    refs, _ = publisher.extend_reference_inputs(references, global_inputs,
        evidence_dir=tmp_path, symbols=symbols, asof=asof)
    rules = refs["market_rules"]
    assert len(rules["artifact"]["records"]) == 8
    for row in rules["artifact"]["records"]:
        receipt = rules["source_receipts"][row["source_receipt_ids"][0]]
        assert row["payload"]["source_sha256"] == receipt["response_sha256"]


def test_dynamic_extension_derives_all_reference_panels_from_candidate_authority(
        tmp_path, monkeypatch):
    from stockdata.candidate_instrument_authority import verify_candidate_instrument_authority
    from test_candidate_instrument_authority import candidate_authority

    asof = "2026-08-28"
    fixture, _, _ = make_supplement(asof=asof)
    references = {component: {**value, "source_evidence": {"files": []}}
                  for component, value in fixture["references"].items()}
    global_inputs = {**fixture["global_signals"], "source_evidence": {"files": []}}
    authority = candidate_authority()
    verified = verify_candidate_instrument_authority(
        authority, decision_cutoff="2026-08-31T09:25:00+08:00")
    symbol = "512480.SH"
    (tmp_path / f"{symbol}-status-ca.json").write_bytes(b"[]")
    facts = {"events": [], "corporate_actions_coverage": {
        "start_date": "2025-07-11", "end_date": asof, "complete": True},
        "source_files": {component: [f"{symbol}-status-ca.json"]
                         for component in references},
        "assessments": {component: "fixture" for component in references}}
    (tmp_path / "evidence-index.json").write_bytes(_canonical({
        "schema_version": "stockdata-main-etf-reviewed-facts/1", "asof": asof,
        "instruments": {symbol: facts}}))
    monkeypatch.setattr(publisher, "_evidence", lambda *args: {"files": []})
    monkeypatch.setattr(publisher, "_validate_reviewed_events", lambda *args: None)
    monkeypatch.setattr(publisher, "_validate_status_capture", lambda *args: {"isST": "1"})
    monkeypatch.setattr(publisher, "_validate_announcement_capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "_validate_universe",
                        lambda *args, **kwargs: {"source_sha256": authority["candidate_profile"]["profile_sha256"]})
    refs, _ = publisher.extend_reference_inputs(
        references, global_inputs, evidence_dir=tmp_path,
        symbols=[symbol, "561980.SH"], asof=asof,
        candidate_instrument_authority=authority)
    for component, inputs in refs.items():
        assert inputs["artifact"]["panel"][-1] == "561980.SH@2026-08-28"
        assert f"{symbol}@{asof}" in inputs["artifact"]["panel"]
        assert inputs["source_evidence"]["candidate_instrument_authority_sha256"] \
            == verified["authority_sha256"]
    rule = next(row["payload"] for row in refs["market_rules"]["artifact"]["records"]
                if row["panel_entry"] == f"{symbol}@{asof}")
    assert {key: rule[key] for key in verified["scopes"][symbol]} \
        == verified["scopes"][symbol]


def test_dynamic_publisher_needs_no_561980_capture_or_instrument_evidence(
        tmp_path, monkeypatch):
    from stockdata.authority import load_enrolled_trust_registry_bytes
    from stockdata.main_buy_supplement import validate_global_snapshot
    from test_candidate_instrument_authority import (
        candidate_authority, candidate_profile, candidate_registry,
    )
    from test_liquidity_amount_product import _receipt, _sessions

    registry_value, pin, root, signer, reviewer = candidate_registry()
    profile = candidate_profile()
    profile["required_symbols"] = ["000300.SH", "512480.SH"]
    profile["candidates"] = profile["candidates"][:1]
    profile["profile_sha256"] = publisher._hash({
        key: value for key, value in profile.items() if key != "profile_sha256"})
    authority = candidate_authority(
        registry_sha=pin, root=root, reviewer=reviewer, profile=profile)
    evidence = tmp_path / "dynamic"
    evidence.mkdir()
    symbol, asof = "512480.SH", profile["asof"]
    days = _sessions()
    amount = _receipt(symbol, days)
    (evidence / f"{symbol}-amount.json").write_bytes(_canonical(amount))
    status = [{
        "request": {"method": "query_stock_basic", "code": "sh.512480"},
        "observed_at": f"{asof}T16:00:00+08:00",
        "response": {"error_code": "0", "fields": ["code", "type", "status"],
                     "rows": [["sh.512480", "5", "1"]]},
    }, {
        "request": {"method": "query_history_k_data_plus", "code": "sh.512480",
                    "fields": "date,tradestatus,isST", "start_date": asof,
                    "end_date": asof, "frequency": "d", "adjustflag": "3"},
        "observed_at": f"{asof}T16:00:00+08:00",
        "response": {"error_code": "0", "fields": ["date", "tradestatus", "isST"],
                     "rows": [[asof, "1", "0"]]},
    }]
    status_name = f"{symbol}-status-ca.json"
    (evidence / status_name).write_bytes(_canonical(status))
    next_day = "2026-08-31"
    calendar = {"request": {"method": "query_trade_dates", "start_date": days[0],
                             "end_date": next_day},
                "response": {"error_code": "0", "rows": [
                    [day, "1"] for day in [*days, next_day]]}}
    (evidence / "calendar.json").write_bytes(_canonical(calendar))
    fees = {"policy_version": "broker-fee-v1", "commission_rate": .0003,
            "min_commission_cny": 5., "etf_stamp_tax": 0.}
    (evidence / "fees.json").write_bytes(_canonical(fees))
    config_raw = _canonical({"holdings": []})
    (evidence / "local-main-config.json").write_bytes(config_raw)
    universe = {"symbols": profile["required_symbols"],
                "source_sha256": profile["profile_sha256"]}
    (evidence / "local-main-universe.json").write_bytes(_canonical(universe))
    announcement_name = f"{symbol}-sse-announcements.json"
    announcement = {"result": [], "pageHelp": {
        "pageCount": 1, "pageNo": 1, "total": 0}}
    announcement_raw = _canonical(announcement)
    (evidence / announcement_name).write_bytes(announcement_raw)
    announcement_receipt = {"observed_at": f"{asof}T16:00:00+08:00",
        "request": {"method": "GET", "url": "https://query.sse.com.cn/commonQuery.do",
                    "params": {"END_DATE": asof.replace("-", ""), "SECURITY_CODE": "512480",
                               "START_DATE": "20250711", "isPagination": "true",
                               "pageHelp.pageSize": "1000", "sqlId": "COMMON_PL_JJXX_JJGG_L"}},
        "response": {"status_code": 200,
                     "sha256": hashlib.sha256(announcement_raw).hexdigest(),
                     "bytes": len(announcement_raw)}}
    receipt_name = announcement_name + ".receipt.json"
    (evidence / receipt_name).write_bytes(_canonical(announcement_receipt))
    global_name = "global-quotes.json"
    global_source = {"fixture": {"price": 1., "chg_pct": 0., "chg_20d": 0.,
                                  "last_date": asof}}
    (evidence / global_name).write_bytes(_canonical(global_source))
    names = [status_name, "calendar.json", "fees.json", "local-main-config.json",
             "local-main-universe.json", announcement_name, receipt_name,
             global_name]
    files = [{"file": name,
              "sha256": hashlib.sha256((evidence / name).read_bytes()).hexdigest()}
             for name in names]
    facts = {"events": [], "reviewed_non_action_urls": [],
             "corporate_actions_coverage": {
                 "start_date": "2025-07-11", "end_date": asof, "complete": True},
             "source_files": {
                 "trading_calendar": ["calendar.json"],
                 "instrument_status": [status_name],
                 "market_rules": ["fees.json"],
                 "universe": ["local-main-config.json", "local-main-universe.json"],
                 "corporate_actions": [announcement_name, receipt_name]},
             "assessments": {component: "isolated synthetic source fixture"
                             for component in ("trading_calendar", "instrument_status",
                                               "market_rules", "universe",
                                               "corporate_actions")}}
    (evidence / "evidence-index.json").write_bytes(_canonical({
        "schema_version": "stockdata-main-etf-reviewed-facts/1", "asof": asof,
        "instruments": {symbol: facts}, "files": files,
        "source_files": {"global_signals": [global_name]},
        "assessments": {"global_signals": "isolated synthetic source fixture"}}))
    authority_file = tmp_path / "authority.json"
    authority_file.write_bytes(_canonical(authority))
    publisher_dir = tmp_path / "publisher"
    publisher_dir.mkdir()
    (publisher_dir / "registry.json").write_bytes(_canonical(registry_value))
    (publisher_dir / "publisher.key").write_bytes(_private_raw(signer))
    (publisher_dir / "publisher.key").chmod(0o600)
    snapshot = {"schema_version": "trading-global-snapshot/1", "asof": asof,
                "quotes": {"fixture": {"price": 1., "chg_pct": 0., "chg_20d": 0.,
                                        "date": asof}},
                "risk": {"level": "normal", "allow_buy": True}}
    snapshot["snapshot_sha256"] = publisher._hash(snapshot)
    validate_global_snapshot(snapshot)
    snapshot_file = tmp_path / "global.json"
    snapshot_file.write_bytes(_canonical(snapshot))
    class FixedDateTime(datetime):
        tick = 0

        @classmethod
        def now(cls, tz=None):
            cls.tick += 1
            value = datetime.fromisoformat(
                f"{asof}T17:00:00+08:00") + timedelta(microseconds=cls.tick)
            return value if tz is None else value.astimezone(tz)
    monkeypatch.setattr(publisher, "datetime", FixedDateTime)

    result = publisher.publish_main_buy_supplement(
        evidence_dir=tmp_path / "absent-561980-evidence",
        global_snapshot_file=snapshot_file, publisher_dir=publisher_dir,
        registry_sha256=pin, provider_manifest_sha256="a" * 64,
        output_dir=tmp_path / "output", additional_evidence_dir=evidence,
        candidate_instrument_authority_file=authority_file, asof=asof,
        observation_end=asof)

    payload = json.loads(Path(result["supplement_file"]).read_bytes())
    assert payload["symbols"] == [symbol]
    assert all(input_["artifact"]["panel"][-1].startswith(symbol + "@")
               for input_ in payload["references"].values())
    assert not any("561980" in path.name for path in evidence.iterdir())
    registry = load_enrolled_trust_registry_bytes(_canonical(registry_value), expected_sha256=pin)
    assert registry.registry_sha256 == pin

    changed = deepcopy(snapshot)
    changed["quotes"]["fixture"]["price"] = 2.
    changed["snapshot_sha256"] = publisher._hash({
        key: value for key, value in changed.items() if key != "snapshot_sha256"})
    snapshot_file.write_bytes(_canonical(changed))
    with pytest.raises(ValueError, match="differs retained source facts"):
        publisher.publish_main_buy_supplement(
            evidence_dir=tmp_path / "absent-561980-evidence",
            global_snapshot_file=snapshot_file, publisher_dir=publisher_dir,
            registry_sha256=pin, provider_manifest_sha256="a" * 64,
            output_dir=tmp_path / "global-drift", additional_evidence_dir=evidence,
            candidate_instrument_authority_file=authority_file, asof=asof,
            observation_end=asof)
    snapshot_file.write_bytes(_canonical(snapshot))
    with pytest.raises(ValueError, match="observation window"):
        publisher.publish_main_buy_supplement(
            evidence_dir=tmp_path / "absent-561980-evidence",
            global_snapshot_file=snapshot_file, publisher_dir=publisher_dir,
            registry_sha256=pin, provider_manifest_sha256="a" * 64,
            output_dir=tmp_path / "bad-window", additional_evidence_dir=evidence,
            candidate_instrument_authority_file=authority_file, asof=asof,
            observation_end="2026-08-27")


def test_base_reference_sources_are_selected_by_index_for_another_session(tmp_path):
    import hashlib
    asof = "2026-08-14"
    fixture, _, _ = make_supplement(asof=asof)
    product = fixture["liquidity"]["product"]
    global_snapshot = fixture["global_signals"]["snapshot"]
    days = [entry.split("@")[1] for entry in product["panel"]] + ["2026-08-17"]
    source_files = {
        "trading_calendar": ["calendar-current.json", "rules.docx"],
        "instrument_status": ["status-current.json", "listing-current.json", "rules.docx"],
        "universe": ["universe-current.json"],
        "corporate_actions": ["announcements-current.json", "sse-561980-split-result-20260626.pdf", "sse-561980-split-result-20260626.pdf.receipt.json"],
        "market_rules": ["classification.html", "rules.docx", "faq.html", "fees.json", "fee-observation.json"],
        "global_signals": ["global-current.json", "global-observation.json"],
    }
    observed = "2026-08-14T16:05:00+08:00"
    values = {
        "calendar-current.json": {"request": {"method": "query_trade_dates", "start_date": days[0], "end_date": days[-1]},
            "observed_at": observed, "response": {"error_code": "0", "rows": [[day, "1"] for day in days]}},
        "status-current.json": {"observed_at": observed, "requests": [
            {"method": "query_stock_basic", "code": "sh.561980"},
            {"method": "query_history_k_data_plus", "code": "sh.561980", "fields": "date,tradestatus,isST",
             "start_date": asof, "end_date": asof, "frequency": "d", "adjustflag": "3"}], "responses": [
            {"error_code": "0", "fields": ["code", "type", "status"], "rows": [["sh.561980", "5", "1"]]},
            {"error_code": "0", "fields": ["date", "tradestatus", "isST"], "rows": [[asof, "1", "1"]]}]},
        "listing-current.json": {"result": [{"FUND_CODE": "561980", "CATEGORY": "F112"}]},
        "universe-current.json": {"is_member": True, "query": {"canonical_symbol": "561980.SH"}, "source": {"sha256": "a" * 64}},
        "announcements-current.json": {"result": [{"SECURITY_CODE": "561980", "TITLE": "split result fixture",
            "URL": "/disclosure/fund/announcement/c/new/2026-06-26/561980_20260626_4J0A.pdf"}],
            "pageHelp": {"pageNo": 1, "pageCount": 1, "total": 1}},
        "fees.json": {"policy_version": "broker-fee-v1", "commission_rate": .000085, "min_commission_cny": 5, "etf_stamp_tax": 0},
        "global-current.json": {name: {"price": q["price"], "chg_pct": q["chg_pct"], "chg_20d": q["chg_20d"], "last_date": q["date"]}
                                for name, q in global_snapshot["quotes"].items()},
    }
    split_url = "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-06-26/561980_20260626_4J0A.pdf"
    values["sse-561980-split-result-20260626.pdf.receipt.json"] = {"request": {"url": split_url},
        "response": {"status_code": 200, "bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}}
    files = []
    for name in sorted({name for names in source_files.values() for name in names}):
        raw = _canonical(values.get(name, {}))
        (tmp_path / name).write_bytes(raw)
        files.append({"file": name, "sha256": hashlib.sha256(raw).hexdigest(),
            "source_url": split_url if name == "sse-561980-split-result-20260626.pdf" else
                "https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_PL_JJXX_JJGG_L&SECURITY_CODE=561980&START_DATE=20250101&END_DATE=20260815"})
    (tmp_path / "evidence-index.json").write_bytes(_canonical({"asof": asof, "symbol": "561980.SH", "files": files,
        "source_files": source_files, "assessments": {name: "synthetic fixture" for name in ["calendar", "status", "global", "universe", "corporate_actions", "market_rules"]}}))
    refs, _ = publisher.prepare_reference_inputs(evidence_dir=tmp_path, liquidity_product=product,
        asof=asof, global_snapshot=global_snapshot, observation_end="2026-08-15")
    assert refs["instrument_status"]["artifact"]["panel"] == ["561980.SH@2026-08-14"]
    with pytest.raises(ValueError, match="announcement observation identity"):
        publisher.prepare_reference_inputs(evidence_dir=tmp_path, liquidity_product=product,
            asof=asof, global_snapshot=global_snapshot, observation_end="2026-08-16")
    with pytest.raises(ValueError, match="identity differs"):
        publisher.prepare_reference_inputs(evidence_dir=tmp_path, liquidity_product=product,
            asof="2026-08-13", global_snapshot=global_snapshot, observation_end="2026-08-15")


def test_new_event_requires_exact_official_attachment_and_cannot_be_omitted():
    import base64
    import hashlib
    raw = b"new official event fixture"
    digest = hashlib.sha256(raw).hexdigest()
    url = "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-09-07/560900_event.pdf"
    receipt = {"request": {"url": url}, "response": {"status_code": 200, "sha256": digest, "bytes": len(raw)}}
    evidence = {"files": [
        {"file": "event.pdf", "source_url": url, "sha256": digest, "raw_base64": base64.b64encode(raw).decode()},
        {"file": "event.pdf.receipt.json", "sha256": hashlib.sha256(_canonical(receipt)).hexdigest(),
         "raw_base64": base64.b64encode(_canonical(receipt)).decode()},
    ]}
    announcements = [(url, "分红公告")]
    events = [{"event_type": "cash_dividend", "effective_date": "2026-09-08", "event_id": digest}]
    publisher._validate_reviewed_events({"events": events}, "560900.SH")
    publisher._validate_event_attachments(announcements, evidence, events)
    with pytest.raises(ValueError, match="unreviewed"):
        publisher._validate_event_attachments(announcements, evidence, [])
    with pytest.raises(ValueError, match="exact official"):
        publisher._validate_event_attachments([(url.replace("560900", "588730"), "分红公告")], evidence, events)
    with pytest.raises(ValueError, match="exact official"):
        publisher._validate_event_attachments(announcements, evidence, [{**events[0], "event_id": "a" * 64}])
    new_row = (url + "-other", "New official announcement, not keyword-classified")
    with pytest.raises(ValueError, match="unreviewed official"):
        publisher._validate_event_attachments([*announcements, new_row], evidence, events)
    publisher._validate_event_attachments([*announcements, new_row], evidence, events,
        reviewed_non_action_urls=[new_row[0]])
    with pytest.raises(ValueError, match="unreviewed official"):
        publisher._validate_event_attachments([*announcements, new_row], evidence, events,
            reviewed_non_action_urls=[new_row[0], new_row[0]])
