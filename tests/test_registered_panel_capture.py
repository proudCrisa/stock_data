from __future__ import annotations

import json
import hashlib
import os

import pytest

from stockdata.cli import build_params
import stockdata.collector_continuity as continuity
from stockdata.future_panel_registration import REGISTRATION_SCHEMA
from stockdata.provider_authority_admission import (
    GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA,
)
import stockdata.registered_panel_capture as registered_panel_capture
from stockdata.registered_panel_capture import (
    RegisteredPanelCaptureError,
    capture_registered_panel,
)
from test_collector_attempt_protocol import _prepared


SYMBOLS = [
    "000001.SZ",
    "000333.SZ",
    "000725.SZ",
    "000858.SZ",
    "002415.SZ",
    "300750.SZ",
    "600030.SH",
    "600036.SH",
    "600276.SH",
    "600519.SH",
    "601166.SH",
    "601318.SH",
]
SESSIONS = ["2026-08-12", "2026-08-13", "2026-08-14"]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


@pytest.fixture(autouse=True)
def _stub_live_reverification(monkeypatch) -> None:
    monkeypatch.setattr(
        registered_panel_capture,
        "reverify_registration_prerequisites",
        lambda **_: None,
        raising=False,
    )


def _registration(tmp_path, **changes):
    cells = sorted(f"{symbol}@{day}" for symbol in SYMBOLS for day in SESSIONS)

    prerequisites = {
        "trust_registry_sha256": "1" * 64,
        "role_publishers": {
            "trading_calendar": "2" * 64,
            "universe": "2" * 64,
            "instrument_status": "2" * 64,
            "corporate_actions": "2" * 64,
            "market_rules": "2" * 64,
        },
        "trading_calendar": {
            "artifact_sha256": "3" * 64,
            "signature_sha256": "4" * 64,
        },
        "market_rule_prerequisite": {
            "schema_version": GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA,
            "artifact_sha256": "5" * 64,
            "source_receipt_ids": ["7" * 64],
            "publisher_key_id": "8" * 64,
            "trust_root_id": "9" * 64,
            "signature_sha256": "6" * 64,
            "authority_envelope": {"schema_version": "stockdata-authority-envelope/1"},
            "available_at_by_panel": {cells[0]: "2026-08-11T08:00:00+08:00"},
            "effective_at_by_panel": {cells[0]: "2026-08-11T00:00:00+08:00"},
            "decision_cutoff_by_panel": {cells[0]: "2026-08-12T09:25:00+08:00"},
            "policy_ids_by_panel": {cells[0]: ["generic-main-sz-v1"]},
        },
        "collector": {
            "schema_version": "stockdata-forward-collector-capability/2",
            "database_path": str((tmp_path / "evidence.sqlite").resolve()),
            "ledger_path": str((tmp_path / "evidence.sqlite.collector-ledger.jsonl").resolve()),
            "source": "tencent",
            "adjustment_mode": "raw",
            "adjustment_version": "tencent-qt-daily-v1",
            "collector_schema_sha256": "7" * 64,
            "database_identity": {
                "schema_version": "stockdata-forward-collector-physical-identity/1",
                "canonical_path": str((tmp_path / "evidence.sqlite").resolve()),
                "parent_st_dev": 1,
                "parent_st_ino": 2,
                "file_st_dev": 1,
                "file_st_ino": 3,
            },
            "ledger_identity": {
                "schema_version": "stockdata-forward-collector-physical-identity/1",
                "canonical_path": str(
                    (tmp_path / "evidence.sqlite.collector-ledger.jsonl").resolve()
                ),
                "parent_st_dev": 1,
                "parent_st_ino": 2,
                "file_st_dev": 1,
                "file_st_ino": 4,
            },
            "database_uuid": "8" * 64,
            "cohort_sha256": "9" * 64,
            "genesis_sha256": "a" * 64,
            "ledger_genesis_event_sha256": "b" * 64,
        },
    }
    payload = {
        "schema_version": REGISTRATION_SCHEMA,
        "registered_at": "2026-08-11T23:14:47+08:00",
        "as_of": "2026-08-11",
        "symbols": SYMBOLS,
        "sessions": SESSIONS,
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
        "database_path": str((tmp_path / "evidence.sqlite").resolve()),
        "panel_sha256": hashlib.sha256(
            json.dumps(cells, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "ascii"
            )
        ).hexdigest(),
        "workspace_count": 36,
        "outcome_feedback_used": False,
        "status": "AWAITING_FULL_SNAPSHOT_READINESS",
        "prerequisite_files": {
            "source_receipts": [str((tmp_path / "receipt.json").resolve())],
            "trading_calendar": str((tmp_path / "calendar.json").resolve()),
            "trading_calendar_authority": str(
                (tmp_path / "calendar-authority.json").resolve()
            ),
            "market_rules": str((tmp_path / "rules.json").resolve()),
            "market_rules_authority": str(
                (tmp_path / "rules-authority.json").resolve()
            ),
        },
        "prerequisites": prerequisites,
        "prerequisites_sha256": hashlib.sha256(
            json.dumps(
                prerequisites,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest(),
    }
    payload.update(changes)
    path = tmp_path / "registration.json"
    path.write_text(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ),
        encoding="ascii",
    )
    return path


def _assert_registered_capture_unavailable(
    path, *, database, monkeypatch, effective_date: str
) -> None:
    legacy_calls = 0
    continuity_calls = 0

    def provider_must_not_run(*args, **kwargs):
        nonlocal legacy_calls
        legacy_calls += 1
        raise AssertionError("registered /4 must not call legacy capture_phase")

    def continuity_rejects(*args, **kwargs):
        nonlocal continuity_calls
        continuity_calls += 1
        raise registered_panel_capture.CollectorContinuityError(
            "continuity orchestration rejected"
        )

    monkeypatch.setattr(registered_panel_capture, "capture_phase", provider_must_not_run)
    monkeypatch.setattr(
        registered_panel_capture,
        "execute_registered_collector_phase",
        continuity_rejects,
    )
    with pytest.raises(RegisteredPanelCaptureError, match="continuity orchestration"):
        capture_registered_panel(
            path,
            database=database,
            effective_date=effective_date,
            phase="pre_open",
        )
    assert legacy_calls == 0
    assert continuity_calls == 1


def test_registered_capture_binds_exact_pending_panel(tmp_path, monkeypatch) -> None:
    _assert_registered_capture_unavailable(
        _registration(tmp_path),
        database=tmp_path / "evidence.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-12",
    )


def test_registered_capture_reverifies_generic_rulebook_prerequisite(
    tmp_path, monkeypatch
) -> None:
    _assert_registered_capture_unavailable(
        _registration(tmp_path),
        database=tmp_path / "evidence.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-12",
    )


def test_registered_capture_rejects_execution_readiness_in_prerequisite(
    tmp_path, monkeypatch
) -> None:
    registration = json.loads(_registration(tmp_path).read_text())
    registration["prerequisites"]["market_rule_prerequisite"]["ready"] = True
    registration["prerequisites_sha256"] = hashlib.sha256(
        _canonical(registration["prerequisites"])
    ).hexdigest()
    path = tmp_path / "execution-authority-registration.json"
    path.write_bytes(_canonical(registration))
    _assert_registered_capture_unavailable(
        path,
        database=tmp_path / "evidence.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-12",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"sessions": ["2026-08-12", "2026-08-13", "2026-08-16"]},
        {"outcome_feedback_used": True},
        {"status": "READY"},
    ],
)
def test_registered_capture_rejects_drifted_panel(
    tmp_path, monkeypatch, changes
) -> None:
    _assert_registered_capture_unavailable(
        _registration(tmp_path, **changes),
        database=tmp_path / "evidence.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-12",
    )


