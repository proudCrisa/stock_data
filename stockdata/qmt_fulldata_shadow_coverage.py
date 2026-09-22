"""Presence-only closure for registered QMT fulldata shadow captures."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .qmt_fulldata_shadow_capture import verify_qmt_fulldata_shadow_capture
from .qmt_transport_capture import _canonical, _read_regular_file, _write_content_addressed

SCHEMA_VERSION = "qmt-fulldata-shadow-coverage/1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ROLE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")


class QmtFulldataShadowCoverageError(ValueError):
    pass


def _raw(path: Path) -> bytes:
    return _read_regular_file(path, 8 * 1024 * 1024, QmtFulldataShadowCoverageError)


def _json(raw: bytes, label: str) -> dict:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QmtFulldataShadowCoverageError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise QmtFulldataShadowCoverageError(f"{label} is not canonical JSON")
    return value


def _under(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if root not in resolved.parents:
        raise QmtFulldataShadowCoverageError("identity path escapes evidence root")
    return resolved


def build_coverage(evidence_root: str | Path) -> dict:
    root = Path(evidence_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise QmtFulldataShadowCoverageError("evidence root is invalid")
    names = {item.name for item in root.iterdir()}
    if not {"registration.json", "execution-identity-receipt.json", "captures", "coverage"} <= names:
        raise QmtFulldataShadowCoverageError("evidence root is incomplete")
    if any(name not in {"registration.json", "execution-identity-receipt.json", "captures", "coverage", "runs", "run_remaining_21.sh"} for name in names):
        raise QmtFulldataShadowCoverageError("evidence root has unexpected entry")
    registration_path, receipt_path = root / "registration.json", root / "execution-identity-receipt.json"
    registration_raw, receipt_raw = _raw(registration_path), _raw(receipt_path)
    registration, receipt = _json(registration_raw, "registration"), _json(receipt_raw, "receipt")
    registration_keys = {"schema_version", "registered_at", "capture_revision", "target_asof", "start", "end", "count", "period", "authority_grade", "decision_eligible", "actions", "permitted_use", "requests"}
    if set(registration) != registration_keys or registration.get("schema_version") != "qmt-fulldata-shadow-registration/1" or registration.get("authority_grade") != "shadow" or registration.get("decision_eligible") is not False or registration.get("actions") != [] or registration.get("permitted_use") != "offline_review_only" or registration.get("period") != "1d" or not isinstance(registration.get("requests"), list) or len(registration["requests"]) != 23:
        raise QmtFulldataShadowCoverageError("registration schema differs")
    roles = [(item.get("symbol"), item.get("adjustment")) if isinstance(item, dict) and set(item) == {"symbol", "adjustment"} else (None, None) for item in registration["requests"]]
    if len(set(roles)) != 23 or any(not isinstance(symbol, str) or not _ROLE.fullmatch(symbol) or adjustment not in {"raw", "qfq"} for symbol, adjustment in roles):
        raise QmtFulldataShadowCoverageError("registration roles differ")
    receipt_keys = {"schema_version", "recorded_at", "registration_path", "registration_sha256", "registered_capture_revision", "executed_capture_revision", "revision_change_reason", "scope_changed", "recorded_after_execution_started", "first_bound_run_identity_path", "authority_grade", "decision_eligible", "actions", "formal_admission"}
    if set(receipt) != receipt_keys or receipt.get("schema_version") != "qmt-fulldata-shadow-execution-identity/1" or receipt.get("registration_sha256") != hashlib.sha256(registration_raw).hexdigest() or receipt.get("registered_capture_revision") != registration["capture_revision"] or receipt.get("recorded_after_execution_started") is not True or receipt.get("scope_changed") is not False or receipt.get("authority_grade") != "shadow" or receipt.get("decision_eligible") is not False or receipt.get("actions") != [] or receipt.get("formal_admission") is not False:
        raise QmtFulldataShadowCoverageError("execution identity receipt differs")
    identity = _under(root, Path(receipt["first_bound_run_identity_path"]))
    identity_raw = _raw(identity)
    if identity_raw != f"HEAD={receipt['executed_capture_revision']}\n".encode("ascii"):
        raise QmtFulldataShadowCoverageError("first bound identity differs")
    captures = root / "captures"
    found = [path for path in captures.rglob("*") if path.is_file() or path.is_symlink()]
    entries = []
    expected_paths = set()
    for symbol, adjustment in roles:
        matches = [path for path in found if path.is_file() and path.parent.parent.name == symbol and path.parent.name.startswith(adjustment + "-attempt-")]
        if len(matches) != 1:
            raise QmtFulldataShadowCoverageError("registered role capture is missing or duplicate")
        path = matches[0]
        raw = _raw(path)
        capture = verify_qmt_fulldata_shadow_capture(_json(raw, "capture"))
        digest = capture["capture_sha256"]
        relative = path.relative_to(root).as_posix()
        if path.name != f"{digest}.json" or not re.fullmatch(rf"captures/{re.escape(symbol)}/{adjustment}-attempt-[1-9][0-9]*/[0-9a-f]{{64}}\.json", relative):
            raise QmtFulldataShadowCoverageError("capture path differs")
        params = capture["derived_bound_request"]["params"]
        if params["symbol"] != symbol or params["dividend_type"] != {"raw":"none", "qfq":"front"}[adjustment] or params["period"] != registration["period"] or params["start"] != registration["start"].replace("-", "") or params["end"] != registration["end"].replace("-", "") or params["count"] != registration["count"]:
            raise QmtFulldataShadowCoverageError("capture request differs")
        terminal = capture["terminal"]
        entries.append({"symbol": symbol, "adjustment": adjustment, "relative_path": relative, "capture_sha256": digest, "artifact_bytes_sha256": hashlib.sha256(raw).hexdigest(), "request_sha256": capture["submit"]["request_sha256"], "ack_sha256": capture["submit"]["ack_sha256"], "response_sha256": terminal["response_sha256"], "request_id": capture["derived_bound_request"]["request_id"], "params": params, "last_date": terminal["response"]["data"][symbol]["index"][-1], "started_at": capture["started_at"], "submitted_at": capture["submitted_at"], "completed_at": capture["completed_at"], "authority": {key: capture[key] for key in ("schema_version", "authority_grade", "decision_eligible", "decision_authority", "actions", "source", "finality", "volume_unit")}})
        expected_paths.add(path)
    if set(found) != expected_paths:
        raise QmtFulldataShadowCoverageError("captures contain extra artifact")
    reg_projection = {"schema_version": registration["schema_version"], "registration_sha256": hashlib.sha256(registration_raw).hexdigest(), "registered_at": registration["registered_at"], "capture_revision": registration["capture_revision"], "target_asof": registration["target_asof"], "start": registration["start"], "end": registration["end"], "count": registration["count"], "period": registration["period"], "authority_grade": registration["authority_grade"], "decision_eligible": False, "actions": [], "permitted_use": registration["permitted_use"], "request_count": 23}
    identity_projection = {"schema_version": receipt["schema_version"], "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(), "recorded_at": receipt["recorded_at"], "registered_capture_revision": receipt["registered_capture_revision"], "executed_capture_revision": receipt["executed_capture_revision"], "revision_change_reason": receipt["revision_change_reason"], "scope_changed": False, "recorded_after_execution_started": True, "authorization_status": "post_start_identity_only_not_pre_authorization", "identity_authority": {key: receipt[key] for key in ("authority_grade", "decision_eligible", "actions", "formal_admission")}, "first_bound_run_identity": {"relative_path": identity.relative_to(root).as_posix(), "sha256": hashlib.sha256(identity_raw).hexdigest(), "head_revision": receipt["executed_capture_revision"]}}
    unsigned = {"schema_version": SCHEMA_VERSION, "coverage_kind": "registered_shadow_artifact_presence", "permitted_use": "offline_review_only", "authority_grade": "shadow", "decision_eligible": False, "decision_authority": False, "actions": [], "formal_admission": False, "registration": reg_projection, "execution_identity": identity_projection, "artifact_count": 23, "roles": entries, "roles_sha256": hashlib.sha256(_canonical(entries)).hexdigest()}
    return {**unsigned, "manifest_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest()}


def write_coverage(evidence_root: str | Path) -> Path:
    root = Path(evidence_root).resolve(strict=True)
    value = build_coverage(root)
    return _write_content_addressed(root / "coverage", value["manifest_sha256"], _canonical(value), QmtFulldataShadowCoverageError)
