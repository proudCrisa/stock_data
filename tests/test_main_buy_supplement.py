from copy import deepcopy
from datetime import date, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from stockdata.authority import ALGORITHM, AUTHORITY_ENVELOPE_SCHEMA, load_enrolled_trust_registry_bytes
from stockdata.liquidity_amount_product import (
    _canonical, _hash, build_liquidity_amounts_product, build_liquidity_authority_inputs,
)
from stockdata.main_buy_supplement import (
    SCHEMA_VERSION, build_global_signals_authority_inputs, verify_main_buy_supplement,
)
from stockdata.market_rules import ETF_MARKET_RULE_PAYLOAD_SCHEMA, ETF_RULE_SCOPES
from stockdata.provider_authority_admission import SOURCE_RECEIPT_SCHEMA
from stockdata.provider_authority_publisher import publish_authority_envelope
from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS
from test_liquidity_amount_product import _receipt
from test_provider_authority_publisher import _b64, _enrollment, _key_id, _private_raw, _root_entry
from test_provider_market_rules import _rule

SYMBOL = "561980.SH"
ASOF = "2026-08-31"
CUTOFF = f"{ASOF}T16:10:00+08:00"


def _signed(inputs, *, registry_sha, root, signer, observed="2026-08-28T16:05:00+08:00"):
    artifact = inputs["artifact"]
    component = artifact["component"]
    value = {
        "component_role": component,
        "artifact": {"kind": f"stock-data-{component.replace('_', '-')}",
                     "identifier": _hash(artifact), "schema_version": artifact["schema_version"]},
        "source_receipt_ids": sorted(inputs["source_receipts"]),
        "effective_at": observed, "available_at": observed,
        "publisher_key_id": _key_id(signer), "trust_root_id": _key_id(root),
        "trust_registry_sha256": registry_sha,
    }
    return {**inputs, "authority_envelope": {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA, "algorithm": ALGORITHM,
        "payload": value, "signature_base64": _b64(signer.sign(_canonical(value))),
    }}


def _reference(component, rows, *, observed="2026-08-28T16:05:00+08:00"):
    records = [{"panel_entry": entry, "payload": payload, "record_sha256": _hash(payload),
                "source_receipt_ids": [], "effective_at": f"{entry.split('@')[1]}T00:00:00+08:00",
                "available_at": observed} for entry, payload in sorted(rows.items())]
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA, "observed_at": observed,
        "source": "official-exchange-rulebook" if component == "market_rules" else "fixture-authority",
        "response_sha256": _rule()["source_sha256"] if component == "market_rules" else _hash(rows),
        "bindings": [{"component": component, "panel_entry": row["panel_entry"],
                      "record_sha256": row["record_sha256"]} for row in records],
    }
    receipt_id = _hash(receipt)
    for record in records:
        record["source_receipt_ids"] = [receipt_id]
    return {"artifact": {"schema_version": COMPONENT_SCHEMAS[component], "component": component,
                         "panel": sorted(rows), "records": records},
            "source_receipts": {receipt_id: receipt}}