def test_registered_capture_rejects_unregistered_day(tmp_path, monkeypatch) -> None:
    _assert_registered_capture_unavailable(
        _registration(tmp_path),
        database=tmp_path / "evidence.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-15",
    )


def test_registered_capture_rejects_expired_or_wrong_database(
    tmp_path, monkeypatch
) -> None:
    path = _registration(tmp_path)
    _assert_registered_capture_unavailable(
        path,
        database=tmp_path / "evidence.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-12",
    )
    _assert_registered_capture_unavailable(
        path,
        database=tmp_path / "other.sqlite",
        monkeypatch=monkeypatch,
        effective_date="2026-08-12",
    )


def test_registered_capture_v4_fails_closed_before_capture_or_provider(
    tmp_path, monkeypatch
) -> None:
    legacy_calls = 0
    continuity_calls = 0

    def capture_must_not_run(spec, phase):
        nonlocal legacy_calls
        legacy_calls += 1
        raise AssertionError("registered /4 must not call legacy capture_phase")

    def continuity_rejects(*args, **kwargs):
        nonlocal continuity_calls
        continuity_calls += 1
        raise registered_panel_capture.CollectorContinuityError(
            "continuity orchestration rejected"
        )

    monkeypatch.setattr(registered_panel_capture, "capture_phase", capture_must_not_run)
    monkeypatch.setattr(
        registered_panel_capture,
        "execute_registered_collector_phase",
        continuity_rejects,
    )
    with pytest.raises(RegisteredPanelCaptureError, match="continuity orchestration"):
        capture_registered_panel(
            _registration(tmp_path),
            database=tmp_path / "evidence.sqlite",
            effective_date="2026-08-12",
            phase="pre_open",
        )
    assert legacy_calls == 0
    assert continuity_calls == 1


