from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_collector_phase_orchestration import _append_completed_attempt
from test_collector_step_state import _bound_registration, _prepare_collector
from test_trusted_local_forward_registration import _local_inputs, _register

import stockdata.collector_continuity as continuity


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def test_v5_registration_freezes_twelve_specs_without_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs_root = tmp_path / "trusted-local"
    inputs_root.mkdir()
    inputs = _local_inputs(inputs_root, monkeypatch)
    registration = Path(inputs["output_file"])
    _register(inputs)
    before = registration.read_bytes()
    value_before = json.loads(before)
    registration_sha256 = hashlib.sha256(before).hexdigest()

    specs = continuity.freeze_collector_step_schedule(
        registration_file=registration
    )

    assert len(specs) == 12
    assert [spec.step_ordinal for spec in specs] == list(range(12))
    assert all(spec.registration_sha256 == registration_sha256 for spec in specs)
    assert value_before["schema_version"] == "rqgm-forward-panel-registration/5"
    assert value_before["authority_mode"] == "trusted_local_mechanical"
    assert registration.read_bytes() == before
    assert json.loads(registration.read_bytes()) == value_before

    ledger = Path(
        continuity.default_collector_ledger_path(inputs["database_file"])
    )
    with continuity.acquire_collector_phase_lease(ledger) as lease:
        for spec in specs:
            _append_completed_attempt(lease, spec)

    history = continuity.parse_collector_ledger(ledger.read_bytes())
    completed = [
        event["event"]["step_ordinal"]
        for event in history
        if event["event_type"] == "ATTEMPT_COMPLETED"
    ]
    assert completed == list(range(12))


def test_v4_registration_remains_accepted_by_schedule_freezer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _prepare_collector(tmp_path, monkeypatch)
    registration = _bound_registration(database)
    before = registration.read_bytes()

    specs = continuity.freeze_collector_step_schedule(
        registration_file=registration
    )

    assert len(specs) == 12
    assert [spec.step_ordinal for spec in specs] == list(range(12))
    assert registration.read_bytes() == before

    ledger = Path(
        continuity.default_collector_ledger_path(database)
    )
    with continuity.acquire_collector_phase_lease(ledger) as lease:
        _append_completed_attempt(lease, specs[0])