def make_supplement(*, asof=ASOF, snapshot=None, rule_overrides=None,
                    decision_cutoff=None, observed_at=None):
    from stockdata.authority import TRUST_REGISTRY_SCHEMA

    root = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    signer = Ed25519PrivateKey.from_private_bytes(bytes([2]) * 32)
    roles = sorted(["liquidity_amounts", "global_signals", "trading_calendar", "instrument_status",
                    "market_rules", "universe", "corporate_actions"])
    registry = {"schema_version": TRUST_REGISTRY_SCHEMA, "registry_version": 1,
                "trust_roots": [_root_entry(root)], "signer_enrollments": [_enrollment(root, signer, roles=roles)]}
    registry_sha = _hash(registry)
    days = []
    day = date.fromisoformat(asof)
    while len(days) < 20:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    days.reverse()
    observed = observed_at or f"{days[-1]}T16:05:00+08:00"
    cutoff = decision_cutoff or f"{asof}T16:10:00+08:00"
    early = f"{(date.fromisoformat(days[0]) - timedelta(days=1)).isoformat()}T16:00:00+08:00"
    def sign(inputs, **kwargs):
        return _signed(inputs, root=root, signer=signer, registry_sha=registry_sha,
                       observed=kwargs.get("observed", observed))
    capture = _receipt(SYMBOL, days)
    capture["observed_at"] = observed
    product = build_liquidity_amounts_product(
        [capture], panel=[(SYMBOL, day) for day in days],
        decision_cutoff=cutoff, expected_watermark=days[-1],
    )
    quotes = {name: {"price": 100., "chg_pct": 0., "chg_20d": 0., "date": days[-1]}
              for name in ("fxUSDJPY", "usNDX", "hkHSI", "fxDINIW")}
    if snapshot is None:
        snapshot = {"schema_version": "trading-global-snapshot/1", "asof": asof,
                    "quotes": quotes, "risk": {"level": "normal", "allow_buy": True}}
        snapshot["snapshot_sha256"] = _hash(snapshot)
    panel = [f"{SYMBOL}@{asof}"]
    calendar_rows = {}
    next_day = date.fromisoformat(asof) + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    sessions = [*days, next_day.isoformat()]
    for day, next_day in zip(sessions, sessions[1:]):
        calendar_rows[f"{SYMBOL}@{day}"] = {
            "is_trading_day": True, "decision_cutoff_at": f"{day}T09:25:00+08:00",
            "session_close_at": f"{day}T15:00:00+08:00",
            "next_session_decision_cutoff_at": f"{next_day}T09:25:00+08:00",
        }
    rule = _rule(schema_version=ETF_MARKET_RULE_PAYLOAD_SCHEMA, policy_id="561980-etf-fixture",
                 security_type="ETF", board="ETF", instrument_id=SYMBOL,
                 effective_from=asof, effective_until=asof,
                 **{key: value for key, value in ETF_RULE_SCOPES[SYMBOL].items() if key != "effective_from"})
    rule.update(rule_overrides or {})
    reference_rows = {
        "instrument_status": {"is_st": False, "is_suspended": False, "listing_status": "listed"},
        "market_rules": rule,
        "universe": {"is_member": True, "universe_id": _hash([SYMBOL])},
        "corporate_actions": {"events": []},
    }
    references = {component: sign(_reference(component, {panel[0]: row}, observed=observed))
                  for component, row in reference_rows.items()}
    references["trading_calendar"] = sign(
        _reference("trading_calendar", calendar_rows, observed=early), observed=early,
    )
    payload = {
        "schema_version": SCHEMA_VERSION, "provider_manifest_sha256": "a" * 64,
        "asof": asof, "decision_cutoff": cutoff, "symbols": [SYMBOL], "registry": registry,
        "liquidity": sign(build_liquidity_authority_inputs(product)),
        "global_signals": sign(build_global_signals_authority_inputs(
            snapshot, panel=panel, available_at=observed)),
        "references": references,
    }
    return payload, registry_sha, signer


def _verify(payload, pin):
    return verify_main_buy_supplement(payload, expected_registry_sha256=pin,
                                      provider_manifest_sha256="a" * 64, asof=ASOF,
                                      decision_cutoff=CUTOFF)


def test_offline_supplement_admits_real_etf_identity_with_all_signed_inputs(monkeypatch):
    payload, pin, _ = make_supplement()
    monkeypatch.setattr("pathlib.Path.read_bytes", lambda *args: pytest.fail("hidden filesystem read"))
    assert _verify(payload, pin) == payload


