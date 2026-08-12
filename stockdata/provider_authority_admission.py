"""Strict admission of signed exact-panel provider component evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json

from .authority import EnrolledTrustRegistry, verify_authority_envelope
from .rqgm_provider_contract import COMPONENT_SCHEMAS, ProviderArtifactReference


SIGNED_COMPONENTS = frozenset(
    {
        "trading_calendar",
        "universe",
        "instrument_status",
        "corporate_actions",
        "market_rules",
    }
)
SOURCE_RECEIPT_SCHEMA = "stockdata-provider-component-source-receipt/1"


@dataclass(frozen=True)
class AdmittedProviderAuthority:
    component: str
    artifact: ProviderArtifactReference
    source_receipt_ids: tuple[str, ...]
    publisher_key_id: str
    trust_root_id: str
    signature_id: str
    authority_envelope: Mapping[str, object]
    available_at_by_panel: Mapping[str, str]
    effective_at_by_panel: Mapping[str, str]
    decision_cutoff_by_panel: Mapping[str, str]

    def readiness_evidence(self) -> dict[str, object]:
        return {
            "ready": True,
            "blockers": [],
            "artifact": self.artifact.to_dict(),
            "source_receipt_ids": list(self.source_receipt_ids),
            "publisher_key_id": self.publisher_key_id,
            "trust_root_id": self.trust_root_id,
            "signature_id": self.signature_id,
            "authority_envelope": dict(self.authority_envelope),
        }


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
        raise ValueError("provider authority artifact is not canonical JSON data") from exc


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical timezone-aware timestamp")
    return value


def _panel_entry(value: object) -> str:
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError("provider authority panel entry must be symbol@YYYY-MM-DD")
    symbol, day = value.split("@")
    if not symbol or date.fromisoformat(day).isoformat() != day:
        raise ValueError("provider authority panel entry must be symbol@YYYY-MM-DD")
    return value


def _component_payload(component: str, payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError(f"{component} record payload must be a non-empty object")
    if component == "trading_calendar":
        if set(payload) != {"decision_cutoff_at", "is_trading_day"}:
            raise ValueError("trading_calendar record payload is incomplete")
        if payload["is_trading_day"] is not True:
            raise ValueError("trading_calendar panel date must be a trading day")
        _timestamp(payload["decision_cutoff_at"], "decision_cutoff_at")
    elif component == "universe":
        if set(payload) != {"is_member", "universe_id"}:
            raise ValueError("universe record payload is incomplete")
        if type(payload["is_member"]) is not bool:
            raise ValueError("universe is_member must be boolean")
        _sha256(payload["universe_id"], "universe_id")
    elif component == "instrument_status":
        if set(payload) != {"is_st", "is_suspended", "listing_status"}:
            raise ValueError("instrument_status record payload is incomplete")
        if payload["listing_status"] not in {"listed", "suspended", "delisted"}:
            raise ValueError("instrument_status listing_status is invalid")
        if type(payload["is_st"]) is not bool or type(payload["is_suspended"]) is not bool:
            raise ValueError("instrument_status flags must be boolean")
    elif component == "corporate_actions":
        if set(payload) != {"events"} or not isinstance(payload["events"], list):
            raise ValueError("corporate_actions record payload is incomplete")
        for event in payload["events"]:
            if not isinstance(event, Mapping) or set(event) != {
                "announcement_at",
                "effective_date",
                "event_id",
                "event_type",
            }:
                raise ValueError("corporate_actions event is incomplete")
            _timestamp(event["announcement_at"], "announcement_at")
            if not isinstance(event["effective_date"], str):
                raise ValueError("corporate_actions effective_date is invalid")
            date.fromisoformat(event["effective_date"])
            _sha256(event["event_id"], "event_id")
            if event["event_type"] not in {
                "cash_dividend",
                "delisting",
                "rights_issue",
                "split",
                "stock_dividend",
                "symbol_change",
            }:
                raise ValueError("corporate_actions event_type is invalid")
    elif component == "market_rules":
        if set(payload) != {
            "board",
            "lot_size",
            "price_limit_policy_id",
            "t_plus_one",
        }:
            raise ValueError("market_rules record payload is incomplete")
        if not isinstance(payload["board"], str) or not payload["board"]:
            raise ValueError("market_rules board is invalid")
        if (
            isinstance(payload["lot_size"], bool)
            or not isinstance(payload["lot_size"], int)
            or payload["lot_size"] < 1
        ):
            raise ValueError("market_rules lot_size is invalid")
        if type(payload["t_plus_one"]) is not bool:
            raise ValueError("market_rules t_plus_one must be boolean")
        _sha256(payload["price_limit_policy_id"], "price_limit_policy_id")
    return payload


def _receipt_bindings(
    value: object, receipt_id: str
) -> tuple[set[tuple[str, str, str]], str]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "bindings",
        "observed_at",
        "response_sha256",
        "source",
    }:
        raise ValueError("provider authority source receipt schema is incomplete")
    if value["schema_version"] != SOURCE_RECEIPT_SCHEMA:
        raise ValueError("provider authority source receipt schema is invalid")
    if not isinstance(value["source"], str) or not value["source"]:
        raise ValueError("provider authority source receipt source is invalid")
    observed_at = _timestamp(value["observed_at"], "source receipt observed_at")
    _sha256(value["response_sha256"], "source receipt response_sha256")
    bindings = value["bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("provider authority source receipt bindings are empty")
    result: list[tuple[str, str, str]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping) or set(binding) != {
            "component",
            "panel_entry",
            "record_sha256",
        }:
            raise ValueError("provider authority source receipt binding is incomplete")
        if binding["component"] not in SIGNED_COMPONENTS:
            raise ValueError("provider authority source receipt component is invalid")
        result.append(
            (
                str(binding["component"]),
                _panel_entry(binding["panel_entry"]),
                _sha256(binding["record_sha256"], "source receipt record_sha256"),
            )
        )
    if result != sorted(result) or len(result) != len(set(result)):
        raise ValueError("provider authority source receipt bindings must be sorted unique")
    if receipt_id != hashlib.sha256(_canonical(dict(value))).hexdigest():
        raise ValueError("provider authority source receipt identity drifted")
    return set(result), observed_at


def admit_signed_component_authority(
    *,
    component: str,
    artifact_value: object,
    authority_envelope: object,
    expected_panel: Sequence[str],
    bound_source_receipts: Mapping[str, object],
    registry: EnrolledTrustRegistry,
    decision_cutoff_by_panel: Mapping[str, str] | None = None,
) -> AdmittedProviderAuthority:
    """Verify semantic coverage and external authority for one signed component."""

    if component not in SIGNED_COMPONENTS:
        raise ValueError("component is not externally signed provider authority")
    if not isinstance(artifact_value, Mapping) or set(artifact_value) != {
        "schema_version",
        "component",
        "panel",
        "records",
    }:
        raise ValueError(f"{component} authority artifact schema is incomplete")
    if artifact_value["schema_version"] != COMPONENT_SCHEMAS[component]:
        raise ValueError(f"{component} authority artifact schema version is invalid")
    if artifact_value["component"] != component:
        raise ValueError(f"{component} authority artifact component drifted")

    panel = artifact_value["panel"]
    normalized_expected = tuple(sorted(_panel_entry(item) for item in expected_panel))
    if (
        not isinstance(panel, list)
        or tuple(panel) != normalized_expected
        or len(panel) != len(set(panel))
    ):
        raise ValueError(f"{component} authority artifact differs exact panel")

    bound_receipts = {
        _sha256(receipt_id, "bound source receipt"): _receipt_bindings(
            value, receipt_id
        )
        for receipt_id, value in bound_source_receipts.items()
    }
    records = artifact_value["records"]
    if not isinstance(records, list) or len(records) != len(normalized_expected):
        raise ValueError(f"{component} authority records do not cover exact panel")
    observed_entries: list[str] = []
    used_receipts: set[str] = set()
    available_at_by_panel: dict[str, str] = {}
    effective_at_by_panel: dict[str, str] = {}
    calendar_cutoffs: dict[str, str] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != {
            "panel_entry",
            "payload",
            "record_sha256",
            "source_receipt_ids",
            "effective_at",
            "available_at",
        }:
            raise ValueError(f"{component} authority record {index} is incomplete")
        entry = _panel_entry(record["panel_entry"])
        observed_entries.append(entry)
        payload = _component_payload(component, record["payload"])
        if _sha256(record["record_sha256"], "record_sha256") != hashlib.sha256(
            _canonical(payload)
        ).hexdigest():
            raise ValueError(f"{component} authority record hash drifted")
        receipt_ids = record["source_receipt_ids"]
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or receipt_ids != sorted(receipt_ids)
            or len(receipt_ids) != len(set(receipt_ids))
        ):
            raise ValueError(f"{component} authority source receipts are invalid")
        normalized_receipts = {
            _sha256(receipt_id, "source_receipt_ids") for receipt_id in receipt_ids
        }
        if not normalized_receipts.issubset(bound_receipts):
            raise ValueError(f"{component} authority uses unbound source receipt")
        binding = (component, entry, str(record["record_sha256"]))
        if any(
            binding not in bound_receipts[receipt_id][0]
            for receipt_id in normalized_receipts
        ):
            raise ValueError(f"{component} authority receipt does not bind record")
        used_receipts.update(normalized_receipts)
        effective_at = datetime.fromisoformat(
            _timestamp(record["effective_at"], "effective_at")
        )
        if effective_at.date().isoformat() != entry.split("@")[1]:
            raise ValueError(f"{component} authority effective date differs panel")
        effective_at_by_panel[entry] = effective_at.isoformat()
        available_times = [
            datetime.fromisoformat(_timestamp(record["available_at"], "available_at")),
            *(
                datetime.fromisoformat(bound_receipts[receipt_id][1])
                for receipt_id in normalized_receipts
            ),
        ]
        available_at_by_panel[entry] = max(available_times).isoformat()
        if component == "trading_calendar":
            calendar_cutoffs[entry] = str(payload["decision_cutoff_at"])

    if tuple(observed_entries) != normalized_expected:
        raise ValueError(f"{component} authority records differ exact panel")
    raw = _canonical(dict(artifact_value))
    artifact = ProviderArtifactReference(
        kind=f"stock-data-{component.replace('_', '-')}",
        identifier=hashlib.sha256(raw).hexdigest(),
        schema_version=COMPONENT_SCHEMAS[component],
    )
    verified = verify_authority_envelope(
        authority_envelope,
        registry=registry,
        expected_component=component,
        expected_artifact=artifact.to_dict(),
        expected_source_receipt_ids=sorted(used_receipts),
    )
    if not isinstance(authority_envelope, Mapping):
        raise ValueError("authority envelope must be an object")
    envelope_available = datetime.fromisoformat(verified.available_at)
    available_at_by_panel = {
        entry: max(datetime.fromisoformat(value), envelope_available).isoformat()
        for entry, value in available_at_by_panel.items()
    }
    admitted = AdmittedProviderAuthority(
        component=component,
        artifact=artifact,
        source_receipt_ids=tuple(sorted(used_receipts)),
        publisher_key_id=verified.publisher_key_id,
        trust_root_id=verified.trust_root_id,
        signature_id=verified.signature_id,
        authority_envelope=dict(authority_envelope),
        available_at_by_panel=available_at_by_panel,
        effective_at_by_panel=effective_at_by_panel,
        decision_cutoff_by_panel=calendar_cutoffs,
    )
    cutoffs = (
        admitted.decision_cutoff_by_panel
        if component == "trading_calendar"
        else decision_cutoff_by_panel
    )
    if cutoffs is None:
        raise ValueError(f"{component} authority requires signed calendar cutoffs")
    require_predecision_authority(admitted, decision_cutoff_by_panel=cutoffs)
    return admitted


def require_predecision_authority(
    authority: AdmittedProviderAuthority,
    *,
    decision_cutoff_by_panel: Mapping[str, str],
) -> None:
    """Reject component records that became available at or after decision time."""

    if set(authority.available_at_by_panel) != set(decision_cutoff_by_panel):
        raise ValueError(f"{authority.component} availability differs exact panel")
    for entry, available_at in authority.available_at_by_panel.items():
        available = datetime.fromisoformat(available_at)
        effective = datetime.fromisoformat(authority.effective_at_by_panel[entry])
        cutoff = datetime.fromisoformat(
            _timestamp(decision_cutoff_by_panel[entry], "decision cutoff")
        )
        if available >= cutoff or effective >= cutoff:
            raise ValueError(f"{authority.component} authority is post-cutoff")


__all__ = [
    "AdmittedProviderAuthority",
    "SIGNED_COMPONENTS",
    "SOURCE_RECEIPT_SCHEMA",
    "admit_signed_component_authority",
    "require_predecision_authority",
]
