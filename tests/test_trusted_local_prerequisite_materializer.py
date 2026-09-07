from __future__ import annotations

import hashlib
import json
import stat
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from stockdata import future_panel_registration, trusted_local_prerequisites
from stockdata.cli import build_params, main
from stockdata.collector_continuity import default_collector_ledger_path
from stockdata.future_panel_registration import (
    prepare_future_collector_database,
    register_future_panel,
)
from stockdata.rqgm_provider_contract import COMPONENT_SCHEMAS

NOW = "2026-08-26T12:34:56+08:00"
SESSIONS = ("2026-09-07", "2026-09-08", "2026-09-09")
SYMBOLS = (
    "000001.SZ",
    "000002.SZ",
    "001001.SZ",
    "002001.SZ",
    "300001.SZ",
    "300002.SZ",
    "301001.SZ",
    "301002.SZ",
    "600000.SH",
    "600001.SH",
    "603000.SH",
    "603001.SH",
)
PANEL = sorted(f"{symbol}@{session}" for symbol in SYMBOLS for session in SESSIONS)
PAST_PANEL = sorted(
    f"{symbol}@{session}"
    for symbol in SYMBOLS
    for session in ("2026-08-24", "2026-08-25", "2026-08-26")
)
OUTPUT_FILES = {
    "trading-calendar.json",
    "trading-calendar-receipt.json",
    "market-rules.json",
    "market-rules-receipt.json",
}
FORBIDDEN_KEY_PARTS = ("sign", "trust", "publisher", "envelope")
EXPECTED_COSTS = {
    "commission_rate": 0.0003,
    "minimum_commission": 5.0,
    "transfer_fee_rate": 0.0,
    "stamp_duty_sell_rate": 0.0005,
    "slippage_model": "OPEN_BPS",
    "slippage_bps": 2.0,
    "slippage_bounds": "BAR_AND_PRICE_LIMITS",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _write_panel(path: Path, panel: list[str] | tuple[str, ...] = PANEL) -> None:
    path.write_bytes(_canonical(list(panel)))


def _calendar_fact(entry: str, *, is_trading_day: bool = True) -> dict[str, object]:
    day = entry.split("@", 1)[1]
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    return {
        "panel_entry": entry,
        "payload": {
            "decision_cutoff_at": f"{day}T09:25:00+08:00",
            "is_trading_day": is_trading_day,
            "next_session_decision_cutoff_at": f"{next_day}T09:25:00+08:00",
            "session_close_at": f"{day}T15:00:00+08:00",
        },
        "effective_at": f"{day}T00:00:00+08:00",
        "available_at": NOW,
    }


def _market_rule_fact(
    entry: str, sessions: tuple[str, ...], *, is_st: bool
) -> dict[str, object]:
    symbol, _ = entry.split("@", 1)
    digits, exchange = symbol.split(".", 1)
    board = (
        "CHINEXT"
        if exchange == "SZ" and digits.startswith(("300", "301"))
        else "MAIN"
    )
    price_limit = 0.20 if board == "CHINEXT" else 0.10 if not is_st else 0.05
    day = entry.split("@", 1)[1]
    return {
        "panel_entry": entry,
        "payload": {
            "schema_version": "stockdata-market-rule-payload/1",
            "policy_id": f"fixture-{board.lower()}-{exchange.lower()}-"
            f"{'st' if is_st else 'nonst'}-{sessions[0]}-{sessions[-1]}",
            "source": "fixture-local-facts",
            "source_sha256": "a" * 64,
            "security_type": "A_SHARE",
            "board": board,
            "exchange": exchange,
            "effective_from": sessions[0],
            "effective_until": sessions[-1],
            "listing_age_min": 0,
            "listing_age_max": None,
            "is_st": is_st,
            "lot_size": 100,
            "t_plus_one": True,
            "reject_suspended": True,
            "reject_zero_volume": True,
            "price_limit_up": price_limit,
            "price_limit_down": price_limit,
            "price_limit_reference": "RECORD_OR_PREVIOUS_CLOSE",
            "price_tick": 0.01,
            "price_rounding": "HALF_UP",
            "locked_limit_order_policy": "REJECT_SIDE",
            "commission_rate": 0.0003,
            "minimum_commission": 5.0,
            "transfer_fee_rate": 0.0,
            "stamp_duty_sell_rate": 0.0005,
            "slippage_model": "OPEN_BPS",
            "slippage_bps": 2.0,
            "slippage_bounds": "BAR_AND_PRICE_LIMITS",
            "time_in_force": "DAY",
            "cancel_unfilled_at_close": True,
        },
        "effective_at": f"{day}T00:00:00+08:00",
        "available_at": NOW,
    }


def _write_facts(
    tmp_path: Path,
    panel: list[str] | tuple[str, ...] = PANEL,
    *,
    holiday: bool = False,
    omit_rule: tuple[str, bool] | None = None,
) -> tuple[Path, Path]:
    entries = sorted(panel)
    sessions = tuple(sorted({entry.split("@", 1)[1] for entry in entries}))
    calendar = {
        "schema_version": "stockdata-trusted-local-calendar-facts/1",
        "panel": entries,
        "records": [
            _calendar_fact(entry, is_trading_day=not holiday) for entry in entries
        ],
    }
    rule_records = [
        _market_rule_fact(entry, sessions, is_st=is_st)
        for entry in entries
        for is_st in (False, True)
        if omit_rule != (entry, is_st)
    ]
    rules = {
        "schema_version": "stockdata-trusted-local-market-rules-facts/1",
        "panel": entries,
        "records": rule_records,
    }
    calendar_path = tmp_path / "calendar-facts.json"
    rules_path = tmp_path / "market-rules-facts.json"
    calendar_path.write_bytes(_canonical(calendar))
    rules_path.write_bytes(_canonical(rules))
    return calendar_path, rules_path


def _materialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object]]:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    monkeypatch.setattr(
        future_panel_registration,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel_file = tmp_path / "future-panel.json"
    _write_panel(panel_file)
    calendar_facts_file, market_rules_facts_file = _write_facts(tmp_path)
    output_dir = tmp_path / "prerequisites"
    result = trusted_local_prerequisites.materialize_trusted_local_prerequisites(
        panel_file=panel_file,
        output_dir=output_dir,
        calendar_facts_file=calendar_facts_file,
        market_rules_facts_file=market_rules_facts_file,
    )
    assert isinstance(result, dict)
    return output_dir, result


def _read_outputs(output_dir: Path) -> dict[str, object]:
    return {
        name: json.loads((output_dir / name).read_bytes())
        for name in sorted(OUTPUT_FILES)
    }


def _walk_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_walk_keys(child))
    return keys