def test_registered_capture_rejects_registration_replacement_before_provider_call(
    tmp_path, monkeypatch
) -> None:
    path = _registration(tmp_path)
    original = path.read_bytes()
    replacement = json.loads(original)
    replacement["sessions"] = ["2026-08-12", "2026-08-13", "2026-08-18"]
    path.write_bytes(_canonical(replacement))
    calls = 0

    def provider_must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not run after registration replacement")

    monkeypatch.setattr(registered_panel_capture, "capture_phase", provider_must_not_run)
    with pytest.raises(RegisteredPanelCaptureError):
        capture_registered_panel(
            path,
            database=tmp_path / "evidence.sqlite",
            effective_date="2026-08-12",
            phase="pre_open",
        )
    assert calls == 0
    assert path.read_bytes() != original


def test_legacy_registration_and_non_genesis_capture_are_zero_call_fail_closed(
    tmp_path, monkeypatch
) -> None:
    path = _registration(tmp_path)
    payload = json.loads(path.read_bytes())
    payload["schema_version"] = "rqgm-forward-panel-registration/3"
    payload["prerequisites"]["collector"] = {
        "schema_version": "stockdata-forward-collector-capability/1",
        "database_path": payload["database_path"],
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
        "database_schema_sha256": "7" * 64,
    }
    payload["prerequisites_sha256"] = hashlib.sha256(
        _canonical(payload["prerequisites"])
    ).hexdigest()
    path.write_bytes(_canonical(payload))
    calls = 0

    def provider_must_not_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not run for legacy/non-genesis input")

    monkeypatch.setattr(registered_panel_capture, "capture_phase", provider_must_not_run)
    with pytest.raises(RegisteredPanelCaptureError):
        capture_registered_panel(
            path,
            database=tmp_path / "evidence.sqlite",
            effective_date="2026-08-12",
            phase="pre_open",
        )
    assert calls == 0