def _dynamic_supplement():
    from stockdata.candidate_instrument_authority import verify_candidate_instrument_authority
    from stockdata.local_main_buy_publisher import _reference
    from test_candidate_instrument_authority import candidate_authority
    from test_liquidity_amount_product import _receipt

    payload, pin, signer = make_supplement()
    root = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    reviewer = Ed25519PrivateKey.from_private_bytes(bytes([3]) * 32)
    payload["registry"]["signer_enrollments"].append(
        _enrollment(root, reviewer, roles=["market_rules"]))
    payload["registry"]["signer_enrollments"].sort(
        key=lambda row: row["publisher_key_id"])
    pin = _hash(payload["registry"])
    from test_candidate_instrument_authority import candidate_profile
    profile = candidate_profile()
    profile["asof"] = ASOF
    for row in profile["candidates"]:
        row["promoted_asof"] = ASOF
    profile["source_authorization"]["scan_asof"] = ASOF
    profile["profile_sha256"] = _hash({key: value for key, value in profile.items()
                                       if key != "profile_sha256"})
    authority = candidate_authority(
        registry_sha=pin, root=root, reviewer=reviewer, profile=profile)
    verified = verify_candidate_instrument_authority(
        authority, decision_cutoff=payload["decision_cutoff"])
    symbols = ["512480.SH", SYMBOL]
    days = sorted({entry.split("@")[1]
                   for entry in payload["liquidity"]["product"]["panel"]})
    dynamic_capture = _receipt("512480.SH", days)
    dynamic_capture["observed_at"] = "2026-08-31T16:05:00+08:00"
    captures = [payload["liquidity"]["product"]["source_receipts"][0],
                dynamic_capture]
    product = build_liquidity_amounts_product(
        captures, panel=[(symbol, day) for symbol in symbols for day in days],
        decision_cutoff=payload["decision_cutoff"], expected_watermark=ASOF,
        candidate_instrument_authority=authority)
    payload["liquidity"] = _signed(
        build_liquidity_authority_inputs(product), registry_sha=pin,
        root=root, signer=signer)
    evidence = {"schema_version": "fixture-reviewed-source-evidence/1", "files": [],
                "candidate_instrument_authority_sha256": authority["authority_sha256"]}
    references = {}
    base_refs = payload["references"]
    for component, inputs in base_refs.items():
        rows = {row["panel_entry"]: deepcopy(row["payload"])
                for row in inputs["artifact"]["records"]}
        for entry, value in list(rows.items()):
            if not entry.startswith(SYMBOL + "@"):
                continue
            dynamic_entry = "512480.SH@" + entry.split("@")[1]
            dynamic = deepcopy(value)
            if component == "market_rules":
                dynamic.update(verified["scopes"]["512480.SH"])
                dynamic.update(instrument_id="512480.SH",
                               policy_id="512480-etf-fixture",
                               source="local-reviewed-official-facts/1",
                               source_sha256=_hash(evidence))
            elif component == "universe":
                dynamic["universe_id"] = profile["profile_sha256"]
            rows[dynamic_entry] = dynamic
        if component == "market_rules":
            for value in rows.values():
                value["source"] = "local-reviewed-official-facts/1"
                value["source_sha256"] = _hash(evidence)
        references[component] = _signed(
            _reference(component, rows, evidence, "2026-08-28T16:05:00+08:00"),
            registry_sha=pin, root=root, signer=signer,
            observed=("2026-07-31T16:05:00+08:00"
                      if component == "trading_calendar" else
                      "2026-08-28T16:05:00+08:00"))
    payload["symbols"] = symbols
    payload["references"] = references
    payload["global_signals"] = _signed(
        build_global_signals_authority_inputs(
            payload["global_signals"]["snapshot"],
            panel=[f"{symbol}@{ASOF}" for symbol in symbols],
            available_at="2026-08-28T16:05:00+08:00"),
        registry_sha=pin, root=root, signer=signer)
    payload["candidate_instrument_authority"] = authority
    return payload, pin


def test_dynamic_candidate_supplement_binds_liquidity_and_all_five_references():
    payload, pin = _dynamic_supplement()
    assert _verify(payload, pin) == payload
    assert payload["symbols"] == ["512480.SH", "561980.SH"]
    assert all(value["source_evidence"]["candidate_instrument_authority_sha256"]
               == payload["candidate_instrument_authority"]["authority_sha256"]
               for value in payload["references"].values())


def test_dynamic_candidate_supplement_rejects_resealed_unbound_authority():
    payload, pin = _dynamic_supplement()
    payload["candidate_instrument_authority"]["instruments"][0][
        "rule_scope"]["fund_type"] = "OTHER"
    authority = payload["candidate_instrument_authority"]
    authority["authority_sha256"] = _hash({key: value for key, value in authority.items()
                                           if key != "authority_sha256"})
    with pytest.raises(ValueError, match="candidate instrument"):
        _verify(payload, pin)


def test_dynamic_candidate_review_signer_cannot_sign_product_components():
    payload, pin = _dynamic_supplement()
    signer = Ed25519PrivateKey.from_private_bytes(bytes([2]) * 32)
    authority = payload["candidate_instrument_authority"]
    envelope = authority["review_envelope"]
    envelope["payload"]["publisher_key_id"] = _key_id(signer)
    envelope["signature_base64"] = _b64(
        signer.sign(_canonical(envelope["payload"])))
    authority["authority_sha256"] = _hash({
        key: value for key, value in authority.items() if key != "authority_sha256"})
    with pytest.raises(ValueError, match="review signer must be independent"):
        _verify(payload, pin)


