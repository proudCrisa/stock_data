from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from stockdata import qmt_fulldata_offline as qmt
from stockdata.local_daily_snapshot import _timestamp


CAPTURE_ROOT = Path("/Users/cdzhangxueli/.stockdata/a08-qmt-recovery-20260914-01/sealed-input")
CAPTURE_TREE = "0808e140f3b97e102101eb02eae926bc9abba98db7e21486eefa6d06d0d75762"
SYMBOLS = ["511010.SH", "518880.SH", "561980.SH"]


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
