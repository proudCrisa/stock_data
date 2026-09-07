import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from stockdata import local_main_buy_publisher as publisher
from stockdata.liquidity_amount_product import _canonical, _hash
from stockdata.provider_authority_admission import SOURCE_RECEIPT_SCHEMA
from test_main_buy_supplement import make_supplement
from test_provider_authority_publisher import _private_raw


def _publisher_fixture(tmp_path, monkeypatch):
    cutoff = "2026-09-05T04:10:00+00:00"
    fixture, pin, signer = make_supplement(
        asof="2026-09-04", decision_cutoff=cutoff
    )
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
    references = {component: {k: deepcopy(v) for k, v in inputs.items() if k != "authority_envelope"}
                  for component, inputs in fixture["references"].items()}
    global_inputs = {k: v for k, v in fixture["global_signals"].items() if k != "authority_envelope"}
    corporate_actions = references["corporate_actions"]
    receipt = next(iter(corporate_actions["source_receipts"].values()))
    receipt["schema_version"] = SOURCE_RECEIPT_SCHEMA
    del receipt["corporate_action_coverage"]
    response_name = "561980.SH-sse-announcements.json"
    response_raw = _canonical({"pageHelp": {"pageNo": 1, "pageCount": 1, "total": 0}, "result": []})
    request_receipt_name = response_name + ".receipt.json"
    request_receipt = {
        "observed_at": "2026-09-05T03:58:00+00:00",
        "request": {"method": "GET", "url": "https://query.sse.com.cn/commonQuery.do", "params": {
            "END_DATE": "20260905", "SECURITY_CODE": "561980", "START_DATE": "20250711",
            "isPagination": "true", "pageHelp.pageSize": "1000", "sqlId": "COMMON_PL_JJXX_JJGG_L"}},
        "response": {"status_code": 200, "sha256": hashlib.sha256(response_raw).hexdigest(),
                     "bytes": len(response_raw)},
    }
    request_receipt_raw = _canonical(request_receipt)
    evidence = {
        "schema_version": "stockdata-local-reference-source-evidence/1",
        "assessment": "synthetic production-path fixture only",
        "files": [
            {"file": response_name, "sha256": hashlib.sha256(response_raw).hexdigest(),
             "raw_base64": base64.b64encode(response_raw).decode("ascii")},
            {"file": request_receipt_name, "sha256": hashlib.sha256(request_receipt_raw).hexdigest(),
             "raw_base64": base64.b64encode(request_receipt_raw).decode("ascii")},
        ],
        "announcement_capture": {
            "response_file": response_name,
            "response_sha256": hashlib.sha256(response_raw).hexdigest(),
            "request_receipt_file": request_receipt_name,
            "request_receipt_sha256": hashlib.sha256(request_receipt_raw).hexdigest(),
        },
    }
    corporate_actions["source_evidence"] = evidence
    receipt["response_sha256"] = _hash(evidence)
    receipt_id = _hash(receipt)
    corporate_actions["source_receipts"] = {receipt_id: receipt}
    for record in corporate_actions["artifact"]["records"]:
        record["source_receipt_ids"] = [receipt_id]
    monkeypatch.setattr(publisher, "prepare_reference_inputs", lambda **kwargs: (references, global_inputs))

    enrollment = fixture["registry"]["signer_enrollments"][0]
    entry = "561980.SH@2026-09-04"
    qualification_payload = {
        "schema_version": publisher.CORPORATE_ACTION_COVERAGE_QUALIFICATION_SCHEMA,
        "trust_registry_sha256": pin,
        "publisher_key_id": enrollment["publisher_key_id"],
        "trust_root_id": enrollment["trust_root_id"],
        "qualified_at": "2026-09-05T03:59:00+00:00",
        "decision_cutoff_at": cutoff,
        "panel": [entry],
        "entries": [{
            "panel_entry": entry,
            "coverage_start": "2025-07-11",
            "coverage_end": "2026-09-05",
            "decision_cutoff_at": cutoff,
            "coverage_complete_through_decision_cutoff": True,
            "source_evidence_sha256": _hash(evidence),
            "announcement_response_file": response_name,
            "announcement_response_sha256": hashlib.sha256(response_raw).hexdigest(),
            "announcement_request_receipt_file": request_receipt_name,
            "announcement_request_receipt_sha256": hashlib.sha256(request_receipt_raw).hexdigest(),
        }],
    }
    qualification = {
        "schema_version": publisher.CORPORATE_ACTION_COVERAGE_QUALIFICATION_ENVELOPE_SCHEMA,
        "algorithm": "ed25519",
        "payload": qualification_payload,
        "signature_base64": base64.b64encode(signer.sign(_canonical(qualification_payload))).decode("ascii"),
    }
    qualification_path = tmp_path / "corporate-action-coverage.json"
    qualification_path.write_bytes(_canonical(qualification))
    class Clock:
        tick = datetime(2026, 9, 5, 4, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz):
            cls.tick += timedelta(microseconds=1)
            return cls.tick
    monkeypatch.setattr(publisher, "datetime", Clock)
    kwargs = {
        "evidence_dir": tmp_path,
        "liquidity_capture_file": capture_path,
        "global_snapshot_file": global_path,
        "publisher_dir": identity,
        "registry_sha256": pin,
        "provider_manifest_sha256": "a" * 64,
        "output_dir": tmp_path / "output",
        "corporate_action_coverage_file": qualification_path,
    }
    return kwargs, qualification_path, corporate_actions