@pytest.mark.parametrize("mutation", ["pin", "signature", "amount", "calendar_gap", "global", "universe", "manifest", "etf"])
def test_supplement_rejects_authority_drift(mutation):
    payload, pin, _ = make_supplement()
    if mutation == "pin":
        pin = "f" * 64
    elif mutation == "signature":
        payload["liquidity"]["authority_envelope"]["signature_base64"] = "A" * 88
    elif mutation == "amount":
        payload["liquidity"]["product"]["records"][0]["payload"]["amount"] += 1
    elif mutation == "calendar_gap":
        payload["references"]["trading_calendar"]["artifact"]["records"][0]["payload"]["next_session_decision_cutoff_at"] = "2026-08-06T09:25:00+08:00"
    elif mutation == "global":
        payload["global_signals"]["snapshot"]["quotes"]["fxUSDJPY"]["price"] += 1
    elif mutation == "universe":
        payload["references"]["universe"]["artifact"]["records"][0]["payload"]["is_member"] = False
    elif mutation == "manifest":
        payload["provider_manifest_sha256"] = "b" * 64
    else:
        payload["references"]["market_rules"]["artifact"]["records"][0]["payload"]["instrument_id"] = "588730.SH"
    with pytest.raises(ValueError):
        _verify(payload, pin)


def test_native_liquidity_publishes_through_existing_production_signer(tmp_path, monkeypatch):
    payload, pin, signer = make_supplement()
    inputs = payload["liquidity"]
    registry_file, artifact_file = tmp_path / "registry.json", tmp_path / "artifact.json"
    registry_file.write_bytes(_canonical(payload["registry"]))
    artifact_file.write_bytes(_canonical(inputs["artifact"]))
    receipt_files = []
    for receipt_id, receipt in inputs["source_receipts"].items():
        path = tmp_path / f"{receipt_id}.json"
        path.write_bytes(_canonical(receipt))
        receipt_files.append(path)
    monkeypatch.setenv("TEST_LIQUIDITY_KEY", _b64(_private_raw(signer)))
    published = publish_authority_envelope(
        component="liquidity_amounts", registry_file=registry_file, registry_sha256=pin,
        artifact_file=artifact_file, source_receipt_files=receipt_files,
        signer_private_key_env="TEST_LIQUIDITY_KEY", output_file=tmp_path / "signed.json",
        effective_at=f"{ASOF}T16:05:00+08:00", available_at=f"{ASOF}T16:05:00+08:00",
        decision_cutoff_by_panel={entry: CUTOFF for entry in inputs["artifact"]["panel"]},
    )
    payload["liquidity"]["authority_envelope"] = published.envelope
    assert _verify(payload, pin) == payload


def test_existing_registry_pin_is_required_for_portable_bytes():
    payload, pin, _ = make_supplement()
    assert load_enrolled_trust_registry_bytes(_canonical(payload["registry"]), expected_sha256=pin).registry_sha256 == pin


def test_signed_calendar_gap_rejects_even_with_valid_reissued_signature():
    payload, pin, signer = make_supplement()
    calendar = payload["references"]["trading_calendar"]
    rows = {row["panel_entry"]: row["payload"] for row in calendar["artifact"]["records"]}
    rows[sorted(rows)[0]]["next_session_decision_cutoff_at"] = "2026-08-06T09:25:00+08:00"
    observed = next(iter(calendar["source_receipts"].values()))["observed_at"]
    payload["references"]["trading_calendar"] = _signed(
        _reference("trading_calendar", rows, observed=observed),
        registry_sha=pin, root=Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32),
        signer=signer, observed=observed,
    )
    with pytest.raises(ValueError, match="session chain has a gap"):
        _verify(payload, pin)


def test_global_snapshot_accepts_optional_null_and_twenty_day_trend():
    from stockdata.main_buy_supplement import validate_global_snapshot

    payload, _, _ = make_supplement()
    snapshot = payload["global_signals"]["snapshot"]
    del snapshot["quotes"]["usNDX"]["chg_pct"]
    snapshot["quotes"]["fxUSDJPY"]["chg_20d"] = None
    snapshot["snapshot_sha256"] = _hash({key: value for key, value in snapshot.items() if key != "snapshot_sha256"})
    assert validate_global_snapshot(snapshot) == snapshot


def test_later_caller_cutoff_retains_original_frozen_cutoff():
    payload, pin, _ = make_supplement(asof="2026-08-14")
    assert verify_main_buy_supplement(
        payload, expected_registry_sha256=pin, provider_manifest_sha256="a" * 64,
        asof="2026-08-14", decision_cutoff="2026-08-14T17:00:00+08:00",
    ) == payload