@pytest.mark.parametrize(
    "schema_version",
    [
        "rqgm-forward-panel-registration/1",
        "rqgm-forward-panel-registration/2",
        "rqgm-forward-panel-registration/3",
    ],
)
def test_legacy_registration_schemas_are_zero_call_byte_identical_gates(
    tmp_path, monkeypatch, schema_version
) -> None:
    path = _registration(tmp_path)
    payload = json.loads(path.read_bytes())
    payload["schema_version"] = schema_version
    if schema_version.endswith("/1"):
        for field in (
            "database_path",
            "prerequisite_files",
            "prerequisites",
            "prerequisites_sha256",
        ):
            payload.pop(field)
    path.write_bytes(_canonical(payload))
    database = tmp_path / "legacy.sqlite"
    ledger = tmp_path / "legacy.sqlite.collector-ledger.jsonl"
    database.write_bytes(b"legacy-database-authority")
    ledger.write_bytes(b"legacy-ledger-authority\n")
    before = (path.read_bytes(), database.read_bytes(), ledger.read_bytes())
    calls = {"legacy": 0, "orchestrator": 0, "popen": 0, "provider": 0}

    def called(name):
        def record(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            return [] if name == "legacy" else ()

        return record

    monkeypatch.setattr(registered_panel_capture, "capture_phase", called("legacy"))
    monkeypatch.setattr(
        registered_panel_capture,
        "execute_registered_collector_phase",
        called("orchestrator"),
    )
    monkeypatch.setattr(continuity.subprocess, "Popen", called("popen"))
    monkeypatch.setattr(
        continuity, "_provider_call", called("provider"), raising=False
    )

    error = None
    try:
        capture_registered_panel(
            path,
            database=database,
            effective_date="2026-08-12",
            phase="pre_open",
        )
    except RegisteredPanelCaptureError as exc:
        error = exc

    assert calls == {"legacy": 0, "orchestrator": 0, "popen": 0, "provider": 0}
    assert isinstance(error, RegisteredPanelCaptureError)
    assert (path.read_bytes(), database.read_bytes(), ledger.read_bytes()) == before


@pytest.mark.parametrize("replaced_authority", ["database", "ledger"])
def test_registered_entry_rejects_database_or_ledger_replacement_without_side_effects(
    tmp_path, monkeypatch, replaced_authority
) -> None:
    prepared = _prepared(tmp_path, monkeypatch)
    authority = prepared[replaced_authority]
    replacement = tmp_path / f"replacement-{authority.name}"
    replacement.write_bytes(authority.read_bytes())
    os.replace(replacement, authority)
    before_ledger = prepared["ledger"].read_bytes()
    calls = {"child": 0, "popen": 0, "provider": 0}

    def forbidden(name):
        def record(*args, **kwargs):
            del args, kwargs
            calls[name] += 1
            raise AssertionError(f"{name} ran after authority replacement")

        return record

    monkeypatch.setattr(
        continuity, "_execute_collector_step_attempt", forbidden("child")
    )
    monkeypatch.setattr(continuity.subprocess, "Popen", forbidden("popen"))
    monkeypatch.setattr(
        continuity, "_provider_call", forbidden("provider"), raising=False
    )

    with pytest.raises(RegisteredPanelCaptureError):
        capture_registered_panel(
            prepared["registration"],
            database=prepared["database"],
            effective_date="2099-01-05",
            phase="pre_open",
        )

    assert calls == {"child": 0, "popen": 0, "provider": 0}
    assert prepared["ledger"].read_bytes() == before_ledger


def test_registered_capture_cli_params() -> None:
    assert build_params(
        [
            "registered-panel-capture",
            "--registration-file", "/tmp/panel.json",
            "--database", "/tmp/evidence.sqlite",
            "--date", "2026-08-12",
            "--phase", "pre_open",
        ]
    ) == {
        "kind": "registered_panel_capture",
        "registration_file": "/tmp/panel.json",
        "database": "/tmp/evidence.sqlite",
        "effective_date": "2026-08-12",
        "phase": "pre_open",
    }


def test_future_registration_cli_params() -> None:
    assert build_params(
        [
            "future-panel-register",
            "--output", "/tmp/registration.json",
            "--database", "/tmp/evidence.sqlite",
            "--panel-file", "/tmp/panel.json",
            "--source-receipt", "/tmp/calendar-receipt.json",
            "--source-receipt", "/tmp/rules-receipt.json",
            "--calendar-file", "/tmp/calendar.json",
            "--calendar-authority", "/tmp/calendar-authority.json",
            "--market-rules-file", "/tmp/rules.json",
            "--market-rules-authority", "/tmp/rules-authority.json",
        ]
    ) == {
        "kind": "future_panel_register",
        "output_file": "/tmp/registration.json",
        "database_file": "/tmp/evidence.sqlite",
        "panel_file": "/tmp/panel.json",
        "source_receipt_files": [
            "/tmp/calendar-receipt.json",
            "/tmp/rules-receipt.json",
        ],
        "calendar_file": "/tmp/calendar.json",
        "calendar_authority_file": "/tmp/calendar-authority.json",
        "market_rules_file": "/tmp/rules.json",
        "market_rules_authority_file": "/tmp/rules-authority.json",
    }


def test_future_prepare_cli_params() -> None:
    assert build_params(
        [
            "future-panel-prepare",
            "--database", "/tmp/evidence.sqlite",
            "--panel-file", "/tmp/panel.json",
        ]
    ) == {
        "kind": "future_panel_prepare",
        "database_file": "/tmp/evidence.sqlite",
        "panel_file": "/tmp/panel.json",
    }
