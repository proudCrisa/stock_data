from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from stockdata.imported_frozen_daily import (
    ImportedFrozenDailyError,
    _canonical,
    _parse_canonical,
    _sha256,
    _validate_preimage,
    build_imported_frozen_daily_manifest,
    load_imported_frozen_daily_manifest,
    verify_imported_frozen_daily_manifest,
    write_imported_frozen_daily_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stage2c_sz159655_preimage.json"


def _preimage() -> dict:
    return json.loads(FIXTURE.read_text(encoding="ascii"))


def _build():
    return build_imported_frozen_daily_manifest(
        FIXTURE,
        expected_sha256="263b811f817cec371fc8cd71037ad6c6bfebc22bf2bdb5691c901723616a88e9",
        imported_at="2026-09-03T03:00:00+08:00",
    )


def test_builds_hash_bound_shadow_manifest_with_complete_import_receipt():
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    manifest = build_imported_frozen_daily_manifest(
        FIXTURE,
        expected_sha256=digest,
        imported_at="2026-09-03T03:00:00+08:00",
    )

    assert manifest["profile"] == "imported_frozen_observation"
    assert manifest["authority_grade"] == "shadow"
    assert manifest["decision_eligible"] is False
    assert manifest["decision_authority"] is False
    assert manifest["actions"] == []
    assert manifest["independence_status"] == "self_comparison_only"
    assert manifest["cutover_ready"] is False
    assert manifest["source_authentication"] == "unverified"
    assert manifest["reference_binding_status"] == "imported_hash_bound"
    assert manifest["permitted_uses"] == ["offline_replay", "shadow_compare"]
    receipt = manifest["input_receipts"][0]
    assert receipt["source_artifact_kind"] == "trading_daily_formal_preimage"
    assert receipt["source_file_sha256"] == digest
    assert receipt["observed_at"] is None
    assert receipt["response"] == _preimage()
    assert receipt["window"] == {
        "start": "2025-07-03",
        "end": "2026-08-27",
        "row_count": 282,
    }
    product = manifest["products"][0]
    assert product["independence_status"] == "self_comparison_only"
    assert product["cutover_ready"] is False
    assert product["authenticated_provenance"] is None
    assert product["claimed_provenance"] == _preimage()["provenance"]
    assert len(product["rows"]) == 282
    assert verify_imported_frozen_daily_manifest(manifest) == manifest


def test_hash_mismatch_fails_before_invalid_json_is_parsed(tmp_path):
    path = tmp_path / "preimage.json"
    path.write_bytes(b"not-json")

    with pytest.raises(ImportedFrozenDailyError, match="byte hash mismatch"):
        build_imported_frozen_daily_manifest(
            path,
            expected_sha256="0" * 64,
            imported_at="2026-09-03T03:00:00+08:00",
        )


@pytest.mark.parametrize("duplicate", [False, True])
def test_strict_parser_rejects_noncanonical_or_duplicate_json(duplicate):
    if duplicate:
        raw = _canonical(_preimage()).replace(b"{", b'{"schema_version":"forged",', 1)
    else:
        raw = json.dumps(_preimage(), ensure_ascii=True).encode("ascii")

    with pytest.raises(
        ImportedFrozenDailyError, match="duplicate JSON key|not canonical JSON"
    ):
        _parse_canonical(raw, "formal preimage")


def test_rejects_wrong_instrument_window_or_row_count():
    cases = []
    wrong_instrument = deepcopy(_preimage())
    wrong_instrument["instrument_code"] = "sz159980"
    cases.append(wrong_instrument)
    short = deepcopy(_preimage())
    short["rows"] = short["rows"][:-1]
    cases.append(short)
    wrong_window = deepcopy(_preimage())
    wrong_window["rows"][-1]["date"] = "2026-08-28"
    cases.append(wrong_window)

    for value in cases:
        with pytest.raises(ImportedFrozenDailyError, match="Stage 2C"):
            _validate_preimage(value)


def test_resealed_authority_tamper_cannot_replay_receipt():
    manifest = _build()
    manifest["decision_eligible"] = True
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_id", "manifest_sha256"}
    }
    manifest["manifest_sha256"] = _sha256(unsigned)
    manifest["manifest_id"] = f"shadow-{manifest['manifest_sha256']}"

    with pytest.raises(ImportedFrozenDailyError, match="does not replay"):
        verify_imported_frozen_daily_manifest(manifest)


def test_content_addressed_write_and_load_are_idempotent(tmp_path):
    manifest = _build()
    output_root = tmp_path / "output"
    output_root.mkdir()

    first = write_imported_frozen_daily_manifest(output_root, manifest)
    second = write_imported_frozen_daily_manifest(output_root, manifest)

    assert first == second
    assert first.name == f"{manifest['manifest_sha256']}.json"
    assert load_imported_frozen_daily_manifest(first) == manifest


@pytest.mark.parametrize("conflict", ["symlink", "hardlink"])
def test_writer_rejects_linked_output(conflict, tmp_path):
    manifest = _build()
    output_root = tmp_path / "output"
    output_root.mkdir()
    target = output_root / f"{manifest['manifest_sha256']}.json"
    other = tmp_path / "other.json"
    other.write_text("wrong", encoding="ascii")
    if conflict == "symlink":
        target.symlink_to(other)
    else:
        os.link(other, target)

    with pytest.raises(ImportedFrozenDailyError):
        write_imported_frozen_daily_manifest(output_root, manifest)
