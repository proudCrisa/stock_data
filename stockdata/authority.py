"""Pinned trust registries and signed provider-authority envelopes."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


TRUST_REGISTRY_SCHEMA = "stockdata-enrolled-trust-registry/1"
SIGNER_ENROLLMENT_SCHEMA = "stockdata-signer-enrollment/1"
AUTHORITY_ENVELOPE_SCHEMA = "stockdata-authority-envelope/1"
ALGORITHM = "ed25519"
PROVIDER_TRUST_REGISTRY_SHA256 = (
    "69b94b1d01cb8dd299db799fac657b78ce77a548d35753ab9dca1c9bf94aeec6"
)
_PROVIDER_TRUST_REGISTRY_PATH = Path(__file__).with_name(
    "enrolled_trust_registry.json"
)
AUTHORITY_COMPONENT_ROLES = frozenset(
    {
        "trading_calendar",
        "universe",
        "instrument_status",
        "corporate_actions",
        "market_rules",
    }
)
_VERIFIED_REGISTRY_TOKEN = object()


@dataclass(frozen=True)
class _SignerEnrollment:
    publisher_key_id: str
    trust_root_id: str
    public_key: bytes
    component_roles: frozenset[str]
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class EnrolledTrustRegistry:
    """A verified registry whose identity is pinned by its caller."""

    schema_version: str
    registry_version: int
    registry_sha256: str
    _trust_roots: Mapping[str, bytes]
    _signers: Mapping[str, _SignerEnrollment]
    _verification_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_token is not _VERIFIED_REGISTRY_TOKEN:
            raise ValueError("trust registry must be created by the pinned loader")


def require_enrolled_role_coverage(
    registry: EnrolledTrustRegistry,
    *,
    roles: Sequence[str],
    valid_from: datetime,
    valid_until: datetime,
) -> Mapping[str, str]:
    """Return enrolled publishers that cover every role for the full interval."""

    if registry._verification_token is not _VERIFIED_REGISTRY_TOKEN:
        raise ValueError("registry must be created by the pinned loader")
    if (
        valid_from.tzinfo is None
        or valid_from.utcoffset() is None
        or valid_until.tzinfo is None
        or valid_until.utcoffset() is None
        or valid_from > valid_until
    ):
        raise ValueError("role coverage interval must be ordered and timezone-aware")
    requested = tuple(sorted(set(roles)))
    if not requested or not set(requested).issubset(AUTHORITY_COMPONENT_ROLES):
        raise ValueError("role coverage contains an unsupported authority role")

    coverage: dict[str, str] = {}
    for role in requested:
        candidates = sorted(
            signer.publisher_key_id
            for signer in registry._signers.values()
            if role in signer.component_roles
            and signer.publisher_key_id != signer.trust_root_id
            and signer.valid_from <= valid_from
            and valid_until <= signer.valid_until
        )
        if not candidates:
            raise ValueError(f"no independently enrolled signer covers {role}")
        coverage[role] = candidates[0]
    return MappingProxyType(coverage)


@dataclass(frozen=True)
class VerifiedAuthority:
    publisher_key_id: str
    trust_root_id: str
    signature_id: str
    effective_at: str
    available_at: str


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("authority JSON contains duplicate keys")
        result[key] = value
    return result


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
        raise ValueError("authority payload is not canonical JSON data") from exc


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _decode_base64(value: object, *, field: str, size: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be canonical base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be canonical base64") from exc
    if len(decoded) != size or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{field} must be canonical base64")
    return decoded


def _key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


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


def _public_key(value: object, *, field: str, expected_id: object) -> bytes:
    public_key = _decode_base64(value, field=field, size=32)
    if _sha256(expected_id, f"{field} identity") != _key_id(public_key):
        raise ValueError(f"{field} identity does not match its public key")
    return public_key


def _verify_signature(
    public_key: bytes, signature: object, payload: object, *, field: str
) -> bytes:
    signature_bytes = _decode_base64(signature, field=field, size=64)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature_bytes, _canonical(payload)
        )
    except (InvalidSignature, ValueError) as exc:
        raise ValueError(f"{field} verification failed") from exc
    return signature_bytes


def _enrollment_payload(
    enrollment: Mapping[str, object], *, registry_version: int
) -> dict[str, object]:
    return {
        "schema_version": SIGNER_ENROLLMENT_SCHEMA,
        "registry_schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": registry_version,
        "publisher_key_id": enrollment["publisher_key_id"],
        "publisher_public_key_base64": enrollment["public_key_base64"],
        "trust_root_id": enrollment["trust_root_id"],
        "component_roles": enrollment["component_roles"],
        "valid_from": enrollment["valid_from"],
        "valid_until": enrollment["valid_until"],
    }


def load_enrolled_trust_registry(
    path: str | Path, *, expected_sha256: str
) -> EnrolledTrustRegistry:
    """Load and verify an Ed25519 registry selected by an external SHA-256 pin."""

    expected_sha256 = _sha256(expected_sha256, "expected_sha256")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError("trust registry is unreadable") from exc
    registry_sha256 = hashlib.sha256(raw).hexdigest()
    if registry_sha256 != expected_sha256:
        raise ValueError("trust registry SHA-256 does not match the expected pin")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("trust registry is invalid JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "registry_version",
        "trust_roots",
        "signer_enrollments",
    }:
        raise ValueError("trust registry schema is incomplete")
    if value["schema_version"] != TRUST_REGISTRY_SCHEMA:
        raise ValueError("unsupported trust registry schema")
    registry_version = value["registry_version"]
    if (
        isinstance(registry_version, bool)
        or not isinstance(registry_version, int)
        or registry_version < 1
    ):
        raise ValueError("registry_version must be a positive integer")

    root_rows = value["trust_roots"]
    enrollment_rows = value["signer_enrollments"]
    if not isinstance(root_rows, list) or not isinstance(enrollment_rows, list):
        raise ValueError("trust roots and signer enrollments must be lists")

    roots: dict[str, bytes] = {}
    for row in root_rows:
        if not isinstance(row, Mapping) or set(row) != {
            "trust_root_id",
            "public_key_base64",
        }:
            raise ValueError("trust root entry is incomplete")
        root_id = _sha256(row["trust_root_id"], "trust_root_id")
        if root_id in roots:
            raise ValueError("trust registry contains a duplicate trust root")
        roots[root_id] = _public_key(
            row["public_key_base64"],
            field="trust root public key",
            expected_id=root_id,
        )

    signers: dict[str, _SignerEnrollment] = {}
    for row in enrollment_rows:
        if not isinstance(row, Mapping) or set(row) != {
            "publisher_key_id",
            "trust_root_id",
            "public_key_base64",
            "component_roles",
            "valid_from",
            "valid_until",
            "authorization_signature_base64",
        }:
            raise ValueError("signer enrollment is incomplete")
        publisher_key_id = _sha256(row["publisher_key_id"], "publisher_key_id")
        trust_root_id = _sha256(row["trust_root_id"], "trust_root_id")
        if publisher_key_id in signers:
            raise ValueError("trust registry contains a duplicate signer")
        root_public_key = roots.get(trust_root_id)
        if root_public_key is None:
            raise ValueError("signer enrollment refers to an unknown trust root")
        signer_public_key = _public_key(
            row["public_key_base64"],
            field="publisher public key",
            expected_id=publisher_key_id,
        )
        roles = row["component_roles"]
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) for role in roles)
            or roles != sorted(roles)
            or len(roles) != len(set(roles))
            or not set(roles).issubset(AUTHORITY_COMPONENT_ROLES)
        ):
            raise ValueError("component_roles must be supported, sorted, and unique")
        _, valid_from = _timestamp(row["valid_from"], "valid_from")
        _, valid_until = _timestamp(row["valid_until"], "valid_until")
        if valid_from > valid_until:
            raise ValueError("signer enrollment validity interval is reversed")
        _verify_signature(
            root_public_key,
            row["authorization_signature_base64"],
            _enrollment_payload(row, registry_version=registry_version),
            field="root authorization signature",
        )
        signers[publisher_key_id] = _SignerEnrollment(
            publisher_key_id=publisher_key_id,
            trust_root_id=trust_root_id,
            public_key=signer_public_key,
            component_roles=frozenset(roles),
            valid_from=valid_from,
            valid_until=valid_until,
        )

    return EnrolledTrustRegistry(
        schema_version=TRUST_REGISTRY_SCHEMA,
        registry_version=registry_version,
        registry_sha256=registry_sha256,
        _trust_roots=MappingProxyType(roots),
        _signers=MappingProxyType(signers),
        _verification_token=_VERIFIED_REGISTRY_TOKEN,
    )


def load_provider_trust_registry() -> EnrolledTrustRegistry:
    """Load the provider-owned registry pinned by this stock_data checkout."""

    return load_enrolled_trust_registry(
        _PROVIDER_TRUST_REGISTRY_PATH,
        expected_sha256=PROVIDER_TRUST_REGISTRY_SHA256,
    )


def verify_authority_envelope(
    envelope: object,
    *,
    registry: EnrolledTrustRegistry,
    expected_component: str,
    expected_artifact: Mapping[str, object],
    expected_source_receipt_ids: Sequence[str],
) -> VerifiedAuthority:
    """Verify an authority envelope against pinned trust and expected evidence."""

    if not isinstance(registry, EnrolledTrustRegistry):
        raise ValueError("registry must be a verified EnrolledTrustRegistry")
    if expected_component not in AUTHORITY_COMPONENT_ROLES:
        raise ValueError("expected_component is not a supported authority role")
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "schema_version",
        "algorithm",
        "payload",
        "signature_base64",
    }:
        raise ValueError("authority envelope schema is incomplete")
    if (
        envelope["schema_version"] != AUTHORITY_ENVELOPE_SCHEMA
        or envelope["algorithm"] != ALGORITHM
    ):
        raise ValueError("unsupported authority envelope schema or algorithm")
    payload = envelope["payload"]
    if not isinstance(payload, Mapping) or set(payload) != {
        "component_role",
        "artifact",
        "source_receipt_ids",
        "effective_at",
        "available_at",
        "publisher_key_id",
        "trust_root_id",
        "trust_registry_sha256",
    }:
        raise ValueError("authority envelope payload is incomplete")
    if payload["component_role"] != expected_component:
        raise ValueError("authority envelope has the wrong component role")
    if payload["trust_registry_sha256"] != registry.registry_sha256:
        raise ValueError("authority envelope is bound to a different trust registry")

    artifact = payload["artifact"]
    if not isinstance(artifact, Mapping) or set(artifact) != {
        "kind",
        "identifier",
        "schema_version",
    }:
        raise ValueError("authority artifact reference is incomplete")
    if (
        not isinstance(artifact["kind"], str)
        or not artifact["kind"]
        or not isinstance(artifact["schema_version"], str)
        or not artifact["schema_version"]
    ):
        raise ValueError("authority artifact reference is invalid")
    _sha256(artifact["identifier"], "artifact identifier")
    if _canonical(dict(artifact)) != _canonical(dict(expected_artifact)):
        raise ValueError("authority artifact differs from the expected reference")

    receipt_ids = payload["source_receipt_ids"]
    expected_receipts = list(expected_source_receipt_ids)
    if (
        not isinstance(receipt_ids, list)
        or not receipt_ids
        or any(
            _sha256(receipt_id, "source receipt id") != receipt_id
            for receipt_id in receipt_ids
        )
        or receipt_ids != sorted(receipt_ids)
        or len(receipt_ids) != len(set(receipt_ids))
    ):
        raise ValueError("source_receipt_ids must be non-empty, sorted, and unique")
    if receipt_ids != expected_receipts:
        raise ValueError("authority source receipts differ from the expected identities")

    publisher_key_id = _sha256(payload["publisher_key_id"], "publisher_key_id")
    trust_root_id = _sha256(payload["trust_root_id"], "trust_root_id")
    if trust_root_id not in registry._trust_roots:
        raise ValueError("authority envelope refers to an unknown trust root")
    signer = registry._signers.get(publisher_key_id)
    if signer is None:
        raise ValueError("authority envelope refers to an unknown signer")
    if signer.trust_root_id != trust_root_id:
        raise ValueError("authority signer is bound to a different trust root")
    if expected_component not in signer.component_roles:
        raise ValueError("authority signer is not authorized for this component role")

    effective_at, _ = _timestamp(payload["effective_at"], "effective_at")
    available_at, available_time = _timestamp(payload["available_at"], "available_at")
    if not signer.valid_from <= available_time <= signer.valid_until:
        raise ValueError("available_at falls outside the signer authorization interval")

    signature = _verify_signature(
        signer.public_key,
        envelope["signature_base64"],
        payload,
        field="authority envelope signature",
    )
    return VerifiedAuthority(
        publisher_key_id=publisher_key_id,
        trust_root_id=trust_root_id,
        signature_id=hashlib.sha256(signature).hexdigest(),
        effective_at=effective_at,
        available_at=available_at,
    )
