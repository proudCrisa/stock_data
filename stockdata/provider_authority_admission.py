"""Strict admission of signed exact-panel provider component evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .authority import EnrolledTrustRegistry, verify_authority_envelope
from .market_rules import (
    validate_market_rule_payload,
    validate_market_rule_regimes,
)
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
GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA = (
    "stockdata-preregistered-generic-market-rulebook/1"
)


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
    payload_by_panel: Mapping[str, Mapping[str, object]]
    decision_cutoff_by_panel: Mapping[str, str]
    signed_calendar_phases_by_panel: Mapping[str, Mapping[str, str]]
    instrument_status_artifact_sha256: str | None = None

    def readiness_evidence(self) -> dict[str, object]:
        evidence: dict[str, object] = {
            "ready": True,
            "blockers": [],
            "artifact": self.artifact.to_dict(),
            "source_receipt_ids": list(self.source_receipt_ids),
            "publisher_key_id": self.publisher_key_id,
            "trust_root_id": self.trust_root_id,
            "signature_id": self.signature_id,
            "authority_envelope": dict(self.authority_envelope),
            "available_at_by_panel": dict(self.available_at_by_panel),
            "effective_at_by_panel": dict(self.effective_at_by_panel),
            "decision_cutoff_by_panel": dict(self.decision_cutoff_by_panel),
        }
        if self.component == "market_rules":
            if self.instrument_status_artifact_sha256 is None:
                raise ValueError(
                    "market_rules authority lacks instrument-status binding"
                )
            evidence["execution_rule_selection"] = {
                "rulebook_artifact_sha256": self.artifact.identifier,
                "instrument_status_artifact_sha256": (
                    self.instrument_status_artifact_sha256
                ),
                "decision_cutoff_by_panel": dict(self.decision_cutoff_by_panel),
                "selected_policy_id_by_panel": {
                    entry: str(payload["policy_id"])
                    for entry, payload in self.payload_by_panel.items()
                },
            }
        return evidence


@dataclass(frozen=True)
class PreregisteredGenericMarketRulebook:
    """Signed generic coverage that is not an execution readiness authority."""

    artifact: ProviderArtifactReference
    source_receipt_ids: tuple[str, ...]
    publisher_key_id: str
    trust_root_id: str
    signature_id: str
    authority_envelope: Mapping[str, object]
    available_at_by_panel: Mapping[str, str]
    effective_at_by_panel: Mapping[str, str]
    decision_cutoff_by_panel: Mapping[str, str]
    policy_ids_by_panel: Mapping[str, tuple[str, ...]]

    def prerequisite_evidence(self) -> dict[str, object]:
        """Serialize only a registration prerequisite, never readiness evidence."""

        return {
            "schema_version": GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA,
            "artifact_sha256": self.artifact.identifier,
            "source_receipt_ids": list(self.source_receipt_ids),
            "publisher_key_id": self.publisher_key_id,
            "trust_root_id": self.trust_root_id,
            "signature_sha256": self.signature_id,
            "authority_envelope": dict(self.authority_envelope),
            "available_at_by_panel": dict(self.available_at_by_panel),
            "effective_at_by_panel": dict(self.effective_at_by_panel),
            "decision_cutoff_by_panel": dict(self.decision_cutoff_by_panel),
            "policy_ids_by_panel": {
                entry: list(policy_ids)
                for entry, policy_ids in self.policy_ids_by_panel.items()
            },
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


def _component_payload(
    component: str,
    payload: object,
    *,
    panel_entry: str | None = None,
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError(f"{component} record payload must be a non-empty object")
    if component == "trading_calendar":
        if set(payload) != {
            "decision_cutoff_at",
            "is_trading_day",
            "next_session_decision_cutoff_at",
            "session_close_at",
        }:
            raise ValueError("trading_calendar record payload is incomplete")
        if payload["is_trading_day"] is not True:
            raise ValueError("trading_calendar panel date must be a trading day")
        if panel_entry is None:
            raise ValueError("trading_calendar payload requires a panel entry")
        panel_day = date.fromisoformat(panel_entry.split("@")[1])
        decision_time = datetime.fromisoformat(
            _timestamp(payload["decision_cutoff_at"], "decision_cutoff_at")
        )
        close_time = datetime.fromisoformat(
            _timestamp(payload["session_close_at"], "session_close_at")
        )
        next_cutoff_time = datetime.fromisoformat(
            _timestamp(
                payload["next_session_decision_cutoff_at"],
                "next_session_decision_cutoff_at",
            )
        )
        if (
            decision_time.date() != panel_day
            or close_time.date() != panel_day
            or next_cutoff_time.date() <= panel_day
            or not decision_time < close_time < next_cutoff_time
        ):
            raise ValueError("trading_calendar session phase order is invalid")
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
        validate_market_rule_payload(payload, panel_entry=panel_entry)
    return payload


def _receipt_bindings(
    value: object, receipt_id: str
) -> tuple[set[tuple[str, str, str]], str, str, str]:
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
    source = value["source"]
    observed_at = _timestamp(value["observed_at"], "source receipt observed_at")
    response_sha256 = _sha256(
        value["response_sha256"], "source receipt response_sha256"
    )
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
    return set(result), observed_at, source, response_sha256


def admit_signed_component_authority(
    *,
    component: str,
    artifact_value: object,
    authority_envelope: object,
    expected_panel: Sequence[str],
    bound_source_receipts: Mapping[str, object],
    registry: EnrolledTrustRegistry,
    decision_cutoff_by_panel: Mapping[str, str] | None = None,
    instrument_status_authority: AdmittedProviderAuthority | None = None,
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

    bound_receipts: dict[
        str, tuple[set[tuple[str, str, str]], str, str, str]
    ] = {}
    records = artifact_value["records"]
    if not isinstance(records, list) or len(records) != len(normalized_expected):
        raise ValueError(f"{component} authority records do not cover exact panel")
    observed_entries: list[str] = []
    used_receipts: set[str] = set()
    available_at_by_panel: dict[str, str] = {}
    effective_at_by_panel: dict[str, str] = {}
    calendar_cutoffs: dict[str, str] = {}
    calendar_phases: dict[str, dict[str, str]] = {}
    market_rule_payloads: list[Mapping[str, object]] = []
    payload_by_panel: dict[str, Mapping[str, object]] = {}
    corporate_action_announcements: dict[str, tuple[datetime, ...]] = {}
    status_payloads: Mapping[str, Mapping[str, object]] | None = None
    if component == "market_rules":
        if (
            instrument_status_authority is None
            or instrument_status_authority.component != "instrument_status"
            or set(instrument_status_authority.payload_by_panel)
            != set(normalized_expected)
        ):
            raise ValueError(
                "market_rules authority requires exact admitted instrument_status authority"
            )
        status_payloads = instrument_status_authority.payload_by_panel
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
        payload = _component_payload(component, record["payload"], panel_entry=entry)
        if component == "market_rules":
            if status_payloads is None:
                raise AssertionError("market rules status dependency was not initialized")
            payload = validate_market_rule_payload(
                payload,
                panel_entry=entry,
                instrument_status=status_payloads[entry],
            )
        if (
            _sha256(record["record_sha256"], "record_sha256")
            != hashlib.sha256(_canonical(payload)).hexdigest()
        ):
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
        if any(
            receipt_id not in bound_source_receipts
            for receipt_id in normalized_receipts
        ):
            raise ValueError(f"{component} authority uses unbound source receipt")
        for receipt_id in normalized_receipts:
            if receipt_id not in bound_receipts:
                bound_receipts[receipt_id] = _receipt_bindings(
                    bound_source_receipts[receipt_id], receipt_id
                )
        binding = (component, entry, str(record["record_sha256"]))
        if any(
            binding not in bound_receipts[receipt_id][0]
            for receipt_id in normalized_receipts
        ):
            raise ValueError(f"{component} authority receipt does not bind record")
        if component == "market_rules" and any(
            payload["source"] != bound_receipts[receipt_id][2]
            or payload["source_sha256"] != bound_receipts[receipt_id][3]
            for receipt_id in normalized_receipts
        ):
            raise ValueError(
                "market_rules payload source differs from its source receipt"
            )
        if component == "market_rules":
            market_rule_payloads.append(payload)
        payload_by_panel[entry] = dict(payload)
        if component == "corporate_actions":
            corporate_action_announcements[entry] = tuple(
                datetime.fromisoformat(
                    _timestamp(event["announcement_at"], "announcement_at")
                )
                for event in payload["events"]
            )
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
            calendar_phases[entry] = {
                "decision_cutoff_at": str(payload["decision_cutoff_at"]),
                "session_close_at": str(payload["session_close_at"]),
                "next_session_decision_cutoff_at": str(
                    payload["next_session_decision_cutoff_at"]
                ),
            }

    if tuple(observed_entries) != normalized_expected:
        raise ValueError(f"{component} authority records differ exact panel")
    if component == "market_rules":
        validate_market_rule_regimes(market_rule_payloads)
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
    envelope_effective = datetime.fromisoformat(verified.effective_at)
    available_at_by_panel = {
        entry: max(datetime.fromisoformat(value), envelope_available).isoformat()
        for entry, value in available_at_by_panel.items()
    }
    effective_at_by_panel = {
        entry: max(datetime.fromisoformat(value), envelope_effective).isoformat()
        for entry, value in effective_at_by_panel.items()
    }
    cutoffs = (
        calendar_cutoffs
        if component == "trading_calendar"
        else decision_cutoff_by_panel
    )
    if cutoffs is None:
        raise ValueError(f"{component} authority requires signed calendar cutoffs")
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
        payload_by_panel=payload_by_panel,
        decision_cutoff_by_panel=dict(cutoffs),
        signed_calendar_phases_by_panel=calendar_phases,
        instrument_status_artifact_sha256=(
            instrument_status_authority.artifact.identifier
            if component == "market_rules" and instrument_status_authority is not None
            else None
        ),
    )
    require_predecision_authority(admitted, decision_cutoff_by_panel=cutoffs)
    if component == "corporate_actions":
        for entry, announcements in corporate_action_announcements.items():
            cutoff = datetime.fromisoformat(
                _timestamp(cutoffs[entry], "decision cutoff")
            )
            if any(announcement >= cutoff for announcement in announcements):
                raise ValueError("corporate_actions announcement is post-cutoff")
    return admitted


def _require_generic_market_rule_coverage(
    *,
    panel_entry: str,
    payloads: Sequence[Mapping[str, object]],
) -> None:
    """Require one generic policy for every ST and listing-age state."""

    for is_st in (False, True):
        branch = [payload for payload in payloads if payload["is_st"] is is_st]
        if not branch:
            raise ValueError(
                f"generic market_rules omit the is_st={is_st} branch for {panel_entry}"
            )
        ordered = sorted(branch, key=lambda payload: int(payload["listing_age_min"]))
        next_listing_age = 0
        for index, payload in enumerate(ordered):
            listing_age_min = int(payload["listing_age_min"])
            listing_age_max = payload["listing_age_max"]
            if listing_age_min != next_listing_age:
                raise ValueError(
                    "generic market_rules listing-age coverage is incomplete"
                )
            if listing_age_max is None:
                if index != len(ordered) - 1:
                    raise ValueError(
                        "generic market_rules listing-age coverage overlaps"
                    )
                break
            next_listing_age = int(listing_age_max) + 1
        else:
            raise ValueError("generic market_rules listing-age coverage is incomplete")


def preregister_generic_market_rulebook(
    *,
    artifact_value: object,
    authority_envelope: object,
    expected_panel: Sequence[str],
    bound_source_receipts: Mapping[str, object],
    registry: EnrolledTrustRegistry,
    decision_cutoff_by_panel: Mapping[str, str],
) -> PreregisteredGenericMarketRulebook:
    """Verify signed all-state market-rule coverage for future-panel registration.

    This function intentionally returns a prerequisite type. It does not accept or
    produce an ``AdmittedProviderAuthority`` and therefore cannot grant execution
    readiness without a later exact instrument-status admission.
    """

    if not isinstance(artifact_value, Mapping) or set(artifact_value) != {
        "schema_version",
        "component",
        "panel",
        "records",
    }:
        raise ValueError("generic market_rules artifact schema is incomplete")
    if artifact_value["schema_version"] != COMPONENT_SCHEMAS["market_rules"]:
        raise ValueError("generic market_rules artifact schema version is invalid")
    if artifact_value["component"] != "market_rules":
        raise ValueError("generic market_rules artifact component drifted")
    normalized_expected = tuple(sorted(_panel_entry(item) for item in expected_panel))
    panel = artifact_value["panel"]
    if (
        not isinstance(panel, list)
        or tuple(panel) != normalized_expected
        or len(panel) != len(set(panel))
    ):
        raise ValueError("generic market_rules artifact differs exact panel")
    if set(decision_cutoff_by_panel) != set(normalized_expected):
        raise ValueError("generic market_rules require exact calendar cutoffs")
    records = artifact_value["records"]
    if not isinstance(records, list) or not records:
        raise ValueError("generic market_rules records are empty")

    bound_receipts: dict[
        str, tuple[set[tuple[str, str, str]], str, str, str]
    ] = {}
    payloads_by_panel: dict[str, list[Mapping[str, object]]] = {
        entry: [] for entry in normalized_expected
    }
    policy_ids_by_panel: dict[str, list[str]] = {
        entry: [] for entry in normalized_expected
    }
    available_at_by_panel: dict[str, datetime] = {}
    effective_at_by_panel: dict[str, datetime] = {}
    used_receipts: set[str] = set()
    observed_records: list[tuple[str, str]] = []
    all_payloads: list[Mapping[str, object]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or set(record) != {
            "panel_entry",
            "payload",
            "record_sha256",
            "source_receipt_ids",
            "effective_at",
            "available_at",
        }:
            raise ValueError(f"generic market_rules record {index} is incomplete")
        entry = _panel_entry(record["panel_entry"])
        if entry not in payloads_by_panel:
            raise ValueError("generic market_rules record is outside the exact panel")
        payload = validate_market_rule_payload(record["payload"], panel_entry=entry)
        if payload["is_st"] is None:
            raise ValueError("generic market_rules must enumerate both ST branches")
        record_sha256 = _sha256(record["record_sha256"], "record_sha256")
        if record_sha256 != hashlib.sha256(_canonical(payload)).hexdigest():
            raise ValueError("generic market_rules record hash drifted")
        observed_records.append((entry, record_sha256))
        receipt_ids = record["source_receipt_ids"]
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or receipt_ids != sorted(receipt_ids)
            or len(receipt_ids) != len(set(receipt_ids))
        ):
            raise ValueError("generic market_rules source receipts are invalid")
        normalized_receipts = {
            _sha256(receipt_id, "source_receipt_ids") for receipt_id in receipt_ids
        }
        if any(
            receipt_id not in bound_source_receipts
            for receipt_id in normalized_receipts
        ):
            raise ValueError("generic market_rules use an unbound source receipt")
        for receipt_id in normalized_receipts:
            if receipt_id not in bound_receipts:
                bound_receipts[receipt_id] = _receipt_bindings(
                    bound_source_receipts[receipt_id], receipt_id
                )
        binding = ("market_rules", entry, record_sha256)
        if any(
            binding not in bound_receipts[receipt_id][0]
            for receipt_id in normalized_receipts
        ):
            raise ValueError("generic market_rules receipt does not bind record")
        if any(
            payload["source"] != bound_receipts[receipt_id][2]
            or payload["source_sha256"] != bound_receipts[receipt_id][3]
            for receipt_id in normalized_receipts
        ):
            raise ValueError(
                "generic market_rules payload source differs from its source receipt"
            )
        effective_at = datetime.fromisoformat(
            _timestamp(record["effective_at"], "effective_at")
        )
        if effective_at.date().isoformat() != entry.split("@")[1]:
            raise ValueError("generic market_rules effective date differs panel")
        available_at = max(
            datetime.fromisoformat(_timestamp(record["available_at"], "available_at")),
            *(
                datetime.fromisoformat(bound_receipts[receipt_id][1])
                for receipt_id in normalized_receipts
            ),
        )
        available_at_by_panel[entry] = max(
            available_at_by_panel.get(entry, available_at), available_at
        )
        effective_at_by_panel[entry] = max(
            effective_at_by_panel.get(entry, effective_at), effective_at
        )
        payloads_by_panel[entry].append(dict(payload))
        policy_ids_by_panel[entry].append(str(payload["policy_id"]))
        all_payloads.append(payload)
        used_receipts.update(normalized_receipts)

    if observed_records != sorted(observed_records) or len(observed_records) != len(
        set(observed_records)
    ):
        raise ValueError("generic market_rules records must be sorted and unique")
    for entry, payloads in payloads_by_panel.items():
        _require_generic_market_rule_coverage(panel_entry=entry, payloads=payloads)
    validate_market_rule_regimes(all_payloads)
    artifact = ProviderArtifactReference(
        kind="stock-data-market-rules",
        identifier=hashlib.sha256(_canonical(dict(artifact_value))).hexdigest(),
        schema_version=COMPONENT_SCHEMAS["market_rules"],
    )
    verified = verify_authority_envelope(
        authority_envelope,
        registry=registry,
        expected_component="market_rules",
        expected_artifact=artifact.to_dict(),
        expected_source_receipt_ids=sorted(used_receipts),
    )
    if not isinstance(authority_envelope, Mapping):
        raise ValueError("authority envelope must be an object")
    envelope_available = datetime.fromisoformat(verified.available_at)
    envelope_effective = datetime.fromisoformat(verified.effective_at)
    available = {
        entry: max(value, envelope_available).isoformat()
        for entry, value in available_at_by_panel.items()
    }
    effective = {
        entry: max(value, envelope_effective).isoformat()
        for entry, value in effective_at_by_panel.items()
    }
    for entry in normalized_expected:
        cutoff = datetime.fromisoformat(
            _timestamp(decision_cutoff_by_panel[entry], "decision cutoff")
        )
        if (
            datetime.fromisoformat(available[entry]) >= cutoff
            or datetime.fromisoformat(effective[entry]) >= cutoff
        ):
            raise ValueError("generic market_rules authority is post-cutoff")
    return PreregisteredGenericMarketRulebook(
        artifact=artifact,
        source_receipt_ids=tuple(sorted(used_receipts)),
        publisher_key_id=verified.publisher_key_id,
        trust_root_id=verified.trust_root_id,
        signature_id=verified.signature_id,
        authority_envelope=dict(authority_envelope),
        available_at_by_panel=available,
        effective_at_by_panel=effective,
        decision_cutoff_by_panel=dict(decision_cutoff_by_panel),
        policy_ids_by_panel={
            entry: tuple(sorted(policy_ids))
            for entry, policy_ids in policy_ids_by_panel.items()
        },
    )


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
    "SIGNED_COMPONENTS",
    "SOURCE_RECEIPT_SCHEMA",
    "AdmittedProviderAuthority",
    "GENERIC_MARKET_RULEBOOK_PREREQUISITE_SCHEMA",
    "PreregisteredGenericMarketRulebook",
    "admit_signed_component_authority",
    "preregister_generic_market_rulebook",
    "require_predecision_authority",
]
