"""Materialize local mechanical prerequisites for a future exact panel."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .future_panel_registration import _panel
from .provider_authority_admission import (
    SOURCE_RECEIPT_SCHEMA,
    validate_local_mechanical_prerequisites,
)
from .rqgm_provider_contract import COMPONENT_SCHEMAS

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CALENDAR_FACTS_SCHEMA = "stockdata-trusted-local-calendar-facts/1"
_MARKET_RULES_FACTS_SCHEMA = "stockdata-trusted-local-market-rules-facts/1"
_CALENDAR_FACTS_SOURCE = "trusted-local-calendar-facts"


class TrustedLocalPrerequisitesError(ValueError):
    """Raised when local prerequisite materialization cannot complete safely."""


def _now() -> datetime:
    return datetime.now(_SHANGHAI)


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
        raise TrustedLocalPrerequisitesError("prerequisite content is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _receipt(
    *, source: str, observed_at: str, response_sha256: str, bindings: Sequence[Mapping[str, str]]
) -> dict[str, object]:
    return {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source": source,
        "observed_at": observed_at,
        "response_sha256": response_sha256,
        "bindings": [dict(binding) for binding in sorted(bindings, key=lambda item: (
            item["component"], item["panel_entry"], item["record_sha256"]
        ))],
    }


def _artifact_records(
    *, component: str, records: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    ordered = sorted(
        (dict(record) for record in records),
        key=lambda record: (str(record["panel_entry"]), str(record["record_sha256"])),
    )
    return {
        "schema_version": COMPONENT_SCHEMAS[component],
        "component": component,
        "panel": sorted({str(record["panel_entry"]) for record in ordered}),
        "records": ordered,
    }


def _canonical_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise TrustedLocalPrerequisitesError(f"{label} path must be canonical")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TrustedLocalPrerequisitesError(f"{label} file is unavailable") from exc
    if resolved != path or not stat.S_ISREG(path.stat().st_mode):
        raise TrustedLocalPrerequisitesError(f"{label} file must be canonical regular file")
    return path


def _read_canonical_json(path: Path, label: str) -> object:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustedLocalPrerequisitesError(f"{label} facts are not canonical") from exc
    if _canonical(value) != raw:
        raise TrustedLocalPrerequisitesError(f"{label} facts are not canonical")
    return value


def _read_canonical_facts(path: Path, label: str) -> Mapping[str, object]:
    value = _read_canonical_json(path, label)
    if not isinstance(value, Mapping):
        raise TrustedLocalPrerequisitesError(f"{label} facts are not canonical")
    return value


def _fact_records(
    facts: Mapping[str, object],
    *,
    schema: str,
    panel: Sequence[str],
    label: str,
    required_branches: bool,
) -> list[Mapping[str, object]]:
    if set(facts) != {"schema_version", "panel", "records"} or facts["schema_version"] != schema:
        raise TrustedLocalPrerequisitesError(f"{label} facts schema is invalid")
    if facts["panel"] != list(panel) or not isinstance(facts["records"], list):
        raise TrustedLocalPrerequisitesError(f"{label} facts differ from exact panel")
    records = facts["records"]
    expected = {
        (entry, branch)
        for entry in panel
        for branch in ((False, True) if required_branches else (None,))
    }
    parsed: list[tuple[str, bool | None, Mapping[str, object]]] = []
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {
            "panel_entry", "payload", "effective_at", "available_at"
        }:
            raise TrustedLocalPrerequisitesError(f"{label} fact record is incomplete")
        entry = record["panel_entry"]
        if not isinstance(entry, str) or entry not in panel or not isinstance(record["payload"], Mapping):
            raise TrustedLocalPrerequisitesError(f"{label} facts coverage is invalid")
        branch = record["payload"].get("is_st") if required_branches else None
        if required_branches and type(branch) is not bool:
            raise TrustedLocalPrerequisitesError(f"{label} facts require both ST branches")
        parsed.append((entry, branch, record))
    actual = {(entry, branch) for entry, branch, _ in parsed}
    if actual != expected or len(parsed) != len(actual):
        raise TrustedLocalPrerequisitesError(f"{label} facts coverage is incomplete")
    if [(entry, branch) for entry, branch, _ in parsed] != sorted(actual):
        raise TrustedLocalPrerequisitesError(f"{label} fact records must be sorted")
    return [record for _, _, record in parsed]


def _write_exclusive(path: Path, payload: object) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _cleanup(directory: Path) -> None:
    for filename in (
        "trading-calendar.json",
        "trading-calendar-receipt.json",
        "market-rules.json",
        "market-rules-receipt.json",
    ):
        try:
            (directory / filename).unlink()
        except FileNotFoundError:
            pass
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass


def materialize_trusted_local_prerequisites(
    panel_file: str | Path,
    output_dir: str | Path,
    calendar_facts_file: str | Path,
    market_rules_facts_file: str | Path,
) -> dict[str, str]:
    """Write exact local calendar and market-rule prerequisites for a future panel."""

    try:
        panel_path = _canonical_path(panel_file, "panel")
        calendar_facts_path = _canonical_path(calendar_facts_file, "calendar facts")
        market_rules_facts_path = _canonical_path(
            market_rules_facts_file, "market rules facts"
        )
        if len({panel_path, calendar_facts_path, market_rules_facts_path}) != 3:
            raise TrustedLocalPrerequisitesError("prerequisite input paths are aliased")
        panel_value = _read_canonical_json(panel_path, "panel")
        if (
            not isinstance(panel_value, list)
            or not panel_value
            or any(not isinstance(entry, str) or entry.count("@") != 1 for entry in panel_value)
        ):
            raise TrustedLocalPrerequisitesError("panel file is invalid")
        panel = tuple(panel_value)
        for entry in panel:
            date.fromisoformat(entry.rsplit("@", 1)[1])
    except ValueError as exc:
        raise TrustedLocalPrerequisitesError(str(exc)) from exc
    now = _now()
    if any(date.fromisoformat(entry.rsplit("@", 1)[1]) <= now.date() for entry in panel):
        raise TrustedLocalPrerequisitesError("every panel session must be future dated")

    calendar_facts = _read_canonical_facts(calendar_facts_path, "calendar")
    market_rules_facts = _read_canonical_facts(market_rules_facts_path, "market rules")
    calendar_facts_records = _fact_records(
        calendar_facts,
        schema=_CALENDAR_FACTS_SCHEMA,
        panel=panel,
        label="calendar",
        required_branches=False,
    )
    market_rule_facts_records = _fact_records(
        market_rules_facts,
        schema=_MARKET_RULES_FACTS_SCHEMA,
        panel=panel,
        label="market rules",
        required_branches=True,
    )
    if any(record["payload"].get("is_trading_day") is not True for record in calendar_facts_records):
        raise TrustedLocalPrerequisitesError("calendar facts contain a holiday or non-trading day")
    rule_sources = {
        (
            record["payload"].get("source"),
            record["payload"].get("source_sha256"),
        )
        for record in market_rule_facts_records
    }
    if (
        len(rule_sources) != 1
        or not isinstance(next(iter(rule_sources))[0], str)
        or not next(iter(rule_sources))[0]
        or not isinstance(next(iter(rule_sources))[1], str)
    ):
        raise TrustedLocalPrerequisitesError("market rules facts source is incomplete")
    try:
        panel, _, _ = _panel(panel_path)
    except ValueError as exc:
        raise TrustedLocalPrerequisitesError(str(exc)) from exc

    directory = Path(output_dir)
    if not directory.is_absolute() or directory != directory.resolve(strict=False):
        raise TrustedLocalPrerequisitesError("output directory path must be canonical")
    try:
        if any(path.is_relative_to(directory) or directory.is_relative_to(path) for path in (
            panel_path,
            calendar_facts_path,
            market_rules_facts_path,
        )):
            raise TrustedLocalPrerequisitesError("output directory overlaps prerequisite inputs")
    except ValueError as exc:
        raise TrustedLocalPrerequisitesError("output directory overlaps prerequisite inputs") from exc
    if directory.exists() or directory.is_symlink():
        raise TrustedLocalPrerequisitesError("output directory must be new")
    try:
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise TrustedLocalPrerequisitesError("output directory cannot be created") from exc

    try:
        calendar_records: list[dict[str, object]] = []
        for record in calendar_facts_records:
            payload = dict(record["payload"])
            calendar_records.append(
                {
                    "panel_entry": record["panel_entry"],
                    "payload": payload,
                    "record_sha256": _sha256(payload),
                    "source_receipt_ids": [],
                    "effective_at": record["effective_at"],
                    "available_at": record["available_at"],
                }
            )
        calendar_receipt = _receipt(
            source=_CALENDAR_FACTS_SOURCE,
            observed_at=now.isoformat(),
            response_sha256=hashlib.sha256(_canonical(calendar_facts)).hexdigest(),
            bindings=[
                {
                    "component": "trading_calendar",
                    "panel_entry": str(record["panel_entry"]),
                    "record_sha256": str(record["record_sha256"]),
                }
                for record in calendar_records
            ],
        )
        calendar_receipt_id = _sha256(calendar_receipt)
        for record in calendar_records:
            record["source_receipt_ids"] = [calendar_receipt_id]
        calendar_artifact = _artifact_records(
            component="trading_calendar", records=calendar_records
        )

        market_rule_records: list[dict[str, object]] = []
        for record in market_rule_facts_records:
            payload = dict(record["payload"])
            market_rule_records.append(
                {
                    "panel_entry": record["panel_entry"],
                    "payload": payload,
                    "record_sha256": _sha256(payload),
                    "source_receipt_ids": [],
                    "effective_at": record["effective_at"],
                    "available_at": record["available_at"],
                }
            )
        rule_source, rule_source_sha256 = next(iter(rule_sources))
        market_rules_receipt = _receipt(
            source=rule_source,
            observed_at=now.isoformat(),
            response_sha256=rule_source_sha256,
            bindings=[
                {
                    "component": "market_rules",
                    "panel_entry": str(record["panel_entry"]),
                    "record_sha256": str(record["record_sha256"]),
                }
                for record in market_rule_records
            ],
        )
        market_rules_receipt_id = _sha256(market_rules_receipt)
        for record in market_rule_records:
            record["source_receipt_ids"] = [market_rules_receipt_id]
        market_rules_artifact = _artifact_records(
            component="market_rules", records=market_rule_records
        )

        validate_local_mechanical_prerequisites(
            calendar_artifact=calendar_artifact,
            market_rules_artifact=market_rules_artifact,
            expected_panel=panel,
            bound_source_receipts={
                calendar_receipt_id: calendar_receipt,
                market_rules_receipt_id: market_rules_receipt,
            },
        )
        calendar_path = directory / "trading-calendar.json"
        calendar_receipt_path = directory / "trading-calendar-receipt.json"
        market_rules_path = directory / "market-rules.json"
        market_rules_receipt_path = directory / "market-rules-receipt.json"
        _write_exclusive(calendar_path, calendar_artifact)
        _write_exclusive(calendar_receipt_path, calendar_receipt)
        _write_exclusive(market_rules_path, market_rules_artifact)
        _write_exclusive(market_rules_receipt_path, market_rules_receipt)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return {
            "output_dir": str(directory.resolve()),
            "trading_calendar_file": str(calendar_path.resolve()),
            "trading_calendar_receipt_file": str(calendar_receipt_path.resolve()),
            "market_rules_file": str(market_rules_path.resolve()),
            "market_rules_receipt_file": str(market_rules_receipt_path.resolve()),
        }
    except Exception as exc:
        _cleanup(directory)
        if isinstance(exc, TrustedLocalPrerequisitesError):
            raise
        raise TrustedLocalPrerequisitesError("trusted local prerequisites cannot be materialized") from exc
