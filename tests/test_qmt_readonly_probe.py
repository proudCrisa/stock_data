from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

import pandas as pd
import pytest

from stockdata import qmt_readonly_probe as probe


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _reseal(artifact):
    unsigned = {key: value for key, value in artifact.items()
                if key != "artifact_sha256"}
    artifact["artifact_sha256"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
    return artifact


class FakeXtdata:
    def __init__(self):
        self.calls = []

    def get_market_data_ex(self, **kwargs):
        self.calls.append(kwargs)
        scale = 0.9 if kwargs["dividend_type"] == "front" else 1.0
        code = kwargs["stock_list"][0]
        frame = pd.DataFrame([
            {
                "time": "20260827", "open": 10.0 * scale,
                "high": 10.2 * scale, "low": 9.9 * scale,
                "close": 10.1 * scale, "volume": 1000, "amount": 10100,
                "suspendFlag": 0,
            },
            {
                "time": "20260828", "open": 10.1 * scale,
                "high": 10.3 * scale, "low": 10.0 * scale,
                "close": 10.2 * scale, "volume": 1200, "amount": 12240,
                "suspendFlag": 0,
            },
        ], index=["20260827", "20260828"])
        return {code: frame}


def _build(monkeypatch, *, codes=None, modes=None, xtdata=None):
    monkeypatch.setattr(probe.platform, "system", lambda: "Windows")
    return probe.build_qmt_readonly_probe(
        codes=codes or ["600000.SH", "159915.SZ"],
        start="2026-08-27",
        end="2026-08-28",
        adjustment_modes=modes,
        created_at="2026-08-27T00:00:00+00:00",
        xtdata_module=xtdata or FakeXtdata(),
    )


def test_capture_uses_only_local_read_api_and_closes_all_pairs(monkeypatch):
    xtdata = FakeXtdata()
    artifact = _build(monkeypatch, xtdata=xtdata)

    assert artifact["authority_grade"] == "diagnostic"
    assert artifact["decision_eligible"] is False
    assert artifact["decision_authority"] is False
    assert artifact["actions"] == []
    assert [(item["code"], item["adjustment_mode"])
            for item in artifact["observations"]] == [
        ("159915.SZ", "raw"), ("159915.SZ", "qfq"),
        ("600000.SH", "raw"), ("600000.SH", "qfq"),
    ]
    assert len(xtdata.calls) == 4
    assert {call["dividend_type"] for call in xtdata.calls} == {"none", "front"}
    assert all(call["fill_data"] is False for call in xtdata.calls)
    assert all(call["period"] == "1d" and call["count"] == 256
               for call in xtdata.calls)
    assert all(item["coverage"]["status"] == "observed_subset_unverified"
               for item in artifact["observations"])
    assert artifact["producer"]["identity_status"] == "unverified"
    assert probe.verify_qmt_readonly_probe(artifact) is artifact


def test_content_addressed_write_and_mac_side_load_are_idempotent(
    monkeypatch, tmp_path
):
    artifact = _build(monkeypatch)

    first = probe.write_qmt_readonly_probe(tmp_path, artifact)
    second = probe.write_qmt_readonly_probe(tmp_path, artifact)

    assert first == second
    assert first.name == f"{artifact['artifact_sha256']}.json"
    assert probe.load_qmt_readonly_probe(first) == artifact


def test_verify_cli_does_not_require_xtquant(monkeypatch, tmp_path):
    artifact = _build(monkeypatch)
    path = probe.write_qmt_readonly_probe(tmp_path, artifact)
    script = probe.Path(probe.__file__).parents[1] \
        / "scripts" / "export_qmt_readonly_probe.py"

    result = subprocess.run(
        [sys.executable, str(script), "verify", "--artifact", str(path)],
        cwd=script.parent.parent,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "verified_diagnostic_only"


def test_rejects_caps_before_calling_qmt(monkeypatch):
    xtdata = FakeXtdata()
    monkeypatch.setattr(probe.platform, "system", lambda: "Windows")

    with pytest.raises(probe.QmtReadonlyProbeError, match="at most 3 codes"):
        probe.build_qmt_readonly_probe(
            codes=["600000.SH", "600001.SH", "600002.SH", "600003.SH"],
            start="2026-08-27", end="2026-08-28", xtdata_module=xtdata,
        )
    with pytest.raises(probe.QmtReadonlyProbeError, match="180 calendar days"):
        probe.build_qmt_readonly_probe(
            codes=["600000.SH"], start="2026-01-01", end="2026-07-01",
            xtdata_module=xtdata,
        )
    assert xtdata.calls == []


def test_rejects_partial_or_cross_code_response(monkeypatch):
    class WrongXtdata:
        def get_market_data_ex(self, **kwargs):
            return {"000001.SZ": pd.DataFrame()}

    with pytest.raises(probe.QmtReadonlyProbeError, match="identity mismatch"):
        _build(
            monkeypatch, codes=["600000.SH"], modes=["raw"],
            xtdata=WrongXtdata(),
        )


def test_rejects_oversized_frame_before_iteration(monkeypatch):
    class OversizedFrame:
        def __len__(self):
            return 257

        def iterrows(self):
            raise AssertionError("oversized frame must not be iterated")

    class OversizedXtdata:
        def get_market_data_ex(self, **kwargs):
            return {kwargs["stock_list"][0]: OversizedFrame()}

    with pytest.raises(probe.QmtReadonlyProbeError, match="row cap exceeded"):
        _build(
            monkeypatch, codes=["600000.SH"], modes=["raw"],
            xtdata=OversizedXtdata(),
        )


def test_rejects_raw_qfq_date_panel_mismatch(monkeypatch):
    class MismatchXtdata(FakeXtdata):
        def get_market_data_ex(self, **kwargs):
            result = super().get_market_data_ex(**kwargs)
            if kwargs["dividend_type"] == "front":
                result[kwargs["stock_list"][0]] = result[
                    kwargs["stock_list"][0]
                ].iloc[:1]
            return result

    with pytest.raises(probe.QmtReadonlyProbeError, match="date coverage differs"):
        _build(
            monkeypatch, codes=["600000.SH"], xtdata=MismatchXtdata(),
        )


def test_rejects_invalid_source_rows(monkeypatch):
    class InvalidXtdata(FakeXtdata):
        def get_market_data_ex(self, **kwargs):
            result = super().get_market_data_ex(**kwargs)
            result[kwargs["stock_list"][0]].loc["20260827", "close"] = float("nan")
            return result

    with pytest.raises(probe.QmtReadonlyProbeError, match="finite numeric"):
        _build(
            monkeypatch, codes=["600000.SH"], modes=["raw"],
            xtdata=InvalidXtdata(),
        )


def test_rejects_resealed_authority_or_identity_escalation(monkeypatch):
    artifact = _build(monkeypatch)
    escalated = copy.deepcopy(artifact)
    escalated["decision_authority"] = True
    _reseal(escalated)

    with pytest.raises(probe.QmtReadonlyProbeError, match="authority contract"):
        probe.verify_qmt_readonly_probe(escalated)

    crossed = copy.deepcopy(artifact)
    crossed["observations"][0]["code"] = "600000.SH"
    _reseal(crossed)
    with pytest.raises(
        probe.QmtReadonlyProbeError,
        match="observation identity|identity coverage",
    ):
        probe.verify_qmt_readonly_probe(crossed)


def test_rejects_tampered_rows_even_when_outer_artifact_is_resealed(monkeypatch):
    artifact = _build(monkeypatch)
    artifact["observations"][0]["rows"][0]["close"] = 99.0
    _reseal(artifact)

    with pytest.raises(probe.QmtReadonlyProbeError, match="row closure"):
        probe.verify_qmt_readonly_probe(artifact)


def test_rejects_duplicate_json_keys_and_noncanonical_bytes(monkeypatch, tmp_path):
    artifact = _build(monkeypatch)
    duplicate = tmp_path / "duplicate.json"
    raw = _canonical(artifact).decode("ascii")
    duplicate.write_text(raw.replace("{", '{"actions":[],', 1), encoding="ascii")
    with pytest.raises(probe.QmtReadonlyProbeError, match="duplicate JSON key"):
        probe.load_qmt_readonly_probe(duplicate)

    formatted = tmp_path / "formatted.json"
    formatted.write_text(json.dumps(artifact, indent=2), encoding="ascii")
    with pytest.raises(probe.QmtReadonlyProbeError, match="not canonical"):
        probe.load_qmt_readonly_probe(formatted)


def test_existing_content_address_conflict_is_not_overwritten(monkeypatch, tmp_path):
    artifact = _build(monkeypatch)
    output = tmp_path / f"{artifact['artifact_sha256']}.json"
    output.write_text("conflict", encoding="ascii")

    with pytest.raises(probe.QmtReadonlyProbeError, match="conflicts"):
        probe.write_qmt_readonly_probe(tmp_path, artifact)
    assert output.read_text(encoding="ascii") == "conflict"


def test_probe_source_contains_no_download_subscription_or_execution_api():
    source = probe.Path(probe.__file__).read_text(encoding="utf-8").lower()
    script = (probe.Path(probe.__file__).parents[1]
              / "scripts" / "export_qmt_readonly_probe.py").read_text(
                  encoding="utf-8"
              ).lower()
    forbidden = (
        "xttrader", "download_history_data", "subscribe_quote", "passorder",
        "order_stock", "cancel_order_stock",
    )
    assert all(token not in source + script for token in forbidden)