def test_friday_price_session_accepts_actual_saturday_evidence_freeze():
    observed = "2026-09-05T07:00:00+08:00"
    cutoff = "2026-09-05T08:00:00+08:00"
    payload, pin, _ = make_supplement(
        asof="2026-09-04", decision_cutoff=cutoff, observed_at=observed,
    )
    assert verify_main_buy_supplement(
        payload, expected_registry_sha256=pin, provider_manifest_sha256="a" * 64,
        asof="2026-09-04", decision_cutoff=cutoff,
    ) == payload
    assert payload["liquidity"]["product"]["panel"][-1] == f"{SYMBOL}@2026-09-04"
    for component in ("liquidity", "global_signals"):
        assert payload[component]["authority_envelope"]["payload"]["available_at"] == observed
        assert all(row["available_at"] == observed for row in payload[component]["artifact"]["records"])
    calendar = payload["references"]["trading_calendar"]["artifact"]["records"][-1]["payload"]
    assert calendar["next_session_decision_cutoff_at"] == "2026-09-07T09:25:00+08:00"


@pytest.mark.parametrize("cutoff", [
    "2026-09-04T14:59:59+08:00", "2026-09-04T15:00:00+08:00",
    "2026-09-07T09:25:00+08:00", "2026-09-07T09:25:01+08:00",
])
def test_frozen_cutoff_rejects_outside_signed_session_window(cutoff):
    payload, pin, _ = make_supplement(
        asof="2026-09-04", decision_cutoff="2026-09-05T08:00:00+08:00",
        observed_at="2026-09-05T07:00:00+08:00",
    )
    payload["decision_cutoff"] = cutoff
    with pytest.raises(ValueError, match="outside signed asof session finality window"):
        verify_main_buy_supplement(
            payload, expected_registry_sha256=pin, provider_manifest_sha256="a" * 64,
            asof="2026-09-04", decision_cutoff=cutoff,
        )


def test_exact_etf_rule_publishes_after_signed_status_admission(tmp_path, monkeypatch):
    payload, pin, signer = make_supplement()
    registry_file = tmp_path / "registry.json"
    registry_file.write_bytes(_canonical(payload["registry"]))
    monkeypatch.setenv("TEST_ETF_RULE_KEY", _b64(_private_raw(signer)))
    status = None
    for component in ("instrument_status", "market_rules"):
        inputs = payload["references"][component]
        artifact_file = tmp_path / f"{component}.json"
        artifact_file.write_bytes(_canonical(inputs["artifact"]))
        receipt_files = []
        for receipt_id, receipt in inputs["source_receipts"].items():
            path = tmp_path / f"{receipt_id}.json"
            path.write_bytes(_canonical(receipt))
            receipt_files.append(path)
        result = publish_authority_envelope(
            component=component, registry_file=registry_file, registry_sha256=pin,
            artifact_file=artifact_file, source_receipt_files=receipt_files,
            signer_private_key_env="TEST_ETF_RULE_KEY", output_file=tmp_path / f"signed-{component}.json",
            effective_at=f"{ASOF}T16:05:00+08:00", available_at=f"{ASOF}T16:05:00+08:00",
            decision_cutoff_by_panel={f"{SYMBOL}@{ASOF}": CUTOFF},
            instrument_status_authority=status,
        )
        if component == "instrument_status":
            status = result.admitted
        payload["references"][component]["authority_envelope"] = result.envelope
    assert _verify(payload, pin) == payload


