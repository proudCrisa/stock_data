from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from stockdata import qmt_frozen_research as frozen


def _latest() -> dict:
    return {
        "account": {"cash": 1}, "account_id": "secret", "errors": [],
        "generated": "2026-09-01T08:56:24",
        "request": {
            "count": 2, "fields": list(frozen.FIELDS), "period": "1d",
            "symbols": ["600519.SH", "000001.SZ"],
        },
        "market": {
            symbol: {
                "index": ["20260828", "20260831"],
                "columns": {
                    "open": [10.0, 10.5], "high": [11.0, 11.5],
                    "low": [9.0, 10.0], "close": [10.5, 11.0],
                    "volume": [100.0, 120.0], "amount": [1000.0, 1320.0],
                },
            }
            for symbol in ("600519.SH", "000001.SZ")
        },
    }


def _write_capture(tmp_path, latest=None):
    latest = latest or _latest()
    raw = json.dumps(latest, ensure_ascii=False).encode("utf-8")
    latest_path = tmp_path / "latest.json"
    receipt_path = tmp_path / "receipt.json"
    latest_path.write_bytes(raw)
    receipt = {
        "schema": frozen.RECEIPT_SCHEMA,
        "capture_id": "20260901T074202.162754Z-test",
        "captured_at": "2026-09-01T07:41:55.661550+00:00",
        "endpoint": "/latest", "http_status": 200,
        "content_type": "application/json; charset=utf-8",
        "byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "symbol_count": len(latest["request"]["symbols"]),
        "row_count": sum(len(item["index"]) for item in latest["market"].values()),
        "generated": latest["generated"], "validation_status": "verified",
        "validation_errors": [], "source": "qmt-local-tunnel",
        "authority": {
            "advice": False, "evidence_grade": False, "judge": False,
            "production": False, "release": False,
        },
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return latest_path, receipt_path


def test_builds_shadow_only_dataset_and_strips_account(tmp_path):
    latest, receipt = _write_capture(tmp_path)
    dataset = frozen.build_qmt_frozen_research(latest, receipt)

    assert dataset["schema_version"] == frozen.SCHEMA_VERSION
    assert dataset["symbol_count"] == 2
    assert dataset["source_row_count"] == 4
    assert dataset["row_count"] == 4
    assert dataset["dropped_zero_placeholder_count"] == 0
    assert dataset["price_identity"] == {
        "adjustment": "unbound", "volume_unit": "unbound",
        "amount_unit": "unbound", "finality": "unverified",
    }
    assert all(value is False for value in dataset["authority"].values())
    assert dataset["source_binding"]["source_declared"] == "qmt-local-tunnel"
    assert dataset["source_binding"]["transport_topology"] == \
        "qmt_cloud_relay_unverified"
    assert dataset["source_binding"]["generated_timezone"] == "unbound"
    encoded = frozen._canonical(dataset)
    assert b'"account"' not in encoded
    assert b'"account_id"' not in encoded
    assert frozen.verify_qmt_frozen_research(dataset) == dataset


def test_rejects_latest_hash_drift(tmp_path):
    latest, receipt = _write_capture(tmp_path)
    latest.write_bytes(latest.read_bytes() + b" ")

    with pytest.raises(frozen.QmtFrozenResearchError, match="match receipt"):
        frozen.build_qmt_frozen_research(latest, receipt)


def test_rejects_bad_ohlc(tmp_path):
    payload = _latest()
    payload["market"]["600519.SH"]["columns"]["low"][0] = 12.0
    latest, receipt = _write_capture(tmp_path, payload)

    with pytest.raises(frozen.QmtFrozenResearchError, match="OHLCVA"):
        frozen.build_qmt_frozen_research(latest, receipt)


def test_drops_complete_zero_placeholder_bar(tmp_path):
    payload = _latest()
    for field in frozen.FIELDS:
        payload["market"]["600519.SH"]["columns"][field][0] = 0
    latest, receipt = _write_capture(tmp_path, payload)

    dataset = frozen.build_qmt_frozen_research(latest, receipt)
    assert dataset["source_row_count"] == 4
    assert dataset["row_count"] == 3
    assert dataset["dropped_zero_placeholder_count"] == 1
    assert dataset["market"]["600519.SH"]["index"] == ["20260831"]


def test_content_addressed_write_is_idempotent(tmp_path):
    capture = tmp_path / "capture"
    output = tmp_path / "output"
    capture.mkdir()
    output.mkdir()
    latest, receipt = _write_capture(capture)
    dataset = frozen.build_qmt_frozen_research(latest, receipt)

    first = frozen.write_qmt_frozen_research(output, dataset)
    assert frozen.write_qmt_frozen_research(output, dataset) == first
    assert first.name == f"{dataset['dataset_sha256']}.json"
    assert frozen.load_qmt_frozen_research(first) == dataset


@pytest.mark.parametrize("target", ["latest", "receipt"])
def test_rejects_duplicate_keys_and_nan(tmp_path, target):
    latest, receipt = _write_capture(tmp_path)
    if target == "latest":
        raw = latest.read_bytes().replace(b'{', b'{"generated":NaN,', 1)
        latest.write_bytes(raw)
        parsed = json.loads(receipt.read_text(encoding="utf-8"))
        parsed["byte_count"] = len(raw)
        parsed["sha256"] = hashlib.sha256(raw).hexdigest()
        receipt.write_text(json.dumps(parsed), encoding="utf-8")
    else:
        raw = receipt.read_bytes().replace(b'{', b'{"schema":"duplicate",', 1)
        receipt.write_bytes(raw)
    with pytest.raises(frozen.QmtFrozenResearchError, match="duplicate JSON key|non-finite"):
        frozen.build_qmt_frozen_research(latest, receipt)


def test_verifier_rejects_resealed_authority_escalation(tmp_path):
    latest, receipt = _write_capture(tmp_path)
    dataset = deepcopy(frozen.build_qmt_frozen_research(latest, receipt))
    dataset["authority"]["decision"] = True
    unsigned = {key: value for key, value in dataset.items() if key != "dataset_sha256"}
    dataset["dataset_sha256"] = frozen._sha256(unsigned)

    with pytest.raises(frozen.QmtFrozenResearchError, match="authority"):
        frozen.verify_qmt_frozen_research(dataset)


def test_verifier_rejects_resealed_receipt_count_drift(tmp_path):
    latest, receipt = _write_capture(tmp_path)
    dataset = frozen.build_qmt_frozen_research(latest, receipt)
    dataset["source_binding"]["capture_receipt"]["row_count"] += 1
    unsigned = {key: value for key, value in dataset.items() if key != "dataset_sha256"}
    dataset["dataset_sha256"] = frozen._sha256(unsigned)

    with pytest.raises(frozen.QmtFrozenResearchError, match="source receipt"):
        frozen.verify_qmt_frozen_research(dataset)
