"""Exact-panel availability evidence for RQGM provider components."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

AVAILABILITY_RECORDS_SCHEMA = "stockdata-component-availability-records/1"
VERIFIED_AVAILABILITY_RECORDS_SCHEMA = "stockdata-component-availability-records/2"
EVIDENCE_COMPONENTS = (
    "corporate_actions",
    "decision_context",
    "execution_prices",
    "instrument_status",
    "market_rules",
    "signal_prices",
    "trading_calendar",
    "universe",
)
PRICE_COMPONENTS = frozenset({"execution_prices", "signal_prices"})


@dataclass(frozen=True)
class VerifiedComponentAvailability:
    ready: bool
    blockers: tuple[str, ...]
    artifact_sha256: str
    panel_sha256: str
    panel_size: int
    record_count: int
    source_receipt_ids: tuple[str, ...]
    max_available_at: str


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError("availability artifact is not canonical JSON data") from exc


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: object, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 - public verifier contract uses ValueError
            f"{field} must be a canonical timezone-aware timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be a canonical timezone-aware timestamp"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.isoformat() != value
    ):
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp")
    return value, parsed


def _panel_entry(value: object) -> str:
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError("panel entries must use symbol@YYYY-MM-DD identities")
    symbol, day = value.split("@")
    if not symbol or date.fromisoformat(day).isoformat() != day:
        raise ValueError("panel entries must use symbol@YYYY-MM-DD identities")
    return value


def _panel_digest(panel: Sequence[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            list(panel), ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def _corporate_action_announcements(payload: object) -> tuple[datetime, ...]:
    if not isinstance(payload, Mapping) or set(payload) != {"events"}:
        raise ValueError("corporate_actions component payload is incomplete")
    events = payload["events"]
    if not isinstance(events, list):
        raise ValueError("corporate_actions component events are invalid")
    announcements: list[datetime] = []
    for event in events:
        if not isinstance(event, Mapping) or set(event) != {
            "announcement_at",
            "effective_date",
            "event_id",
            "event_type",
        }:
            raise ValueError("corporate_actions component event is incomplete")
        _sha256(event["event_id"], "corporate action event_id")
        if not isinstance(event["effective_date"], str):
            raise ValueError("corporate_actions effective_date is invalid")
        date.fromisoformat(event["effective_date"])
        if event["event_type"] not in {
            "cash_dividend",
            "delisting",
            "rights_issue",
            "split",
            "stock_dividend",
            "symbol_change",
        }:
            raise ValueError("corporate_actions event_type is invalid")
        announcements.append(
            _timestamp(event["announcement_at"], "corporate action announcement_at")[1]
        )
    return tuple(announcements)


def _receipt_ids(
    value: object,
    *,
    bound_receipts: set[str],
    field: str,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(_sha256(receipt_id, field) != receipt_id for receipt_id in value)
        or value != sorted(value)
        or len(value) != len(set(value))
        or not set(value).issubset(bound_receipts)
    ):
        raise ValueError(f"{field} are invalid or unbound")
    return tuple(value)


def _legacy_records(
    records: object,
    *,
    panel: Sequence[str],
    decision_cutoffs: Mapping[str, tuple[str, datetime]],
    bound_receipts: set[str],
) -> tuple[set[str], list[tuple[datetime, str]]]:
    if not isinstance(records, list):
        raise ValueError(  # noqa: TRY004 - retain legacy verifier contract
            "availability records must be a list"
        )
    expected_keys = {
        (component, panel_entry)
        for component in EVIDENCE_COMPONENTS
        for panel_entry in panel
    }
    actual_keys: list[tuple[str, str]] = []
    used_receipts: set[str] = set()
    available_times: list[tuple[datetime, str]] = []
    required_fields = {
        "component",
        "panel_entry",
        "record_sha256",
        "source_receipt_ids",
        "effective_at",
        "available_at",
        "decision_cutoff_at",
    }
    for record in records:
        if not isinstance(record, Mapping) or set(record) != required_fields:
            raise ValueError("availability record schema is incomplete")
        component = record["component"]
        panel_entry = _panel_entry(record["panel_entry"])
        if component not in EVIDENCE_COMPONENTS or panel_entry not in panel:
            raise ValueError("availability record is outside the exact component panel")
        actual_keys.append((component, panel_entry))
        _sha256(record["record_sha256"], "record_sha256")
        receipt_ids = _receipt_ids(
            record["source_receipt_ids"],
            bound_receipts=bound_receipts,
            field="availability record source receipts",
        )
        used_receipts.update(receipt_ids)
        _timestamp(record["effective_at"], "effective_at")
        available_at, available_time = _timestamp(
            record["available_at"], "available_at"
        )
        cutoff_at, cutoff_time = _timestamp(
            record["decision_cutoff_at"], "decision_cutoff_at"
        )
        expected_cutoff_at, expected_cutoff_time = decision_cutoffs[panel_entry]
        if cutoff_at != expected_cutoff_at or cutoff_time != expected_cutoff_time:
            raise ValueError(
                "availability record differs from the authoritative decision cutoff"
            )
        if available_time > cutoff_time:
            raise ValueError("availability record is post-cutoff")
        available_times.append((available_time, available_at))

    if actual_keys != sorted(actual_keys) or len(actual_keys) != len(set(actual_keys)):
        raise ValueError("availability records must be sorted and unique")
    if set(actual_keys) != expected_keys:
        raise ValueError("availability records do not cover the exact component panel")
    return used_receipts, available_times


def _component_record_closure(
    value: object,
    *,
    panel: Sequence[str],
    calendar_phases: Mapping[str, Mapping[str, tuple[str, datetime]]],
    bound_receipts: set[str],
) -> dict[tuple[str, str], dict[str, object]]:
    if not isinstance(value, Mapping) or set(value) != set(EVIDENCE_COMPONENTS):
        raise ValueError("component record set is incomplete")

    closure: dict[tuple[str, str], dict[str, object]] = {}
    required_fields = {
        "panel_entry",
        "payload",
        "record_sha256",
        "source_receipt_ids",
        "effective_at",
        "available_at",
    }
    for component in EVIDENCE_COMPONENTS:
        records = value[component]
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise ValueError(  # noqa: TRY004 - verifier rejects malformed evidence
                f"{component} records must be a list"
            )
        observed_entries: list[str] = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) != required_fields:
                raise ValueError(f"{component} component record schema is incomplete")
            panel_entry = _panel_entry(record["panel_entry"])
            observed_entries.append(panel_entry)
            if panel_entry not in panel:
                raise ValueError("component record is outside the exact panel")
            payload = record["payload"]
            record_sha256 = _sha256(record["record_sha256"], "record_sha256")
            recomputed_sha256 = hashlib.sha256(_canonical(payload)).hexdigest()
            if record_sha256 != recomputed_sha256:
                raise ValueError(f"{component} component record canonical hash drifted")
            receipt_ids = _receipt_ids(
                record["source_receipt_ids"],
                bound_receipts=bound_receipts,
                field=f"{component} component record source receipts",
            )
            event_at, _ = _timestamp(record["effective_at"], "effective_at")
            available_at, _ = _timestamp(
                record["available_at"], "available_at"
            )
            closure[(component, panel_entry)] = {
                "record_sha256": record_sha256,
                "source_receipt_ids": receipt_ids,
                "event_at": event_at,
                "available_at": available_at,
            }
            if component == "corporate_actions":
                closure[(component, panel_entry)]["announcement_times"] = (
                    _corporate_action_announcements(payload)
                )
            if component == "trading_calendar":
                if not isinstance(payload, Mapping):
                    raise ValueError("trading calendar payload is incomplete")
                phase_fields = {
                    "decision_cutoff_at",
                    "session_close_at",
                    "next_session_decision_cutoff_at",
                }
                if not phase_fields.issubset(payload):
                    raise ValueError("trading calendar phase cutoffs are incomplete")
                if any(
                    _timestamp(payload[field], field)
                    != calendar_phases[panel_entry][field]
                    for field in phase_fields
                ):
                    raise ValueError(
                        "trading calendar phase differs from signed calendar"
                    )

        if (
            observed_entries != sorted(observed_entries)
            or len(observed_entries) != len(set(observed_entries))
        ):
            raise ValueError(f"{component} component records must be sorted and unique")
        if set(observed_entries) != set(panel):
            raise ValueError(f"{component} component records do not cover the exact panel")
    return closure


def _signed_calendar_phases(
    value: object,
    *,
    panel: Sequence[str],
    decision_cutoffs: Mapping[str, tuple[str, datetime]],
) -> dict[str, dict[str, tuple[str, datetime]]]:
    if not isinstance(value, Mapping) or set(value) != set(panel):
        raise ValueError("signed calendar phases do not cover the exact panel")
    required_fields = {
        "decision_cutoff_at",
        "session_close_at",
        "next_session_decision_cutoff_at",
    }
    result: dict[str, dict[str, tuple[str, datetime]]] = {}
    for panel_entry in panel:
        phase = value[panel_entry]
        if not isinstance(phase, Mapping) or set(phase) != required_fields:
            raise ValueError("signed calendar phase schema is incomplete")
        parsed = {field: _timestamp(phase[field], field) for field in required_fields}
        if parsed["decision_cutoff_at"] != decision_cutoffs[panel_entry]:
            raise ValueError(
                "signed calendar differs from the authoritative decision cutoff"
            )
        panel_day = date.fromisoformat(panel_entry.split("@")[1])
        decision_time = parsed["decision_cutoff_at"][1]
        close_time = parsed["session_close_at"][1]
        next_cutoff_time = parsed["next_session_decision_cutoff_at"][1]
        if (
            decision_time.date() != panel_day
            or close_time.date() != panel_day
            or next_cutoff_time.date() <= panel_day
            or not decision_time < close_time < next_cutoff_time
        ):
            raise ValueError("signed calendar phase order is invalid")
        result[panel_entry] = parsed
    return result


def _verified_records(
    records: object,
    *,
    panel: Sequence[str],
    closure: Mapping[tuple[str, str], Mapping[str, object]],
    calendar_phases: Mapping[str, Mapping[str, tuple[str, datetime]]],
    bound_receipts: set[str],
) -> tuple[set[str], list[tuple[datetime, str]]]:
    if not isinstance(records, list):
        raise ValueError(  # noqa: TRY004 - verifier rejects malformed evidence
            "availability records must be a list"
        )
    expected_keys = set(closure)
    actual_keys: list[tuple[str, str]] = []
    used_receipts: set[str] = set()
    available_times: list[tuple[datetime, str]] = []
    required_fields = {
        "component",
        "panel_entry",
        "record_sha256",
        "source_receipt_ids",
        "event_at",
        "available_at",
        "cutoff_kind",
        "applicable_cutoff_at",
    }
    for record in records:
        if not isinstance(record, Mapping) or set(record) != required_fields:
            raise ValueError("availability record schema is incomplete")
        component = record["component"]
        panel_entry = _panel_entry(record["panel_entry"])
        if component not in EVIDENCE_COMPONENTS or panel_entry not in panel:
            raise ValueError("availability record is outside the exact component panel")
        key = (str(component), panel_entry)
        actual_keys.append(key)
        bound_record = closure[key]
        record_sha256 = _sha256(record["record_sha256"], "record_sha256")
        if record_sha256 != bound_record["record_sha256"]:
            raise ValueError("availability record differs from the bound component record")
        receipt_ids = _receipt_ids(
            record["source_receipt_ids"],
            bound_receipts=bound_receipts,
            field="availability record source receipts",
        )
        if receipt_ids != bound_record["source_receipt_ids"]:
            raise ValueError("availability record source receipts differ from component")
        event_at, _ = _timestamp(record["event_at"], "event_at")
        if event_at != bound_record["event_at"]:
            raise ValueError("availability record event time differs from component")
        available_at, available_time = _timestamp(
            record["available_at"], "available_at"
        )
        if available_at != bound_record["available_at"]:
            raise ValueError("availability record availability time differs from component")

        phases = calendar_phases[panel_entry]
        expected_cutoff_kind = (
            "next_session_decision_cutoff_at"
            if component in PRICE_COMPONENTS
            else "decision_cutoff_at"
        )
        if record["cutoff_kind"] != expected_cutoff_kind:
            raise ValueError("availability record cutoff kind is forged")
        cutoff_at, cutoff_time = _timestamp(
            record["applicable_cutoff_at"], "applicable_cutoff_at"
        )
        if cutoff_at != phases[expected_cutoff_kind][0]:
            raise ValueError("availability record applicable cutoff is forged")
        if component in PRICE_COMPONENTS:
            session_close = phases["session_close_at"][1]
            if available_time < session_close:
                raise ValueError("price availability precedes session close")
            if available_time >= cutoff_time:
                raise ValueError("availability record is post-cutoff")
        elif component == "corporate_actions":
            announcements = bound_record.get("announcement_times")
            if (
                not isinstance(announcements, tuple)
                or available_time >= cutoff_time
                or any(
                    not isinstance(announcement, datetime)
                    or announcement >= cutoff_time
                    for announcement in announcements
                )
            ):
                raise ValueError("corporate action availability is post-cutoff")
        elif available_time > cutoff_time:
            raise ValueError("availability record is post-cutoff")
        used_receipts.update(receipt_ids)
        available_times.append((available_time, available_at))

    if actual_keys != sorted(actual_keys) or len(actual_keys) != len(set(actual_keys)):
        raise ValueError("availability records must be sorted and unique")
    if set(actual_keys) != expected_keys:
        raise ValueError("availability records do not cover the exact component panel")
    return used_receipts, available_times


def verify_component_availability_records(
    artifact: object,
    *,
    expected_panel_sha256: str,
    expected_panel_size: int,
    expected_decision_cutoffs: Mapping[str, str],
    bound_source_receipt_ids: Sequence[str],
    component_records: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    expected_signed_calendar_phases: Mapping[str, Mapping[str, str]] | None = None,
) -> VerifiedComponentAvailability:
    """Verify complete per-component PIT lineage for one exact panel."""

    expected_panel_sha256 = _sha256(
        expected_panel_sha256, "expected_panel_sha256"
    )
    if (
        isinstance(expected_panel_size, bool)
        or not isinstance(expected_panel_size, int)
        or expected_panel_size < 1
    ):
        raise ValueError("expected_panel_size must be a positive integer")
    bound_receipts = tuple(bound_source_receipt_ids)
    if (
        not bound_receipts
        or any(
            _sha256(receipt_id, "bound source receipt id") != receipt_id
            for receipt_id in bound_receipts
        )
        or bound_receipts != tuple(sorted(bound_receipts))
        or len(bound_receipts) != len(set(bound_receipts))
    ):
        raise ValueError("bound source receipts must be non-empty, sorted, and unique")
    bound_receipt_set = set(bound_receipts)

    if not isinstance(artifact, Mapping) or set(artifact) != {
        "schema_version",
        "panel",
        "records",
    }:
        raise ValueError("availability artifact schema is incomplete")
    schema_version = artifact["schema_version"]
    if schema_version not in {
        AVAILABILITY_RECORDS_SCHEMA,
        VERIFIED_AVAILABILITY_RECORDS_SCHEMA,
    }:
        raise ValueError("unsupported availability artifact schema")
    panel = artifact["panel"]
    if (
        not isinstance(panel, list)
        or not panel
        or any(_panel_entry(item) != item for item in panel)
        or panel != sorted(panel)
        or len(panel) != len(set(panel))
    ):
        raise ValueError("exact panel must be non-empty, sorted, and unique")
    if len(panel) != expected_panel_size or _panel_digest(panel) != expected_panel_sha256:
        raise ValueError("availability artifact differs from the exact panel")
    if not isinstance(expected_decision_cutoffs, Mapping) or set(
        expected_decision_cutoffs
    ) != set(panel):
        raise ValueError("decision cutoffs do not cover the exact panel")
    decision_cutoffs: dict[str, tuple[str, datetime]] = {}
    for panel_entry in panel:
        decision_cutoffs[panel_entry] = _timestamp(
            expected_decision_cutoffs[panel_entry],
            f"decision cutoff for {panel_entry}",
        )

    if schema_version == AVAILABILITY_RECORDS_SCHEMA:
        used_receipts, available_times = _legacy_records(
            artifact["records"],
            panel=panel,
            decision_cutoffs=decision_cutoffs,
            bound_receipts=bound_receipt_set,
        )
        ready = False
        blockers: tuple[str, ...] = ("legacy_schema_without_record_closure",)
    else:
        calendar_phases = _signed_calendar_phases(
            expected_signed_calendar_phases,
            panel=panel,
            decision_cutoffs=decision_cutoffs,
        )
        closure = _component_record_closure(
            component_records,
            panel=panel,
            calendar_phases=calendar_phases,
            bound_receipts=bound_receipt_set,
        )
        used_receipts, available_times = _verified_records(
            artifact["records"],
            panel=panel,
            closure=closure,
            calendar_phases=calendar_phases,
            bound_receipts=bound_receipt_set,
        )
        # Exact closure is bidirectional: every bound source receipt must be
        # consumed by at least one component record. An unused ("orphan")
        # receipt can carry arbitrary or post-hoc bindings and must never
        # ride along inside a ready bundle.
        unconsumed = bound_receipt_set - used_receipts
        ready = not unconsumed
        blockers = ("source_receipt_not_consumed",) if unconsumed else ()
    max_available_at = max(available_times, key=lambda item: item[0])[1]
    return VerifiedComponentAvailability(
        ready=ready,
        blockers=blockers,
        artifact_sha256=hashlib.sha256(_canonical(dict(artifact))).hexdigest(),
        panel_sha256=expected_panel_sha256,
        panel_size=expected_panel_size,
        record_count=len(artifact["records"]),
        source_receipt_ids=tuple(sorted(used_receipts)),
        max_available_at=max_available_at,
    )
