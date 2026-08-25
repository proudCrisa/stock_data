from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from stockdata.component_availability import (
    EVIDENCE_COMPONENTS,
    VERIFIED_AVAILABILITY_RECORDS_SCHEMA,
    verify_component_availability_records,
)

PANEL = ["000001.SZ@2026-01-02"]
RECEIPT_ID = hashlib.sha256(b"source-response").hexdigest()
DECISION_CUTOFF = "2026-01-02T09:25:00+08:00"
SESSION_CLOSE = "2026-01-02T15:00:00+08:00"
NEXT_SESSION_CUTOFF = "2026-01-05T09:25:00+08:00"


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _corporate_action_payload(panel_entry: str) -> dict[str, object]:
    day = panel_entry.split("@")[1]
    return {
        "events": [
            {
                "announcement_at": f"{day}T08:00:00+08:00",
                "effective_date": day,
                "event_id": hashlib.sha256(
                    f"corporate-action:{panel_entry}".encode("ascii")
                ).hexdigest(),
                "event_type": "cash_dividend",
            }
        ]
    }


def _component_records() -> dict[str, list[dict[str, object]]]:
    records: dict[str, list[dict[str, object]]] = {}
    for component in EVIDENCE_COMPONENTS:
        payload: dict[str, object] = (
            _corporate_action_payload(PANEL[0])
            if component == "corporate_actions"
            else {"value": f"{component}:{PANEL[0]}"}
        )
        if component == "trading_calendar":
            payload.update(
                {
                    "decision_cutoff_at": DECISION_CUTOFF,
                    "is_trading_day": True,
                    "next_session_decision_cutoff_at": NEXT_SESSION_CUTOFF,
                    "session_close_at": SESSION_CLOSE,
                }
            )
        available_at = (
            SESSION_CLOSE
            if component in {"execution_prices", "signal_prices"}
            else "2026-01-02T09:00:00+08:00"
        )
        records[component] = [
            {
                "panel_entry": PANEL[0],
                "payload": payload,
                "record_sha256": _canonical_sha256(payload),
                "source_receipt_ids": [RECEIPT_ID],
                "effective_at": "2026-01-02T00:00:00+08:00",
                "available_at": available_at,
            }
        ]
    return records


def _artifact(
    component_records: dict[str, list[dict[str, object]]],
    signed_calendar_phases: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    phases = signed_calendar_phases or _signed_calendar_phases()
    rows = []
    for component in EVIDENCE_COMPONENTS:
        for record in component_records[component]:
            panel_entry = record["panel_entry"]
            is_price = component in {"execution_prices", "signal_prices"}
            cutoff_kind = (
                "next_session_decision_cutoff_at"
                if is_price
                else "decision_cutoff_at"
            )
            rows.append(
                {
                    "component": component,
                    "panel_entry": panel_entry,
                    "record_sha256": record["record_sha256"],
                    "source_receipt_ids": record["source_receipt_ids"],
                    "event_at": record["effective_at"],
                    "available_at": record["available_at"],
                    "cutoff_kind": cutoff_kind,
                    "applicable_cutoff_at": phases[panel_entry][cutoff_kind],
                }
            )
    return {
        "schema_version": VERIFIED_AVAILABILITY_RECORDS_SCHEMA,
        "panel": sorted(phases),
        "records": rows,
    }


def _signed_calendar_phases() -> dict[str, dict[str, str]]:
    return {
        PANEL[0]: {
            "decision_cutoff_at": DECISION_CUTOFF,
            "session_close_at": SESSION_CLOSE,
            "next_session_decision_cutoff_at": NEXT_SESSION_CUTOFF,
        }
    }


def _verify(
    artifact: object,
    component_records: object,
    signed_calendar_phases: object | None = None,
):
    phases = (
        _signed_calendar_phases()
        if signed_calendar_phases is None
        else signed_calendar_phases
    )
    panel = sorted(phases)
    return verify_component_availability_records(
        artifact,
        expected_panel_sha256=_canonical_sha256(panel),
        expected_panel_size=len(panel),
        expected_decision_cutoffs={
            panel_entry: phases[panel_entry]["decision_cutoff_at"]
            for panel_entry in panel
        },
        bound_source_receipt_ids=[RECEIPT_ID],
        component_records=component_records,
        expected_signed_calendar_phases=phases,
    )


def test_verified_schema_closes_over_all_eight_component_records() -> None:
    component_records = _component_records()

    verified = _verify(_artifact(component_records), component_records)

    assert verified.ready is True
    assert verified.blockers == ()
    assert verified.record_count == len(EVIDENCE_COMPONENTS)


def test_legacy_schema_is_compatible_but_blocked() -> None:
    component_records = _component_records()
    artifact = _artifact(component_records)
    artifact["schema_version"] = "stockdata-component-availability-records/1"
    for row in artifact["records"]:
        row["effective_at"] = row.pop("event_at")
        row.pop("applicable_cutoff_at")
        row["decision_cutoff_at"] = DECISION_CUTOFF
        row["available_at"] = "2026-01-02T09:00:00+08:00"
        row.pop("cutoff_kind")

    verified = _verify(artifact, None)

    assert verified.ready is False
    assert verified.blockers == ("legacy_schema_without_record_closure",)


@pytest.mark.parametrize("mutation", ["orphan", "duplicate", "missing"])
def test_availability_requires_exact_one_to_one_closure(mutation: str) -> None:
    component_records = _component_records()
    artifact = _artifact(component_records)
    rows = artifact["records"]
    if mutation == "orphan":
        rows[0]["component"] = "availability_records"
        message = "outside the exact component panel"
    elif mutation == "duplicate":
        rows.append(deepcopy(rows[-1]))
        message = "sorted and unique"
    else:
        rows.pop()
        message = "do not cover"

    with pytest.raises(ValueError, match=message):
        _verify(artifact, component_records)


def test_unrelated_hash_cannot_close_a_real_component_record() -> None:
    component_records = _component_records()
    artifact = _artifact(component_records)
    artifact["records"][0]["record_sha256"] = hashlib.sha256(b"unrelated").hexdigest()

    with pytest.raises(ValueError, match="differs from the bound component record"):
        _verify(artifact, component_records)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_receipt_ids", [hashlib.sha256(b"other").hexdigest()], "source receipts"),
        ("event_at", "2026-01-01T00:00:00+08:00", "event time"),
        ("available_at", "2026-01-02T08:59:59+08:00", "availability time"),
    ],
)
def test_availability_lineage_fields_must_match_the_component_record(
    field: str,
    value: object,
    message: str,
) -> None:
    component_records = _component_records()
    artifact = _artifact(component_records)
    artifact["records"][0][field] = value

    with pytest.raises(ValueError, match=message):
        _verify(artifact, component_records)