def _receipt_id(receipt: object) -> str:
    return hashlib.sha256(_canonical(receipt)).hexdigest()


def _assert_receipt_binds_records(
    receipt: dict[str, object], artifact: dict[str, object]
) -> None:
    assert set(receipt) == {
        "schema_version",
        "source",
        "observed_at",
        "response_sha256",
        "bindings",
    }
    assert receipt["schema_version"] == "stockdata-provider-component-source-receipt/1"
    assert receipt["observed_at"] == NOW
    assert isinstance(receipt["response_sha256"], str)
    assert len(receipt["response_sha256"]) == 64
    records = artifact["records"]
    bindings = receipt["bindings"]
    assert isinstance(records, list)
    assert isinstance(bindings, list)
    expected_bindings = [
        {
            "component": artifact["component"],
            "panel_entry": record["panel_entry"],
            "record_sha256": record["record_sha256"],
        }
        for record in records
    ]
    assert bindings == sorted(expected_bindings, key=lambda item: (
        item["panel_entry"],
        item["record_sha256"],
    ))
    assert _receipt_id(receipt) in {
        receipt_id
        for record in records
        for receipt_id in record["source_receipt_ids"]
    }


def _assert_artifact_shape(
    artifact: dict[str, object], component: str, expected_record_count: int
) -> None:
    assert set(artifact) == {"schema_version", "component", "panel", "records"}
    assert artifact["schema_version"] == COMPONENT_SCHEMAS[component]
    assert artifact["component"] == component
    assert artifact["panel"] == PANEL
    records = artifact["records"]
    assert isinstance(records, list)
    assert len(records) == expected_record_count
    assert records == sorted(
        records,
        key=lambda record: (record["panel_entry"], record["record_sha256"]),
    )


