from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from test_provider_authority_admission import _future_registration_inputs

import stockdata.collector_continuity as continuity
from stockdata import registered_panel_capture
from stockdata.collector_continuity import parse_collector_ledger
from stockdata.future_panel_registration import (
    FuturePanelRegistrationError,
    register_future_panel,
)
from stockdata.provider_authority_admission import (
    validate_local_mechanical_prerequisites,
)
from stockdata.registered_panel_capture import (
    RegisteredPanelCaptureError,
    capture_registered_panel,
)

LOCAL_AUTHORITY_MODE = "trusted_local_mechanical"
LOCAL_SCHEMA = "rqgm-forward-panel-registration/5"
LOCAL_PREREQUISITE_FILES = {
    "source_receipts",
    "trading_calendar",
    "market_rules",
}
LOCAL_PREREQUISITES = {
    "source_receipt_ids",
    "trading_calendar",
    "market_rule_prerequisite",
    "collector",
}
SIGNATURE_FIELDS = {
    "trust_registry_sha256",
    "role_publishers",
    "signature_sha256",
    "trust_root_id",
    "publisher_key_id",
    "authority_envelope",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _local_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    inputs, _ = _future_registration_inputs(tmp_path, monkeypatch)
    inputs.pop("calendar_authority_file")
    inputs.pop("market_rules_authority_file")
    inputs["authority_mode"] = LOCAL_AUTHORITY_MODE
    return inputs


def _register(inputs: dict[str, object]) -> dict[str, object]:
    result = register_future_panel(**inputs)
    assert isinstance(result, dict)
    return result


def _registration_path(inputs: dict[str, object]) -> Path:
    return Path(inputs["output_file"])


def _ledger_path(inputs: dict[str, object]) -> Path:
    return Path(continuity.default_collector_ledger_path(inputs["database_file"]))


def _mutate_json(path: Path, mutator: object) -> None:
    value = json.loads(path.read_bytes())
    assert callable(mutator)
    mutator(value)
    path.write_bytes(_canonical(value))


def test_trusted_local_registration_is_exactly_bound_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)

    result = _register(inputs)
    registration = _registration_path(inputs)
    ledger = _ledger_path(inputs)
    registration_before = registration.read_bytes()
    ledger_before = ledger.read_bytes()

    assert result["schema_version"] == LOCAL_SCHEMA
    assert result["authority_mode"] == LOCAL_AUTHORITY_MODE
    assert result["workspace_count"] == 36
    assert result["outcome_feedback_used"] is False
    assert set(result["prerequisite_files"]) == LOCAL_PREREQUISITE_FILES
    assert set(result["prerequisites"]) == LOCAL_PREREQUISITES
    assert not SIGNATURE_FIELDS.intersection(result)
    assert not SIGNATURE_FIELDS.intersection(result["prerequisites"])
    assert registration.read_bytes() == _canonical(result)

    events = parse_collector_ledger(ledger)
    bindings = [event for event in events if event["event_type"] == "REGISTRATION_BOUND"]
    assert len(bindings) == 1
    binding = bindings[0]["event"]
    assert binding["registration_sha256"] == hashlib.sha256(registration_before).hexdigest()
    assert binding["panel_sha256"] == result["panel_sha256"]
    assert binding["sessions"] == result["sessions"]
    assert binding["sessions_sha256"] == hashlib.sha256(
        _canonical(result["sessions"])
    ).hexdigest()
    assert binding["prerequisites_sha256"] == result["prerequisites_sha256"]

    assert _register(inputs) == result
    assert registration.read_bytes() == registration_before
    assert ledger.read_bytes() == ledger_before
    assert sum(
        event["event_type"] == "REGISTRATION_BOUND"
        for event in parse_collector_ledger(ledger)
    ) == 1


def test_trusted_local_registration_binds_static_hashes_and_exact_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    result = _register(inputs)
    prerequisites = result["prerequisites"]
    panel = {
        f"{symbol}@{session}"
        for symbol in result["symbols"]
        for session in result["sessions"]
    }

    receipt_ids = sorted(
        hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in inputs["source_receipt_files"]
    )
    assert prerequisites["source_receipt_ids"] == receipt_ids
    for component, file_key in (
        ("trading_calendar", "calendar_file"),
        ("market_rule_prerequisite", "market_rules_file"),
    ):
        evidence = prerequisites[component]
        assert evidence["artifact_sha256"] == hashlib.sha256(
            Path(inputs[file_key]).read_bytes()
        ).hexdigest()
        assert evidence["source_receipt_ids"] == receipt_ids
        assert set(evidence["available_at_by_panel"]) == panel
        assert set(evidence["effective_at_by_panel"]) == panel
        assert set(evidence["decision_cutoff_by_panel"]) == panel
    assert set(prerequisites["market_rule_prerequisite"]["policy_ids_by_panel"]) == panel


