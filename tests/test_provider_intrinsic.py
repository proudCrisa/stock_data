from __future__ import annotations

from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

from stockdata.adjustment_identity import verify_adjustment_identity
from stockdata.cache import Cache
from stockdata.forward_capture import _bind_cohort
from stockdata.forward_context import (
    SOURCE as CONTEXT_SOURCE,
    CapturedMarketRows,
    capture_forward_context,
)
from stockdata.provider_intrinsic import (
    INTRINSIC_COMPONENTS,
    reconstruct_intrinsic_evidence,
    verify_intrinsic_evidence,
)
from stockdata.rqgm_provider_contract import ProviderArtifactReference


DAY = "2026-08-13"
PANEL = [f"000001.SZ@{DAY}"]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _adjustment(role: str, mode: str, version: str):
    return verify_adjustment_identity(
        {
            "schema_version": f"stockdata-{role}-adjustment-identity/1",
            "price_role": role,
            "source": "fixture",
            "adjustment_mode": mode,
            "adjustment_version": version,
        },
        expected_price_role=role,
    )


def _price_receipt(mode: str, version: str) -> dict[str, object]:
    row = [DAY, "10", "10.2", "9.9", "10.1", "1000"]
    return {
        "observed_at": f"{DAY}T15:01:00+08:00",
        "source": "fixture",
        "request": {"adjustment_mode": mode, "adjustment_version": version},
        "response": {
            "fields": "date,open,high,low,close,volume",
            "rows": [row],
        },
    }


def _database(tmp_path, *, context_overrides: dict[str, object] | None = None):
    cache = Cache(tmp_path / "provider.sqlite")
    for mode, version in (("raw", "fixture-raw-v1"), ("qfq", "fixture-qfq-v1")):
        receipt = _price_receipt(mode, version)
        cache.upsert(
            "000001.SZ",
            [
                {
                    "date": DAY,
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.9,
                    "close": 10.1,
                    "volume": 1000.0,
                    "retrieved_at": receipt["observed_at"],
                    "_capture_receipt": receipt,
                }
            ],
            source="fixture",
            adjustment_mode=mode,
            adjustment_version=version,
            capture_receipts=[receipt],
        )

    _bind_cohort(
        cache,
        {
            "symbols": ["000001.SZ"],
            "start": DAY,
            "source": "fixture",
            "adjustment_mode": "raw",
            "adjustment_version": "fixture-raw-v1",
        },
    )
    observed = datetime(2026, 8, 13, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    row = {
        "symbol": "sz000001",
        "name": "Example",
        "trade": "10.0",
        "volume": 1000,
    }
    row.update(context_overrides or {})
    rows = [row]
    receipt = {
        "observed_at": observed.isoformat(timespec="seconds"),
        "source": CONTEXT_SOURCE,
        "request": {"node": "hs_a"},
        "response": {
            "advertised_count": 1,
            "rows": rows,
            "raw_pages": [json.dumps(rows)],
        },
    }
    capture_forward_context(
        cache,
        DAY,
        fetcher=lambda: CapturedMarketRows(rows, receipt),
        now=observed,
    )
    cache.close()
    return tmp_path / "provider.sqlite"


def _reconstructed(tmp_path):
    database = _database(tmp_path)
    execution = _adjustment("execution", "raw", "fixture-raw-v1")
    signal = _adjustment("signal", "qfq", "fixture-qfq-v1")
    reconstructed = reconstruct_intrinsic_evidence(
        database,
        panel=PANEL,
        execution_adjustment=execution,
        signal_adjustment=signal,
        decision_cutoffs={PANEL[0]: f"{DAY}T09:25:00+08:00"},
    )
    return database, execution, signal, reconstructed


def _references(components):
    return {
        component: ProviderArtifactReference(
            f"stock-data-{component.replace('_', '-')}",
            hashlib.sha256(_canonical(components[component])).hexdigest(),
            f"stockdata-{component.replace('_', '-')}/1",
        )
        for component in INTRINSIC_COMPONENTS
    }


def test_reconstructs_distinct_price_roles_and_predecision_context(tmp_path) -> None:
    database, execution, signal, reconstructed = _reconstructed(tmp_path)

    assert reconstructed.components["execution_prices"][
        "adjustment_identity_sha256"
    ] == execution.identifier
    assert reconstructed.components["signal_prices"][
        "adjustment_identity_sha256"
    ] == signal.identifier
    assert reconstructed.components["decision_context"]["records"][0][
        "available_at"
    ] == f"{DAY}T09:05:00+08:00"

    verdicts = verify_intrinsic_evidence(
        reconstructed,
        claimed_components=reconstructed.components,
        component_references=_references(reconstructed.components),
        bound_source_receipts=reconstructed.source_receipts,
        database_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
    )
    assert all(verdicts[component]["ready"] for component in INTRINSIC_COMPONENTS)
    assert all(verdicts[component]["coverage_count"] == 1 for component in INTRINSIC_COMPONENTS)


def test_raw_context_activity_proxies_do_not_enter_intrinsic_artifact(tmp_path) -> None:
    database = _database(
        tmp_path,
        context_overrides={
            "name": "Example ST",
            "trade": "0",
            "volume": 0,
            "listing_status": "listed",
            "board": "MAIN",
            "is_st": True,
            "is_suspended": True,
            "is_member": False,
        },
    )
    execution = _adjustment("execution", "raw", "fixture-raw-v1")
    signal = _adjustment("signal", "qfq", "fixture-qfq-v1")

    reconstructed = reconstruct_intrinsic_evidence(
        database,
        panel=PANEL,
        execution_adjustment=execution,
        signal_adjustment=signal,
        decision_cutoffs={PANEL[0]: f"{DAY}T09:25:00+08:00"},
    )

    captured_input = reconstructed.components["decision_context"]["records"][0][
        "payload"
    ]["captured_input"]
    assert all(
        field not in captured_input
        for field in (
            "listing_status",
            "board",
            "is_st",
            "is_suspended",
            "is_member",
        )
    )


def test_reverification_from_database_bytes_is_identical(tmp_path) -> None:
    database, execution, signal, reconstructed = _reconstructed(tmp_path)

    replayed = reconstruct_intrinsic_evidence(
        database.read_bytes(),
        panel=PANEL,
        execution_adjustment=execution,
        signal_adjustment=signal,
        decision_cutoffs={PANEL[0]: f"{DAY}T09:25:00+08:00"},
    )

    assert replayed == reconstructed


def test_component_or_source_receipt_drift_fails_closed(tmp_path) -> None:
    database, _, _, reconstructed = _reconstructed(tmp_path)
    references = _references(reconstructed.components)
    claimed = dict(reconstructed.components)
    claimed["execution_prices"] = {
        **claimed["execution_prices"],
        "records": [],
    }
    component_drift = verify_intrinsic_evidence(
        reconstructed,
        claimed_components=claimed,
        component_references=references,
        bound_source_receipts=reconstructed.source_receipts,
        database_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
    )
    assert component_drift["execution_prices"]["ready"] is False
    assert component_drift["execution_prices"]["blockers"] == [
        {"code": "intrinsic_component_byte_mismatch", "count": 1}
    ]

    receipt_id = next(iter(reconstructed.source_receipts))
    missing = dict(reconstructed.source_receipts)
    missing.pop(receipt_id)
    receipt_drift = verify_intrinsic_evidence(
        reconstructed,
        claimed_components=reconstructed.components,
        component_references=references,
        bound_source_receipts=missing,
        database_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
    )
    assert any(
        not receipt_drift[component]["ready"] for component in INTRINSIC_COMPONENTS
    )
