"""Content identities for execution and signal price adjustments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json


EXECUTION_ADJUSTMENT_SCHEMA = "stockdata-execution-adjustment-identity/1"
SIGNAL_ADJUSTMENT_SCHEMA = "stockdata-signal-adjustment-identity/1"


@dataclass(frozen=True)
class VerifiedAdjustmentIdentity:
    identifier: str
    price_role: str
    source: str
    adjustment_mode: str
    adjustment_version: str


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
        raise ValueError("adjustment identity is not canonical JSON data") from exc


def verify_adjustment_identity(
    value: object, *, expected_price_role: str
) -> VerifiedAdjustmentIdentity:
    """Verify one declared adjustment identity and return its content hash."""

    schemas = {
        "execution": EXECUTION_ADJUSTMENT_SCHEMA,
        "signal": SIGNAL_ADJUSTMENT_SCHEMA,
    }
    if expected_price_role not in schemas:
        raise ValueError("expected_price_role must be execution or signal")
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "price_role",
        "source",
        "adjustment_mode",
        "adjustment_version",
    }:
        raise ValueError("adjustment identity schema is incomplete")
    if (
        value["schema_version"] != schemas[expected_price_role]
        or value["price_role"] != expected_price_role
    ):
        raise ValueError("adjustment identity has the wrong role or schema")
    for field in ("source", "adjustment_mode", "adjustment_version"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"adjustment identity {field} must be non-empty")
    if expected_price_role == "execution" and value["adjustment_mode"] != "raw":
        raise ValueError("execution prices must use raw adjustment identity")
    return VerifiedAdjustmentIdentity(
        identifier=hashlib.sha256(_canonical(dict(value))).hexdigest(),
        price_role=expected_price_role,
        source=value["source"],
        adjustment_mode=value["adjustment_mode"],
        adjustment_version=value["adjustment_version"],
    )