def test_trusted_local_registration_rejects_unused_well_formed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    extra = json.loads(Path(inputs["source_receipt_files"][0]).read_bytes())
    extra["observed_at"] = "2026-08-13T09:00:00+08:00"
    extra_path = tmp_path / "unused-receipt.json"
    extra_path.write_bytes(_canonical(extra))
    inputs["source_receipt_files"] = [
        *inputs["source_receipt_files"],
        extra_path,
    ]

    with pytest.raises(FuturePanelRegistrationError, match="receipt|closure|prerequisite"):
        _register(inputs)

    assert not _registration_path(inputs).exists()


def test_trusted_local_registration_rejects_extra_malformed_receipt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    extra_path = tmp_path / "malformed-receipt.json"
    extra_path.write_bytes(b"{not-canonical-json")
    inputs["source_receipt_files"] = [
        *inputs["source_receipt_files"],
        extra_path,
    ]

    with pytest.raises(FuturePanelRegistrationError, match="source receipt"):
        _register(inputs)

    assert not _registration_path(inputs).exists()


def test_trusted_local_prerequisites_parse_every_bound_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    calendar = json.loads(Path(inputs["calendar_file"]).read_bytes())
    market_rules = json.loads(Path(inputs["market_rules_file"]).read_bytes())
    bound_receipts = {
        hashlib.sha256(Path(path).read_bytes()).hexdigest(): json.loads(
            Path(path).read_bytes()
        )
        for path in inputs["source_receipt_files"]
    }
    bound_receipts["f" * 64] = {"malformed": True}
    panel = json.loads(Path(inputs["panel_file"]).read_bytes())

    with pytest.raises(ValueError, match="receipt|schema|incomplete"):
        validate_local_mechanical_prerequisites(
            calendar_artifact=calendar,
            market_rules_artifact=market_rules,
            expected_panel=panel,
            bound_source_receipts=bound_receipts,
        )


def test_trusted_local_prerequisites_accept_exact_used_receipt_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    bound_receipts = {
        hashlib.sha256(Path(path).read_bytes()).hexdigest(): json.loads(
            Path(path).read_bytes()
        )
        for path in inputs["source_receipt_files"]
    }
    result = validate_local_mechanical_prerequisites(
        calendar_artifact=json.loads(Path(inputs["calendar_file"]).read_bytes()),
        market_rules_artifact=json.loads(Path(inputs["market_rules_file"]).read_bytes()),
        expected_panel=json.loads(Path(inputs["panel_file"]).read_bytes()),
        bound_source_receipts=bound_receipts,
    )

    expected_receipts = sorted(bound_receipts)
    assert result["source_receipt_ids"] == expected_receipts
    assert result["trading_calendar"]["source_receipt_ids"] == expected_receipts
    assert result["market_rule_prerequisite"]["source_receipt_ids"] == expected_receipts


def test_trusted_local_prerequisites_reject_unreferenced_receipt_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    receipt_path = Path(inputs["source_receipt_files"][0])
    receipt = json.loads(receipt_path.read_bytes())
    calendar_path = Path(inputs["calendar_file"])
    calendar = json.loads(calendar_path.read_bytes())
    old_receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    extra_binding = {
        "component": "trading_calendar",
        "panel_entry": calendar["records"][0]["panel_entry"],
        "record_sha256": "f" * 64,
    }
    receipt["bindings"] = sorted(
        [*receipt["bindings"], extra_binding],
        key=lambda item: (
            item["component"], item["panel_entry"], item["record_sha256"]
        ),
    )
    new_receipt_id = hashlib.sha256(_canonical(receipt)).hexdigest()
    receipt_path.write_bytes(_canonical(receipt))
    for record in calendar["records"]:
        assert record["source_receipt_ids"] == [old_receipt_id]
        record["source_receipt_ids"] = [new_receipt_id]
    calendar_path.write_bytes(_canonical(calendar))

    with pytest.raises(FuturePanelRegistrationError, match="binding|closure|receipt"):
        _register(inputs)

    assert not _registration_path(inputs).exists()