def test_materializer_writes_four_private_canonical_json_files_with_exact_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, result = _materialize(tmp_path, monkeypatch)
    outputs = _read_outputs(output_dir)

    assert isinstance(result, dict)
    assert {path.name for path in output_dir.iterdir()} == OUTPUT_FILES
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output_dir.iterdir())
    assert set(_walk_keys(outputs))
    assert not any(
        any(part in key.lower() for part in FORBIDDEN_KEY_PARTS)
        for key in _walk_keys(outputs)
    )

    calendar = outputs["trading-calendar.json"]
    calendar_receipt = outputs["trading-calendar-receipt.json"]
    rules = outputs["market-rules.json"]
    rules_receipt = outputs["market-rules-receipt.json"]
    assert isinstance(calendar, dict)
    assert isinstance(calendar_receipt, dict)
    assert isinstance(rules, dict)
    assert isinstance(rules_receipt, dict)
    _assert_artifact_shape(calendar, "trading_calendar", len(PANEL))
    _assert_artifact_shape(rules, "market_rules", len(PANEL) * 2)
    _assert_receipt_binds_records(calendar_receipt, calendar)
    _assert_receipt_binds_records(rules_receipt, rules)

    calendar_receipt_id = _receipt_id(calendar_receipt)
    rules_receipt_id = _receipt_id(rules_receipt)
    for record in calendar["records"]:
        assert record["source_receipt_ids"] == [calendar_receipt_id]
        assert record["available_at"] == NOW
        day = record["panel_entry"].split("@", 1)[1]
        next_day = (
            date.fromisoformat(day) + timedelta(days=1)
        ).isoformat()
        assert record["effective_at"] == f"{day}T00:00:00+08:00"
        assert record["payload"] == {
            "is_trading_day": True,
            "decision_cutoff_at": f"{day}T09:25:00+08:00",
            "session_close_at": f"{day}T15:00:00+08:00",
            "next_session_decision_cutoff_at": f"{next_day}T09:25:00+08:00",
        }
        assert record["record_sha256"] == hashlib.sha256(
            _canonical(record["payload"])
        ).hexdigest()
    assert [
        (record["panel_entry"], record["payload"]["decision_cutoff_at"])
        for record in calendar["records"]
    ] == [(entry, f"{entry.split('@', 1)[1]}T09:25:00+08:00") for entry in PANEL]

    expected_combinations = {
        ("SH", "MAIN"),
        ("SZ", "MAIN"),
        ("SZ", "CHINEXT"),
    }
    grouped_rules: dict[str, list[dict[str, object]]] = {}
    for record in rules["records"]:
        assert record["source_receipt_ids"] == [rules_receipt_id]
        assert record["available_at"] == NOW
        entry = record["panel_entry"]
        symbol, day = entry.split("@", 1)
        exchange = symbol.rsplit(".", 1)[1]
        expected_board = (
            "CHINEXT"
            if exchange == "SZ" and symbol.startswith(("300", "301"))
            else "MAIN"
        )
        payload = record["payload"]
        assert payload["exchange"] == exchange
        assert payload["board"] == expected_board
        assert payload["effective_from"] == SESSIONS[0]
        assert payload["effective_until"] == SESSIONS[-1]
        assert payload["is_st"] in (False, True)
        assert payload["source"] == rules_receipt["source"]
        assert payload["source_sha256"] == rules_receipt["response_sha256"]
        assert record["effective_at"] == f"{day}T00:00:00+08:00"
        assert record["record_sha256"] == hashlib.sha256(
            _canonical(payload)
        ).hexdigest()
        assert payload["policy_id"]
        assert set(EXPECTED_COSTS).issubset(payload)
        for key, expected in EXPECTED_COSTS.items():
            assert payload[key] == expected
        grouped_rules.setdefault(entry, []).append(payload)
    assert {
        (payload["exchange"], payload["board"])
        for records in grouped_rules.values()
        for payload in records
    } == expected_combinations
    assert all({payload["is_st"] for payload in records} == {False, True} for records in grouped_rules.values())
    assert all(len(records) == 2 for records in grouped_rules.values())

    for name, value in outputs.items():
        assert (output_dir / name).read_bytes() == _canonical(value)