def _resign_qualification(path, signer, mutate):
    value = json.loads(path.read_bytes())
    mutate(value["payload"])
    value["signature_base64"] = base64.b64encode(
        signer.sign(_canonical(value["payload"]))
    ).decode("ascii")
    path.write_bytes(_canonical(value))


def _argv(kwargs):
    argv = []
    for name, value in kwargs.items():
        argv.extend(("--" + name.replace("_", "-"), str(value)))
    return argv


def test_cli_requires_corporate_action_coverage_file():
    values = {
        "evidence-dir": "evidence", "liquidity-capture-file": "liquidity",
        "global-snapshot-file": "global", "publisher-dir": "publisher",
        "registry-sha256": "a" * 64, "provider-manifest-sha256": "b" * 64,
        "output-dir": "output",
    }
    argv = [item for name, value in values.items() for item in (f"--{name}", value)]
    with pytest.raises(SystemExit) as exc:
        publisher.main(argv)
    assert exc.value.code == 2


def test_production_cli_publishes_exact_cutoff_corporate_action_v2(
    tmp_path, monkeypatch, capsys
):
    kwargs, qualification_path, _ = _publisher_fixture(tmp_path, monkeypatch)
    publisher.main(_argv(kwargs))
    printed = json.loads(capsys.readouterr().out)
    supplement = json.loads((tmp_path / "output" / "supplement.json").read_bytes())
    corporate_actions = supplement["references"]["corporate_actions"]
    receipt = next(iter(corporate_actions["source_receipts"].values()))
    qualification = json.loads(qualification_path.read_bytes())
    cutoff = qualification["payload"]["decision_cutoff_at"]
    assert printed["decision_cutoff"] == cutoff == supplement["decision_cutoff"]
    assert supplement["liquidity"]["product"]["decision_cutoff"] == cutoff
    assert receipt["schema_version"] == "stockdata-provider-component-source-receipt/2"
    assert receipt["corporate_action_coverage"] == [
        {key: qualification["payload"]["entries"][0][key] for key in (
            "panel_entry", "coverage_start", "coverage_end", "decision_cutoff_at",
            "coverage_complete_through_decision_cutoff")}
    ]
    retained = corporate_actions["source_evidence"]
    assert retained["coverage_qualification"] == qualification