def test_component_record_hash_is_recomputed_from_canonical_payload() -> None:
    component_records = _component_records()
    component_records["corporate_actions"][0]["payload"] = {"changed": True}

    with pytest.raises(ValueError, match="canonical hash drifted"):
        _verify(_artifact(component_records), component_records)


@pytest.mark.parametrize(
    ("component", "cutoff_kind", "cutoff", "message"),
    [
        (
            "execution_prices",
            "decision_cutoff_at",
            DECISION_CUTOFF,
            "cutoff kind is forged",
        ),
        (
            "signal_prices",
            "next_session_decision_cutoff_at",
            "2026-01-05T09:26:00+08:00",
            "applicable cutoff is forged",
        ),
        (
            "universe",
            "next_session_decision_cutoff_at",
            NEXT_SESSION_CUTOFF,
            "cutoff kind is forged",
        ),
    ],
)
def test_cutoff_kind_and_value_are_derived_from_signed_calendar(
    component: str,
    cutoff_kind: str,
    cutoff: str,
    message: str,
) -> None:
    component_records = _component_records()
    artifact = _artifact(component_records)
    row = next(item for item in artifact["records"] if item["component"] == component)
    row["cutoff_kind"] = cutoff_kind
    row["applicable_cutoff_at"] = cutoff

    with pytest.raises(ValueError, match=message):
        _verify(artifact, component_records)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_close_at", "2026-01-01T15:00:00+08:00"),
        ("next_session_decision_cutoff_at", "2026-01-02T16:00:00+08:00"),
    ],
)
def test_signed_calendar_phases_must_match_the_session_days(
    field: str,
    value: str,
) -> None:
    component_records = _component_records()
    calendar = component_records["trading_calendar"][0]
    calendar["payload"][field] = value
    calendar["record_sha256"] = _canonical_sha256(calendar["payload"])
    artifact = _artifact(component_records)
    signed_calendar_phases = _signed_calendar_phases()
    signed_calendar_phases[PANEL[0]][field] = value

    with pytest.raises(ValueError, match="phase order is invalid"):
        _verify(artifact, component_records, signed_calendar_phases)


def test_calendar_record_and_availability_cannot_jointly_forge_signed_cutoff() -> None:
    component_records = _component_records()
    calendar = component_records["trading_calendar"][0]
    forged_cutoff = "2026-01-05T09:26:00+08:00"
    calendar["payload"]["next_session_decision_cutoff_at"] = forged_cutoff
    calendar["record_sha256"] = _canonical_sha256(calendar["payload"])
    artifact = _artifact(component_records)
    price_row = next(
        row for row in artifact["records"] if row["component"] == "execution_prices"
    )
    price_row["applicable_cutoff_at"] = forged_cutoff

    with pytest.raises(ValueError, match="differs from signed calendar"):
        _verify(artifact, component_records)