@pytest.mark.parametrize("mode", ["signed", "trusted_local", ""])
def test_trusted_local_registration_rejects_wrong_authority_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    inputs["authority_mode"] = mode

    with pytest.raises(FuturePanelRegistrationError, match="authority|mode|schema"):
        _register(inputs)

    assert not _registration_path(inputs).exists()


@pytest.mark.parametrize("location", ["registration", "prerequisites", "calendar"])
def test_trusted_local_registration_rejects_mixed_signature_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    _register(inputs)
    registration = _registration_path(inputs)
    value = json.loads(registration.read_bytes())
    if location == "registration":
        value["trust_registry_sha256"] = "0" * 64
    elif location == "prerequisites":
        value["prerequisites"]["role_publishers"] = {}
    else:
        value["prerequisites"]["trading_calendar"]["signature_sha256"] = "0" * 64
    forged = _canonical(value)
    registration.write_bytes(forged)

    with pytest.raises(FuturePanelRegistrationError):
        _register(inputs)

    assert registration.read_bytes() == forged


def test_trusted_local_registration_rejects_static_timing_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    _mutate_json(
        Path(inputs["calendar_file"]),
        lambda value: value["records"][0].update(
            {"available_at": "2026-08-15T12:01:00+08:00"}
        ),
    )

    with pytest.raises(FuturePanelRegistrationError, match="timing|available|cutoff"):
        _register(inputs)

    assert not _registration_path(inputs).exists()


@pytest.mark.parametrize("artifact_key", ["calendar_file", "market_rules_file"])
def test_trusted_local_registration_rejects_incomplete_static_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact_key: str
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    _mutate_json(
        Path(inputs[artifact_key]),
        lambda value: value["records"].pop(),
    )

    with pytest.raises(
        FuturePanelRegistrationError,
        match="coverage|panel|record|complete|branch|is_st",
    ):
        _register(inputs)

    assert not _registration_path(inputs).exists()


def test_trusted_local_registration_never_overwrites_existing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    _register(inputs)
    registration = _registration_path(inputs)
    ledger = _ledger_path(inputs)
    forged = json.loads(registration.read_bytes())
    forged["status"] = "FORGED"
    forged_bytes = _canonical(forged)
    registration.write_bytes(forged_bytes)
    ledger_before = ledger.read_bytes()

    with pytest.raises(FuturePanelRegistrationError):
        _register(inputs)

    assert registration.read_bytes() == forged_bytes
    assert ledger.read_bytes() == ledger_before


def test_trusted_local_registration_rejects_collector_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    _register(inputs)
    registration = _registration_path(inputs)
    ledger = _ledger_path(inputs)
    registration_before = registration.read_bytes()
    ledger_before = ledger.read_bytes()

    replacement = tmp_path / "replacement.sqlite"
    shutil.copyfile(inputs["database_file"], replacement)
    os.replace(replacement, inputs["database_file"])

    with pytest.raises(FuturePanelRegistrationError, match="collector|identity|drift"):
        _register(inputs)

    assert registration.read_bytes() == registration_before
    assert ledger.read_bytes() == ledger_before


def test_capture_rejects_trusted_local_static_drift_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _local_inputs(tmp_path, monkeypatch)
    _register(inputs)
    _mutate_json(
        Path(inputs["calendar_file"]),
        lambda value: value["records"][0]["payload"].update(
            {"is_trading_day": False}
        ),
    )
    provider_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def provider_must_not_run(*args: object, **kwargs: object) -> None:
        provider_calls.append((args, kwargs))
        raise AssertionError("provider must not run after static prerequisite drift")

    monkeypatch.setattr(registered_panel_capture, "capture_phase", provider_must_not_run)
    monkeypatch.setattr(continuity, "_provider_call", provider_must_not_run, raising=False)
    monkeypatch.setattr(continuity.subprocess, "Popen", provider_must_not_run)
    monkeypatch.setattr(
        continuity,
        "_collector_attempt_now",
        lambda: "2026-08-17T09:00:00+08:00",
    )

    with pytest.raises(RegisteredPanelCaptureError, match="static|prerequisite|drift"):
        capture_registered_panel(
            _registration_path(inputs),
            database=inputs["database_file"],
            effective_date="2026-08-17",
            phase="pre_open",
        )

    assert provider_calls == []
