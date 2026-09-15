from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from stockdata import qmt_fulldata_offline as qmt
from stockdata.daily_bar_product import _hash
from stockdata.local_daily_snapshot import _timestamp


CAPTURE_ROOT = Path("/Users/cdzhangxueli/.stockdata/a08-qmt-recovery-20260914-01/sealed-input")
CAPTURE_TREE = "0808e140f3b97e102101eb02eae926bc9abba98db7e21486eefa6d06d0d75762"
CAPTURE_0915_ROOT = Path(
    "/Users/cdzhangxueli/.stockdata/a08-qmt-recovery-20260914-01/"
    "curated-sealed-input-20260915.LQQDzG")
CAPTURE_0915_TREE = "facea5f1fab254abd4865154b5b0315a2653b66eebdd9ffd6f355e574a67db8d"
SYMBOLS = ["511010.SH", "518880.SH", "561980.SH"]
M1_ROOT = Path(
    "/Users/cdzhangxueli/.stockdata/trading-candidate-eod-timeout-fix-20260907/"
    "canonical-final-evidence/policy-news-real-source-attempt-01/"
    "current-observation-positive/m1-qmt-fulldata-live-uAJzPG")


def _m1_capture(symbol, adjustment):
    request_path = M1_ROOT / "requests" / f"{symbol}-{adjustment}.json"
    response_path = (M1_ROOT / "supplement/retry-response.json"
                     if (symbol, adjustment) == ("518880.SH", "raw")
                     else M1_ROOT / "responses" / f"{symbol}-{adjustment}.json")
    request_raw = request_path.read_text(encoding="utf-8")
    response_raw = response_path.read_text(encoding="utf-8")
    request, response = json.loads(request_raw), json.loads(response_raw)
    evidence = {
        "schema_version": qmt.VOLUME_EVIDENCE_SCHEMA,
        "evidence_id": qmt.PRODUCTION_VOLUME_EVIDENCE_ID,
        "volume_unit": "hand",
        "request_sha256": _hash(request),
        "response_sha256": _hash(response),
        **qmt._CROSSPROOF,
    }
    evidence["evidence_sha256"] = _hash(evidence)
    return qmt.build_capture(
        request=request, response=response,
        observed_at=qmt.APPROVED_VOLUME_PAIRS[(symbol, adjustment)][2],
        volume_unit_evidence=evidence,
        request_raw=request_raw, response_raw=response_raw)


def test_loads_exact_a08_six_role_capture_and_replays_once():
    sealed = qmt.load_sealed_capture_directory(
        CAPTURE_ROOT, expected_tree_sha256=CAPTURE_TREE,
        asof="2026-09-14", expected_symbols=SYMBOLS)
    assert set(sealed) == {(symbol, role) for symbol in SYMBOLS
                           for role in ("execution", "signal")}
    for (symbol, role), capture in sealed.items():
        raw_volume = capture["response"]["data"][symbol]["columns"]["volume"][-1]
        rows, adjustment = qmt.replay(
            capture, symbol=symbol, start="2025-07-21", asof="2026-09-14",
            adjustment="raw" if role == "execution" else "qfq",
            cutoff=_timestamp("2026-09-14T18:00:00+08:00"))
        assert adjustment == ("raw" if role == "execution" else "qfq")
        assert rows[-1]["date"] == "2026-09-14"
        assert rows[-1]["volume"] == raw_volume * 100.0


def test_rejects_tree_change(tmp_path):
    copied = tmp_path / "sealed"
    shutil.copytree(CAPTURE_ROOT, copied)
    target = copied / "results.json"
    target.write_text(target.read_text() + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="directory identity"):
        qmt.load_sealed_capture_directory(
            copied, expected_tree_sha256=CAPTURE_TREE,
            asof="2026-09-14", expected_symbols=SYMBOLS)


def test_rejects_symlinked_capture_root(tmp_path):
    linked = tmp_path / "sealed"
    linked.symlink_to(CAPTURE_ROOT, target_is_directory=True)
    with pytest.raises(ValueError, match="directory identity"):
        qmt.load_sealed_capture_directory(
            linked, expected_tree_sha256=CAPTURE_TREE,
            asof="2026-09-14", expected_symbols=SYMBOLS)