@pytest.mark.parametrize("failure", [
    "incomplete", "expired", "wrong_panel", "wrong_cutoff", "evidence_hash",
    "evidence_file_hash", "duplicate_panel", "alternate_capture", "bad_signature",
])
def test_production_coverage_failure_is_atomic(tmp_path, monkeypatch, failure):
    kwargs, qualification_path, corporate_actions = _publisher_fixture(tmp_path, monkeypatch)
    fixture, _, signer = make_supplement(asof="2026-09-04")
    mutations = {
        "incomplete": lambda payload: payload["entries"][0].update(
            coverage_complete_through_decision_cutoff=False),
        "expired": lambda payload: payload["entries"][0].update(coverage_end="2026-09-04"),
        "wrong_panel": lambda payload: payload.update(panel=["588730.SH@2026-09-04"]),
        "wrong_cutoff": lambda payload: payload["entries"][0].update(
            decision_cutoff_at="2026-09-05T04:09:00+00:00"),
        "evidence_hash": lambda payload: payload["entries"][0].update(
            source_evidence_sha256="b" * 64),
        "evidence_file_hash": lambda payload: payload["entries"][0].update(
            announcement_response_sha256="c" * 64),
        "duplicate_panel": lambda payload: payload["entries"].append(deepcopy(payload["entries"][0])),
    }
    if failure == "alternate_capture":
        evidence = corporate_actions["source_evidence"]
        response_raw = _canonical({"pageHelp": {"pageNo": 1, "pageCount": 2, "total": 1}, "result": []})
        response_name = "unreviewed-second-response.json"
        receipt_name = response_name + ".receipt.json"
        receipt = {
            "observed_at": "2026-09-05T03:58:00+00:00",
            "request": {"method": "GET", "url": "https://query.sse.com.cn/commonQuery.do", "params": {
                "END_DATE": "20260905", "SECURITY_CODE": "561980", "START_DATE": "20250711",
                "isPagination": "true", "pageHelp.pageSize": "1000", "sqlId": "COMMON_PL_JJXX_JJGG_L"}},
            "response": {"status_code": 200, "sha256": hashlib.sha256(response_raw).hexdigest(),
                         "bytes": len(response_raw)},
        }
        receipt_raw = _canonical(receipt)
        evidence["files"].extend([
            {"file": response_name, "sha256": hashlib.sha256(response_raw).hexdigest(),
             "raw_base64": base64.b64encode(response_raw).decode("ascii")},
            {"file": receipt_name, "sha256": hashlib.sha256(receipt_raw).hexdigest(),
             "raw_base64": base64.b64encode(receipt_raw).decode("ascii")},
        ])
        def point_to_alternate(payload):
            entry = payload["entries"][0]
            entry.update(
                source_evidence_sha256=_hash(evidence),
                announcement_response_file=response_name,
                announcement_response_sha256=hashlib.sha256(response_raw).hexdigest(),
                announcement_request_receipt_file=receipt_name,
                announcement_request_receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
            )
        _resign_qualification(qualification_path, signer, point_to_alternate)
    elif failure == "bad_signature":
        value = json.loads(qualification_path.read_bytes())
        value["signature_base64"] = base64.b64encode(b"x" * 64).decode("ascii")
        qualification_path.write_bytes(_canonical(value))
    else:
        _resign_qualification(qualification_path, signer, mutations[failure])
    with pytest.raises(ValueError, match="corporate-action coverage"):
        publisher.main(_argv(kwargs))
    assert not (tmp_path / "output").exists()


def test_legacy_v1_without_qualification_file_publishes_nothing(tmp_path, monkeypatch):
    kwargs, _, _ = _publisher_fixture(tmp_path, monkeypatch)
    kwargs["corporate_action_coverage_file"] = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="561980.SH@2026-09-04: qualification file"):
        publisher.publish_main_buy_supplement(**kwargs)
    assert not (tmp_path / "output").exists()


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
    symbols = sorted(publisher.REVIEWED_MAIN_BUY_SYMBOLS_V1)
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
    fixture, _, _ = make_supplement(asof=asof)
    symbols = sorted(publisher.REVIEWED_MAIN_BUY_SYMBOLS_V1)
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


def test_direct_extension_rejects_symbols_outside_reviewed_main_set(tmp_path):
    symbols = sorted(publisher.REVIEWED_MAIN_BUY_SYMBOLS_V1 | {"159992.SZ"})
    with pytest.raises(ValueError, match="reviewed main reference set"):
        publisher.extend_reference_inputs({}, {}, evidence_dir=tmp_path,
            symbols=symbols, asof="2026-09-07")


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
        "corporate_actions": ["announcements-current.json", "announcements-current.json.receipt.json",
                              "sse-561980-split-result-20260626.pdf", "sse-561980-split-result-20260626.pdf.receipt.json"],
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
    announcement_raw = _canonical(values["announcements-current.json"])
    values["announcements-current.json.receipt.json"] = {
        "observed_at": "2026-08-15T16:04:00+08:00",
        "request": {"method": "GET", "url": "https://query.sse.com.cn/commonQuery.do", "params": {
            "sqlId": "COMMON_PL_JJXX_JJGG_L", "SECURITY_CODE": "561980",
            "START_DATE": "20250101", "END_DATE": "20260815", "isPagination": "true",
            "pageHelp.pageSize": "1000"}},
        "response": {"status_code": 200, "sha256": hashlib.sha256(announcement_raw).hexdigest(),
                     "bytes": len(announcement_raw)},
    }
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
