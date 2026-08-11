"""Exact-panel availability evidence for RQGM provider components."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json


AVAILABILITY_RECORDS_SCHEMA = "stockdata-component-availability-records/1"
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


@dataclass(frozen=True)
class VerifiedComponentAvailability:
    artifact_sha256: str
    panel_sha256: str
    panel_size: int
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
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp")
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


def verify_component_availability_records(
    artifact: object,
    *,
    expected_panel_sha256: str,
    expected_panel_size: int,
    expected_decision_cutoffs: Mapping[str, str],
    bound_source_receipt_ids: Sequence[str],
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
    if artifact["schema_version"] != AVAILABILITY_RECORDS_SCHEMA:
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

    records = artifact["records"]
    if not isinstance(records, list):
        raise ValueError("availability records must be a list")
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
        receipt_ids = record["source_receipt_ids"]
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or any(
                _sha256(receipt_id, "source receipt id") != receipt_id
                for receipt_id in receipt_ids
            )
            or receipt_ids != sorted(receipt_ids)
            or len(receipt_ids) != len(set(receipt_ids))
            or not set(receipt_ids).issubset(bound_receipt_set)
        ):
            raise ValueError(
                "availability record source receipts are invalid or unbound"
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
    max_available_at = max(available_times, key=lambda item: item[0])[1]
    return VerifiedComponentAvailability(
        artifact_sha256=hashlib.sha256(_canonical(dict(artifact))).hexdigest(),
        panel_sha256=expected_panel_sha256,
        panel_size=expected_panel_size,
        source_receipt_ids=tuple(sorted(used_receipts)),
        max_available_at=max_available_at,
    )