@pytest.mark.parametrize(
    ("available_at", "message"),
    [
        ("2026-01-02T14:59:59+08:00", "precedes session close"),
        (NEXT_SESSION_CUTOFF, "post-cutoff"),
    ],
)
def test_price_availability_uses_close_to_next_session_window(
    available_at: str,
    message: str,
) -> None:
    component_records = _component_records()
    component_records["execution_prices"][0]["available_at"] = available_at
    artifact = _artifact(component_records)

    with pytest.raises(ValueError, match=message):
        _verify(artifact, component_records)


def test_price_may_become_available_during_closed_days_before_next_cutoff() -> None:
    component_records = _component_records()
    component_records["execution_prices"][0]["available_at"] = (
        "2026-01-03T12:00:00+08:00"
    )

    assert _verify(_artifact(component_records), component_records).ready is True


def test_non_price_component_still_uses_same_session_decision_cutoff() -> None:
    component_records = _component_records()
    component_records["universe"][0]["available_at"] = "2026-01-02T09:25:01+08:00"

    with pytest.raises(ValueError, match="post-cutoff"):
        _verify(_artifact(component_records), component_records)


@pytest.mark.parametrize(
    "announcement_at",
    [DECISION_CUTOFF, "2026-01-02T09:25:01+08:00"],
)
def test_corporate_action_announcement_at_cutoff_is_rejected(
    announcement_at: str,
) -> None:
    component_records = _component_records()
    payload = component_records["corporate_actions"][0]["payload"]
    assert isinstance(payload, dict)
    event = payload["events"][0]
    assert isinstance(event, dict)
    event["announcement_at"] = announcement_at
    component_records["corporate_actions"][0]["record_sha256"] = _canonical_sha256(
        payload
    )

    assert component_records["corporate_actions"][0]["effective_at"] == (
        "2026-01-02T00:00:00+08:00"
    )
    with pytest.raises(ValueError, match="corporate action availability is post-cutoff"):
        _verify(_artifact(component_records), component_records)


@pytest.mark.parametrize(
    "available_at",
    [DECISION_CUTOFF, "2026-01-02T09:25:01+08:00"],
)
def test_corporate_action_record_available_at_cutoff_is_rejected(
    available_at: str,
) -> None:
    component_records = _component_records()
    component_records["corporate_actions"][0]["available_at"] = available_at

    with pytest.raises(ValueError, match="corporate action availability is post-cutoff"):
        _verify(_artifact(component_records), component_records)


@pytest.mark.parametrize("mutation", ["missing_component", "duplicate_record"])
def test_actual_component_records_must_also_form_an_exact_closure(mutation: str) -> None:
    component_records = _component_records()
    if mutation == "missing_component":
        component_records.pop("market_rules")
        message = "component record set is incomplete"
    else:
        component_records["market_rules"].append(
            deepcopy(component_records["market_rules"][0])
        )
        message = "records must be sorted and unique"

    with pytest.raises(ValueError, match=message):
        _verify(_artifact(_component_records()), component_records)


def test_two_panel_entries_cannot_cross_bind_component_hashes() -> None:
    component_records = _component_records()
    second_entry = "600000.SH@2026-01-06"
    second_phases = {
        "decision_cutoff_at": "2026-01-06T09:25:00+08:00",
        "session_close_at": "2026-01-06T15:00:00+08:00",
        "next_session_decision_cutoff_at": "2026-01-07T09:25:00+08:00",
    }
    phases = {**_signed_calendar_phases(), second_entry: second_phases}
    for component in EVIDENCE_COMPONENTS:
        second = deepcopy(component_records[component][0])
        second["panel_entry"] = second_entry
        second["effective_at"] = "2026-01-06T00:00:00+08:00"
        second["available_at"] = (
            second_phases["session_close_at"]
            if component in {"execution_prices", "signal_prices"}
            else "2026-01-06T09:00:00+08:00"
        )
        second["payload"] = {"value": f"{component}:{second_entry}"}
        if component == "trading_calendar":
            second["payload"].update({**second_phases, "is_trading_day": True})
        elif component == "corporate_actions":
            second["payload"] = _corporate_action_payload(second_entry)
        second["record_sha256"] = _canonical_sha256(second["payload"])
        component_records[component].append(second)
    artifact = _artifact(component_records, phases)

    assert _verify(artifact, component_records, phases).ready is True

    price_rows = [
        row for row in artifact["records"] if row["component"] == "execution_prices"
    ]
    price_rows[0]["record_sha256"], price_rows[1]["record_sha256"] = (
        price_rows[1]["record_sha256"],
        price_rows[0]["record_sha256"],
    )
    with pytest.raises(ValueError, match="differs from the bound component record"):
        _verify(artifact, component_records, phases)