@pytest.mark.parametrize("late_layer", [None, "record", "receipt", "envelope"])
def test_current_calendar_observation_preserves_historical_pit_default(late_layer):
    from stockdata.provider_authority_admission import admit_signed_component_authority

    payload, pin, signer = make_supplement()
    old = payload["references"]["trading_calendar"]
    rows = {row["panel_entry"]: row["payload"] for row in old["artifact"]["records"]}
    observed = f"{ASOF}T16:05:00+08:00"
    inputs = _reference("trading_calendar", rows, observed=observed)
    if late_layer == "record":
        inputs["artifact"]["records"][0]["available_at"] = CUTOFF
    elif late_layer == "receipt":
        receipt = next(iter(inputs["source_receipts"].values()))
        receipt["observed_at"] = CUTOFF
        receipt_id = _hash(receipt)
        inputs["source_receipts"] = {receipt_id: receipt}
        for record in inputs["artifact"]["records"]:
            record["source_receipt_ids"] = [receipt_id]
    signed = _signed(inputs, registry_sha=pin,
        root=Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32), signer=signer,
        observed=CUTOFF if late_layer == "envelope" else observed)
    kwargs = dict(component="trading_calendar", artifact_value=signed["artifact"],
        authority_envelope=signed["authority_envelope"], expected_panel=signed["artifact"]["panel"],
        bound_source_receipts=signed["source_receipts"],
        registry=load_enrolled_trust_registry_bytes(_canonical(payload["registry"]), expected_sha256=pin))
    with pytest.raises(ValueError):
        admit_signed_component_authority(**kwargs)
    if late_layer:
        with pytest.raises(ValueError):
            admit_signed_component_authority(**kwargs, current_decision_observation_cutoff=CUTOFF)
    else:
        admitted = admit_signed_component_authority(**kwargs, current_decision_observation_cutoff=CUTOFF)
        assert admitted.signed_calendar_phases_by_panel[f"{SYMBOL}@{ASOF}"]["decision_cutoff_at"] == f"{ASOF}T09:25:00+08:00"
        payload["references"]["trading_calendar"] = signed
        assert _verify(payload, pin) == payload


def test_current_observation_cutoff_is_calendar_only():
    from stockdata.provider_authority_admission import admit_signed_component_authority

    payload, pin, _ = make_supplement()
    inputs = payload["references"]["instrument_status"]
    with pytest.raises(ValueError, match="calendar-only"):
        admit_signed_component_authority(component="instrument_status", artifact_value=inputs["artifact"],
            authority_envelope=inputs["authority_envelope"], expected_panel=inputs["artifact"]["panel"],
            bound_source_receipts=inputs["source_receipts"],
            registry=load_enrolled_trust_registry_bytes(_canonical(payload["registry"]), expected_sha256=pin),
            current_decision_observation_cutoff=CUTOFF)


def test_liquidity_product_can_freeze_before_final_signed_supplement():
    payload, pin, _ = make_supplement()
    original_product = _canonical(payload["liquidity"]["product"])
    payload["decision_cutoff"] = f"{ASOF}T16:11:00+08:00"
    assert verify_main_buy_supplement(payload, expected_registry_sha256=pin,
        provider_manifest_sha256="a" * 64, asof=ASOF, decision_cutoff=payload["decision_cutoff"]) == payload
    assert _canonical(payload["liquidity"]["product"]) == original_product
    payload["decision_cutoff"] = f"{ASOF}T16:09:00+08:00"
    with pytest.raises(ValueError, match="product freeze is after authority cutoff"):
        verify_main_buy_supplement(payload, expected_registry_sha256=pin,
            provider_manifest_sha256="a" * 64, asof=ASOF, decision_cutoff=payload["decision_cutoff"])


def test_reference_raw_evidence_must_be_bound_to_each_signed_record():
    payload, pin, _ = make_supplement()
    inputs = payload["references"]["universe"]
    rows = {row["panel_entry"]: row["payload"] for row in inputs["artifact"]["records"]}
    inputs["source_evidence"] = rows
    assert _verify(payload, pin) == payload
    inputs["source_evidence"] = {"untrusted": "replacement"}
    receipt = {**next(iter(inputs["source_receipts"].values())),
               "response_sha256": _hash(inputs["source_evidence"])}
    inputs["source_receipts"][_hash(receipt)] = receipt
    with pytest.raises(ValueError, match="raw reference evidence is not receipt-bound"):
        _verify(payload, pin)


def test_global_raw_source_evidence_is_signed_and_replayed():
    payload, pin, signer = make_supplement()
    inputs = build_global_signals_authority_inputs(payload["global_signals"]["snapshot"],
        panel=[f"{SYMBOL}@{ASOF}"], available_at=f"{ASOF}T16:05:00+08:00",
        source_evidence={"source": "local-automation", "raw": "retained original bytes"})
    payload["global_signals"] = _signed(inputs, registry_sha=pin,
        root=Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32), signer=signer,
        observed=f"{ASOF}T16:05:00+08:00")
    assert _verify(payload, pin) == payload
    payload["global_signals"]["source_evidence"]["raw"] = "changed"
    with pytest.raises(ValueError, match="global snapshot closure drifted"):
        _verify(payload, pin)
