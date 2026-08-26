"""Minimal strict publisher for provider authority artifacts.

This module assembles externally authorized provider authority evidence.  It
does not generate keys, enroll trust by itself, or persist signing secrets.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .authority import (
    ALGORITHM,
    AUTHORITY_COMPONENT_ROLES,
    AUTHORITY_ENVELOPE_SCHEMA,
    EnrolledTrustRegistry,
    TRUST_REGISTRY_SCHEMA,
    load_enrolled_trust_registry,
)
from .provider_authority_admission import (
    AdmittedProviderAuthority,
    PreregisteredGenericMarketRulebook,
    SOURCE_RECEIPT_SCHEMA,
    admit_signed_component_authority,
    preregister_generic_market_rulebook,
)
from .rqgm_provider_contract import COMPONENT_SCHEMAS, ProviderArtifactReference


@dataclass(frozen=True)
class PublishedEnvelope:
    envelope: Mapping[str, object]
    admitted: AdmittedProviderAuthority | None = None
    preregistered: PreregisteredGenericMarketRulebook | None = None

    def __post_init__(self) -> None:
        if (self.admitted is None) == (self.preregistered is None):
            raise ValueError("published authority must have exactly one verification result")


class _RegistrySigner(Protocol):
    component_roles: Collection[str]
    trust_root_id: str


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("provider authority publisher JSON contains duplicate keys")
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
        raise ValueError("provider authority publisher payload is not canonical JSON") from exc


def _read_canonical_json(path: str | Path, field: str) -> object:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{field} is unreadable") from exc
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is invalid JSON") from exc
    if raw != _canonical(value):
        raise ValueError(f"{field} must be canonical JSON")
    return value


def _write_canonical_json(path: str | Path, value: object) -> None:
    Path(path).write_bytes(_canonical(value))


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _signing_key_from_env(env_name: str) -> Ed25519PrivateKey:
    if not env_name:
        raise ValueError("signer private key environment variable name is required")
    value = os.environ.get(env_name)
    if value is None:
        raise ValueError("signer private key secret is missing from the environment")
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("signer private key secret must be canonical base64") from exc
    if len(raw) != 32 or _base64(raw) != value:
        raise ValueError("signer private key secret must be canonical base64")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _receipt_id(receipt: object) -> str:
    if not isinstance(receipt, Mapping):
        raise ValueError("source receipt must be an object")
    if receipt.get("schema_version") != SOURCE_RECEIPT_SCHEMA:
        raise ValueError("source receipt schema is invalid")
    return hashlib.sha256(_canonical(dict(receipt))).hexdigest()


def _artifact_reference(component: str, artifact: object) -> ProviderArtifactReference:
    if component not in AUTHORITY_COMPONENT_ROLES:
        raise ValueError("component is not a supported provider authority role")
    if not isinstance(artifact, Mapping):
        raise ValueError("component artifact must be an object")
    if artifact.get("component") != component:
        raise ValueError("component artifact role does not match requested component")
    if artifact.get("schema_version") != COMPONENT_SCHEMAS[component]:
        raise ValueError("component artifact schema version is invalid")
    return ProviderArtifactReference(
        kind=f"stock-data-{component.replace('_', '-')}",
        identifier=hashlib.sha256(_canonical(dict(artifact))).hexdigest(),
        schema_version=COMPONENT_SCHEMAS[component],
    )


def _registry_signer(
    registry: EnrolledTrustRegistry,
    publisher_key_id: str,
    component: str,
) -> _RegistrySigner:
    signers = getattr(registry, "_signers", {})
    signer = signers.get(publisher_key_id)
    if signer is None:
        raise ValueError("authority publisher refers to an unknown signer")
    roles: Collection[str] = getattr(signer, "component_roles", frozenset())
    if not roles:
        raise ValueError("authority publisher signer has no component roles")
    if component not in roles:
        raise ValueError("authority publisher signer role does not match component")
    return cast(_RegistrySigner, signer)


def build_canonical_registry(
    *,
    root_public_key_file: str | Path,
    enrollment_file: str | Path,
    output_file: str | Path,
    registry_version: int = 1,
) -> EnrolledTrustRegistry:
    """Build and production-verify a canonical registry from external inputs."""

    if (
        isinstance(registry_version, bool)
        or not isinstance(registry_version, int)
        or registry_version < 1
    ):
        raise ValueError("registry_version must be a positive integer")
    root = _read_canonical_json(root_public_key_file, "root public key")
    enrollment = _read_canonical_json(enrollment_file, "root-signed enrollment")
    if not isinstance(root, Mapping) or set(root) != {
        "trust_root_id",
        "public_key_base64",
    }:
        raise ValueError("root public key schema is incomplete")
    if not isinstance(enrollment, Mapping) or set(enrollment) != {
        "publisher_key_id",
        "trust_root_id",
        "public_key_base64",
        "component_roles",
        "valid_from",
        "valid_until",
        "authorization_signature_base64",
    }:
        raise ValueError("root-signed enrollment schema is incomplete")
    if enrollment["trust_root_id"] != root["trust_root_id"]:
        raise ValueError("root-signed enrollment trust root does not match")
    roles = enrollment["component_roles"]
    if not isinstance(roles, list) or not roles:
        raise ValueError("root-signed enrollment roles must be non-empty")
    registry = {
        "schema_version": TRUST_REGISTRY_SCHEMA,
        "registry_version": registry_version,
        "trust_roots": [dict(root)],
        "signer_enrollments": [dict(enrollment)],
    }
    raw = _canonical(registry)
    pin = hashlib.sha256(raw).hexdigest()
    output = Path(output_file)
    output.write_bytes(raw)
    try:
        return load_enrolled_trust_registry(output, expected_sha256=pin)
    except ValueError:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def publish_authority_envelope(
    *,
    component: str,
    registry_file: str | Path,
    registry_sha256: str,
    artifact_file: str | Path,
    source_receipt_files: Sequence[str | Path],
    signer_private_key_env: str,
    output_file: str | Path,
    effective_at: str,
    available_at: str,
    publisher_key_id: str | None = None,
    decision_cutoff_by_panel: Mapping[str, str] | None = None,
) -> PublishedEnvelope:
    """Build, sign, production-admit, and write one authority envelope."""

    registry = load_enrolled_trust_registry(
        registry_file, expected_sha256=_sha256(registry_sha256, "registry_sha256")
    )
    artifact = _read_canonical_json(artifact_file, "component artifact")
    artifact_reference = _artifact_reference(component, artifact)
    if not isinstance(artifact, Mapping) or not isinstance(artifact.get("panel"), list):
        raise ValueError("component artifact panel is invalid")
    receipts: dict[str, object] = {}
    for receipt_file in source_receipt_files:
        receipt = _read_canonical_json(receipt_file, "source receipt")
        receipt_id = _receipt_id(receipt)
        if receipt_id in receipts:
            raise ValueError("source receipts must be unique")
        receipts[receipt_id] = receipt
    if not receipts:
        raise ValueError("at least one source receipt is required")

    signer_key = _signing_key_from_env(signer_private_key_env)
    derived_key_id = _key_id(_public_key(signer_key))
    if publisher_key_id is not None and _sha256(
        publisher_key_id, "publisher_key_id"
    ) != derived_key_id:
        raise ValueError("publisher_key_id does not match signer private key")
    signer = _registry_signer(registry, derived_key_id, component)
    payload = {
        "component_role": component,
        "artifact": artifact_reference.to_dict(),
        "source_receipt_ids": sorted(receipts),
        "effective_at": effective_at,
        "available_at": available_at,
        "publisher_key_id": derived_key_id,
        "trust_root_id": signer.trust_root_id,
        "trust_registry_sha256": registry.registry_sha256,
    }
    envelope = {
        "schema_version": AUTHORITY_ENVELOPE_SCHEMA,
        "algorithm": ALGORITHM,
        "payload": payload,
        "signature_base64": _base64(signer_key.sign(_canonical(payload))),
    }
    if component == "market_rules":
        if decision_cutoff_by_panel is None:
            raise ValueError("generic market-rule publication requires calendar cutoffs")
        preregistered = preregister_generic_market_rulebook(
            artifact_value=artifact,
            authority_envelope=envelope,
            expected_panel=artifact["panel"],
            bound_source_receipts=receipts,
            registry=registry,
            decision_cutoff_by_panel=decision_cutoff_by_panel,
        )
        _write_canonical_json(output_file, envelope)
        return PublishedEnvelope(envelope=envelope, preregistered=preregistered)
    admitted = admit_signed_component_authority(
        component=component,
        artifact_value=artifact,
        authority_envelope=envelope,
        expected_panel=artifact["panel"],
        bound_source_receipts=receipts,
        registry=registry,
        decision_cutoff_by_panel=decision_cutoff_by_panel,
    )
    _write_canonical_json(output_file, envelope)
    return PublishedEnvelope(envelope=envelope, admitted=admitted)


def _parse_cutoffs(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        panel_entry, separator, cutoff = value.partition("=")
        if not separator or not panel_entry or not cutoff or panel_entry in result:
            raise ValueError("--decision-cutoff must be unique PANEL=TIMESTAMP")
        result[panel_entry] = cutoff
    return result


def _handle_build_registry(args: argparse.Namespace) -> None:
    build_canonical_registry(
        root_public_key_file=args.root_public_key,
        enrollment_file=args.enrollment,
        output_file=args.output,
        registry_version=args.registry_version,
    )


def _handle_publish_envelope(args: argparse.Namespace) -> None:
    publish_authority_envelope(
        component=args.component,
        registry_file=args.registry,
        registry_sha256=args.registry_sha256,
        artifact_file=args.artifact,
        source_receipt_files=args.source_receipt,
        signer_private_key_env=args.signer_private_key_env,
        output_file=args.output,
        effective_at=args.effective_at,
        available_at=args.available_at,
        publisher_key_id=args.publisher_key_id,
        decision_cutoff_by_panel=_parse_cutoffs(args.decision_cutoff),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockdata-provider-authority-publisher",
        description="Build externally authorized provider authority registries and envelopes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-registry")
    build.add_argument("--root-public-key", required=True)
    build.add_argument("--enrollment", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--registry-version", type=int, default=1)
    build.set_defaults(func=_handle_build_registry)

    publish = subparsers.add_parser("publish-envelope")
    publish.add_argument("--component", required=True)
    publish.add_argument("--registry", required=True)
    publish.add_argument("--registry-sha256", required=True)
    publish.add_argument("--artifact", required=True)
    publish.add_argument("--source-receipt", action="append", default=[])
    publish.add_argument("--signer-private-key-env", required=True)
    publish.add_argument("--output", required=True)
    publish.add_argument("--effective-at", required=True)
    publish.add_argument("--available-at", required=True)
    publish.add_argument("--publisher-key-id")
    publish.add_argument("--decision-cutoff", action="append", default=[])
    publish.set_defaults(func=_handle_publish_envelope)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ValueError as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