def test_materializer_output_registers_as_trusted_local_schema_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, _ = _materialize(tmp_path, monkeypatch)
    panel_file = tmp_path / "future-panel.json"
    database_file = tmp_path / "collector.sqlite"
    registration_file = tmp_path / "registration.json"
    prepare_future_collector_database(
        database_file=database_file,
        panel_file=panel_file,
    )
    monkeypatch.setattr(
        future_panel_registration,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )

    registration = register_future_panel(
        output_file=registration_file,
        database_file=database_file,
        panel_file=panel_file,
        source_receipt_files=[
            output_dir / "trading-calendar-receipt.json",
            output_dir / "market-rules-receipt.json",
        ],
        calendar_file=output_dir / "trading-calendar.json",
        market_rules_file=output_dir / "market-rules.json",
        authority_mode="trusted_local_mechanical",
    )

    assert registration["schema_version"] == "rqgm-forward-panel-registration/5"
    assert registration["authority_mode"] == "trusted_local_mechanical"
    assert registration_file.read_bytes() == _canonical(registration)
    assert registration["workspace_count"] == 36
    assert Path(default_collector_ledger_path(database_file)).is_file()


def test_materializer_requires_explicit_existing_canonical_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel_file = tmp_path / "panel.json"
    _write_panel(panel_file)
    output_dir = tmp_path / "prerequisites"
    calendar_facts_file = tmp_path / "missing-calendar-facts.json"
    market_rules_facts_file = tmp_path / "missing-market-rules-facts.json"

    with pytest.raises(
        trusted_local_prerequisites.TrustedLocalPrerequisitesError,
        match="canonical|calendar|rule|fact|source",
    ):
        trusted_local_prerequisites.materialize_trusted_local_prerequisites(
            panel_file=panel_file,
            output_dir=output_dir,
            calendar_facts_file=calendar_facts_file,
            market_rules_facts_file=market_rules_facts_file,
        )

    assert not output_dir.exists()


def test_materializer_rejects_weekday_holiday_without_canonical_calendar_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel_file = tmp_path / "holiday-panel.json"
    _write_panel(panel_file, ["000001.SZ@2026-10-01"])
    calendar_facts_file, market_rules_facts_file = _write_facts(
        tmp_path, ["000001.SZ@2026-10-01"], holiday=True
    )
    output_dir = tmp_path / "prerequisites"

    with pytest.raises(
        trusted_local_prerequisites.TrustedLocalPrerequisitesError,
        match="calendar|holiday|trading|canonical",
    ):
        trusted_local_prerequisites.materialize_trusted_local_prerequisites(
            panel_file=panel_file,
            output_dir=output_dir,
            calendar_facts_file=calendar_facts_file,
            market_rules_facts_file=market_rules_facts_file,
        )

    assert not output_dir.exists()


def test_materializer_rejects_incomplete_existing_market_rule_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel = ["000001.SZ@2026-09-07"]
    panel_file = tmp_path / "panel.json"
    _write_panel(panel_file, panel)
    calendar_facts_file, market_rules_facts_file = _write_facts(
        tmp_path, panel, omit_rule=(panel[0], True)
    )
    output_dir = tmp_path / "prerequisites"

    with pytest.raises(
        trusted_local_prerequisites.TrustedLocalPrerequisitesError,
        match="coverage|rule|complete|branch",
    ):
        trusted_local_prerequisites.materialize_trusted_local_prerequisites(
            panel_file=panel_file,
            output_dir=output_dir,
            calendar_facts_file=calendar_facts_file,
            market_rules_facts_file=market_rules_facts_file,
        )

    assert not output_dir.exists()