def test_old_m1_evidence_cannot_masquerade_as_a08():
    sealed = qmt.load_sealed_capture_directory(
        CAPTURE_ROOT, expected_tree_sha256=CAPTURE_TREE,
        asof="2026-09-14", expected_symbols=SYMBOLS)
    capture = deepcopy(sealed[("511010.SH", "execution")])
    capture["volume_unit_evidence"]["evidence_id"] = (
        "approved-m1-qmt-fulldata-volume-hand/1")
    with pytest.raises(ValueError, match="volume"):
        qmt.replay(
            capture, symbol="511010.SH", start="2025-07-21",
            asof="2026-09-14", adjustment="raw",
            cutoff=_timestamp("2026-09-14T18:00:00+08:00"))


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize("adjustment", ["raw", "qfq"])
def test_replays_each_frozen_m1_pair(symbol, adjustment):
    rows, actual = qmt.replay(
        _m1_capture(symbol, adjustment), symbol=symbol,
        start="2025-01-01", asof="2026-09-11", adjustment=adjustment,
        cutoff=_timestamp("2026-09-11T19:00:00+08:00"))
    assert actual == adjustment
    assert rows[-1]["date"] == "2026-09-11"


def test_rejects_m1_bytes_with_a08_evidence_profile():
    capture = _m1_capture("511010.SH", "raw")
    evidence = capture["volume_unit_evidence"]
    evidence.update(evidence_id=qmt.A08_VOLUME_EVIDENCE_ID,
                    **qmt.A08_CROSSPROOF)
    evidence["evidence_sha256"] = _hash({
        key: value for key, value in evidence.items()
        if key != "evidence_sha256"})
    with pytest.raises(ValueError, match="observation differs approved completion"):
        qmt.replay(
            capture, symbol="511010.SH", start="2025-01-01",
            asof="2026-09-11", adjustment="raw",
            cutoff=_timestamp("2026-09-11T19:00:00+08:00"))


def test_rejects_a08_bytes_with_m1_evidence_profile():
    sealed = qmt.load_sealed_capture_directory(
        CAPTURE_ROOT, expected_tree_sha256=CAPTURE_TREE,
        asof="2026-09-14", expected_symbols=SYMBOLS)
    capture = sealed[("511010.SH", "execution")]
    evidence = capture["volume_unit_evidence"]
    evidence.update(evidence_id=qmt.PRODUCTION_VOLUME_EVIDENCE_ID,
                    **qmt._CROSSPROOF)
    evidence["evidence_sha256"] = _hash({
        key: value for key, value in evidence.items()
        if key != "evidence_sha256"})
    with pytest.raises(ValueError, match="observation differs approved completion"):
        qmt.replay(
            capture, symbol="511010.SH", start="2025-07-21",
            asof="2026-09-14", adjustment="raw",
            cutoff=_timestamp("2026-09-14T18:00:00+08:00"))


def test_loads_exact_0915_single_post_capture_and_replays_once():
    sealed = qmt.load_sealed_capture_directory(
        CAPTURE_0915_ROOT, expected_tree_sha256=CAPTURE_0915_TREE,
        asof="2026-09-15", expected_symbols=SYMBOLS)
    assert set(sealed) == {(symbol, role) for symbol in SYMBOLS
                           for role in ("execution", "signal")}
    for (symbol, role), capture in sealed.items():
        rows, adjustment = qmt.replay(
            capture, symbol=symbol, start="2025-07-22", asof="2026-09-15",
            adjustment="raw" if role == "execution" else "qfq",
            cutoff=_timestamp("2026-09-15T18:00:00+08:00"))
        assert adjustment == ("raw" if role == "execution" else "qfq")
        assert rows[-1]["date"] == "2026-09-15"


def test_0915_rejects_wire_ack_count_state_and_scope_tampering(tmp_path):
    mutations = (
        ("wire", "attempts/511010.SH-raw-request.json", "20250722", "20250721"),
        ("ack", "attempts/511010.SH-raw-ack.json", "88e398f4", "bad-id"),
        ("count", "results.json", '"post_count":1', '"post_count":2'),
        ("state", "attempts/511010.SH-raw-state.json", "88e398f4", "bad-id"),
        ("extra", None, None, None),
    )
    for name, relative, old, new in mutations:
        copied = tmp_path / name
        shutil.copytree(CAPTURE_0915_ROOT, copied)
        if relative is None:
            (copied / "unexpected.json").write_text("{}", encoding="utf-8")
        else:
            target = copied / relative
            target.write_text(target.read_text(encoding="utf-8").replace(old, new),
                              encoding="utf-8")
        with pytest.raises(ValueError):
            qmt.load_sealed_capture_directory(
                copied, expected_tree_sha256=qmt._tree_sha256(copied),
                asof="2026-09-15", expected_symbols=SYMBOLS)