def test_materializer_rejects_existing_calendar_facts_missing_a_panel_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel = ["000001.SZ@2026-09-07", "000002.SZ@2026-09-08"]
    panel_file = tmp_path / "panel.json"
    _write_panel(panel_file, panel)
    calendar_facts_file, market_rules_facts_file = _write_facts(tmp_path, panel)
    calendar = json.loads(calendar_facts_file.read_bytes())
    calendar["records"].pop()
    calendar_facts_file.write_bytes(_canonical(calendar))
    output_dir = tmp_path / "prerequisites"

    with pytest.raises(
        trusted_local_prerequisites.TrustedLocalPrerequisitesError,
        match="coverage|panel|calendar|complete",
    ):
        trusted_local_prerequisites.materialize_trusted_local_prerequisites(
            panel_file=panel_file,
            output_dir=output_dir,
            calendar_facts_file=calendar_facts_file,
            market_rules_facts_file=market_rules_facts_file,
        )

    assert not output_dir.exists()


def test_cli_materializes_without_an_observed_at_argument_from_runtime_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel_file = tmp_path / "panel.json"
    output_dir = tmp_path / "cli-output"
    _write_panel(panel_file)
    cli_args = [
        "future-panel-local-prerequisites",
        "--panel-file",
        str(panel_file),
        "--output-dir",
        str(output_dir),
        "--calendar-facts-file",
        str(tmp_path / "calendar-facts.json"),
        "--market-rules-facts-file",
        str(tmp_path / "market-rules-facts.json"),
    ]
    _write_facts(tmp_path)
    assert build_params(cli_args) == {
        "kind": "future_panel_local_prerequisites",
        "panel_file": str(panel_file),
        "output_dir": str(output_dir),
        "calendar_facts_file": str(tmp_path / "calendar-facts.json"),
        "market_rules_facts_file": str(tmp_path / "market-rules-facts.json"),
    }

    assert main(cli_args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    assert "observed_at" not in cli_result
    assert json.loads(
        (output_dir / "trading-calendar-receipt.json").read_bytes()
    )["observed_at"] == NOW
    assert json.loads(
        (output_dir / "market-rules-receipt.json").read_bytes()
    )["observed_at"] == NOW


@pytest.mark.parametrize(
    "panel_case,panel_bytes",
    [
        ("past", _canonical(PAST_PANEL)),
        ("short", _canonical(PANEL[:-1])),
        ("malformed", b"{malformed-json"),
    ],
)
def test_materializer_rejects_past_or_malformed_panel_without_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    panel_case: str,
    panel_bytes: bytes,
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel_file = tmp_path / "panel.json"
    panel_file.write_bytes(panel_bytes)
    output_dir = tmp_path / "output"
    calendar_facts_file, market_rules_facts_file = _write_facts(tmp_path)

    with pytest.raises((ValueError, OSError)):
        trusted_local_prerequisites.materialize_trusted_local_prerequisites(
            panel_file=panel_file,
            output_dir=output_dir,
            calendar_facts_file=calendar_facts_file,
            market_rules_facts_file=market_rules_facts_file,
        )

    assert not output_dir.exists()


def test_materializer_rejects_existing_output_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trusted_local_prerequisites,
        "_now",
        lambda: datetime.fromisoformat(NOW),
    )
    panel_file = tmp_path / "panel.json"
    _write_panel(panel_file)
    output_dir = tmp_path / "existing-output"
    output_dir.mkdir()
    sentinel = output_dir / "sentinel"
    sentinel.write_bytes(b"keep")
    calendar_facts_file, market_rules_facts_file = _write_facts(tmp_path)

    with pytest.raises((ValueError, FileExistsError, OSError)):
        trusted_local_prerequisites.materialize_trusted_local_prerequisites(
            panel_file=panel_file,
            output_dir=output_dir,
            calendar_facts_file=calendar_facts_file,
            market_rules_facts_file=market_rules_facts_file,
        )

    assert sorted(path.name for path in output_dir.iterdir()) == ["sentinel"]
    assert sentinel.read_bytes() == b"keep"
