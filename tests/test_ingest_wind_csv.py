"""scripts/ingest_wind_csv.py 的防御规则测试（三轮 Codex 交叉审查后补）。

覆盖：正常入库 + 覆盖记录 + 按文件归档；停牌行跳过；非法行拒收；
表头错误；跨文件冲突整日拒收；覆盖空洞不记录 sync_coverage；
空交易日历时一律不记录覆盖。
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ingest_wind_csv",
    str(Path(__file__).resolve().parent.parent / "scripts" / "ingest_wind_csv.py"),
)
ingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingest)

HEADER = "trade_date,wind_code,open,high,low,close,volume\n"


def _csv(code: str, day: str, close: float = 1.0, volume: str = "100") -> str:
    return f"{day},{code},1,1,1,{close},{volume}\n"


def _seed_calendar(db, days):
    """在库内铺出交易日历（用其他代码的占位行）。"""
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily (code TEXT, date TEXT, open REAL, "
        "high REAL, low REAL, close REAL, volume REAL, source TEXT, "
        "adjustment_mode TEXT, adjustment_version TEXT, retrieved_at TEXT, "
        "is_final INTEGER, receipt_id INTEGER, PRIMARY KEY (code, date, "
        "source, adjustment_mode, adjustment_version))")
    for d in days:
        conn.execute(
            "INSERT OR IGNORE INTO daily VALUES (?,?,1,1,1,1,1,'baostock','qfq',"
            "'v','',1,NULL)", ("000001.SZ", d))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    db = tmp_path / "cache.sqlite"
    monkeypatch.setenv("STOCKDATA_DB", str(db))
    return csv_dir, db


def _run(csv_dir, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ingest_wind_csv.py", str(csv_dir)])
    return ingest.main()


def _rows(db, code="600519.SH"):
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "daily" not in tables:
        conn.close()
        return [], []
    rows = conn.execute(
        "SELECT date, close, volume, source, adjustment_version FROM daily "
        "WHERE code=? ORDER BY date", (code,)).fetchall()
    cov = conn.execute(
        "SELECT start_date, end_date FROM sync_coverage WHERE code=?", (code,)).fetchall()
    conn.close()
    return rows, cov


class TestHappyPath:
    def test_ingest_and_archive(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02", "2024-01-03"])
        (csv_dir / "a.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02") + _csv("600519.SH", "2024-01-03"))
        assert _run(csv_dir, monkeypatch) == 0
        rows, cov = _rows(db)
        assert [r[0] for r in rows] == ["2024-01-02", "2024-01-03"]
        assert rows[0][2] == 100.0
        assert rows[0][3:] == ("wind", "wind-fwd-v1")
        assert cov == [("2024-01-02", "2024-01-03")]
        assert not list(csv_dir.glob("*.csv"))  # 已归档
        assert (csv_dir / "ingested" / "a.csv").exists()


class TestEmptyCalendar:
    def test_no_coverage_recorded_when_calendar_empty(self, env, monkeypatch):
        # 空日历无法验证覆盖完整性：一律不记录 coverage，退出码 2，
        # 文件不归档（但合法行仍入库）。
        csv_dir, db = env
        (csv_dir / "a.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02") + _csv("600519.SH", "2024-01-03"))
        assert _run(csv_dir, monkeypatch) == 2
        rows, cov = _rows(db)
        assert len(rows) == 2
        assert cov == []
        assert (csv_dir / "a.csv").exists()  # 未归档

    def test_rerun_cannot_self_certify_coverage(self, env, monkeypatch):
        # 第一轮空日历写入 wind 行后，第二轮不得拿这些本身份的行
        # 充当日历证据来自证覆盖完整。
        csv_dir, db = env
        (csv_dir / "a.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02") + _csv("600519.SH", "2024-01-03"))
        assert _run(csv_dir, monkeypatch) == 2
        assert _run(csv_dir, monkeypatch) == 2  # 重跑仍不记录
        _, cov = _rows(db)
        assert cov == []


class TestSuspensionSkip:
    def test_empty_volume_row_skipped(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02", "2024-01-03"])
        (csv_dir / "a.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02")
            + _csv("600519.SH", "2024-01-03", volume=""))
        assert _run(csv_dir, monkeypatch) == 0
        rows, _ = _rows(db)
        assert [r[0] for r in rows] == ["2024-01-02"]


class TestInvalidRows:
    @pytest.mark.parametrize("row", [
        "2024-01-02,600519.SH,nan,1,1,1,100\n",        # NaN
        "2024-01-02,600519.SH,-1,1,1,1,100\n",          # 负价格
        "2024-01-02,600519.SH,1,1,1,1,-5\n",            # 负成交量
        "2024-13-99,600519.SH,1,1,1,1,100\n",           # 非法日期
        "2024-01-02,BADCODE,1,1,1,1,100\n",             # 非法代码
        "2024-01-02,600519.SH,5,1,1,1,100\n",           # open > high
        "2024-01-02,600519.SH,1,1,1,1,inf\n",           # inf 成交量
    ])
    def test_invalid_row_rejected(self, env, monkeypatch, row):
        csv_dir, db = env
        (csv_dir / "a.csv").write_text(HEADER + row)
        assert _run(csv_dir, monkeypatch) == 2
        rows, _ = _rows(db)
        assert rows == []
        assert (csv_dir / "a.csv").exists()  # 脏文件不归档


class TestBadHeader:
    def test_missing_columns_rejected(self, env, monkeypatch):
        csv_dir, db = env
        (csv_dir / "a.csv").write_text("date,code,close\n2024-01-02,600519.SH,1\n")
        assert _run(csv_dir, monkeypatch) == 1
        rows, _ = _rows(db)
        assert rows == []

    def test_bad_header_counts_as_problem_when_other_files_ok(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02"])
        (csv_dir / "bad.csv").write_text("date,code,close\n2024-01-02,600519.SH,1\n")
        (csv_dir / "good.csv").write_text(HEADER + _csv("000063.SZ", "2024-01-02"))
        assert _run(csv_dir, monkeypatch) == 2
        rows, _ = _rows(db, code="000063.SZ")
        assert len(rows) == 1
        assert (csv_dir / "bad.csv").exists()              # 坏文件留在原地
        assert (csv_dir / "ingested" / "good.csv").exists()  # 干净文件仍归档


class TestConflicts:
    def test_conflicting_day_rejected(self, env, monkeypatch):
        csv_dir, db = env
        (csv_dir / "a.csv").write_text(HEADER + _csv("600519.SH", "2024-01-02", close=1.0))
        # 数值不同但各自合法的同代码同日行 → 冲突
        (csv_dir / "b.csv").write_text(
            HEADER + "2024-01-02,600519.SH,9.9,9.9,9.9,9.9,100\n")
        assert _run(csv_dir, monkeypatch) == 2
        rows, _ = _rows(db)
        assert rows == []  # 冲突日整日拒收

    def test_identical_duplicates_ok(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02"])
        (csv_dir / "a.csv").write_text(HEADER + _csv("600519.SH", "2024-01-02"))
        (csv_dir / "b.csv").write_text(HEADER + _csv("600519.SH", "2024-01-02"))
        assert _run(csv_dir, monkeypatch) == 0
        rows, _ = _rows(db)
        assert len(rows) == 1


class TestPartialArchive:
    def test_clean_file_archived_dirty_file_stays(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02"])
        (csv_dir / "clean.csv").write_text(HEADER + _csv("000063.SZ", "2024-01-02"))
        (csv_dir / "dirty.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02")
            + "2024-01-02,600519.SH,nan,1,1,1,100\n")
        assert _run(csv_dir, monkeypatch) == 2
        rows, _ = _rows(db)
        assert len(rows) == 1  # dirty 文件的合法行仍入库
        assert (csv_dir / "ingested" / "clean.csv").exists()
        assert (csv_dir / "dirty.csv").exists()

    def test_suspension_evidence_file_stays_when_code_has_hole(self, env, monkeypatch):
        # 代码有未解释覆盖空洞时，含其停牌证据的文件不得归档（证据需保留）。
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        (csv_dir / "bars.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02") + _csv("600519.SH", "2024-01-03")
            + _csv("600519.SH", "2024-01-05"))          # 01-04 无数据且无证据 → 空洞
        (csv_dir / "susp.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-03", volume=""))  # 停牌证据文件
        assert _run(csv_dir, monkeypatch) == 2
        assert (csv_dir / "bars.csv").exists()
        assert (csv_dir / "susp.csv").exists()  # 证据文件不得归档

    def test_suspension_evidence_file_archived_when_clean(self, env, monkeypatch):
        # 无空洞时，只含停牌证据的干净文件正常归档。
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02", "2024-01-03"])
        (csv_dir / "bars.csv").write_text(HEADER + _csv("600519.SH", "2024-01-02"))
        (csv_dir / "susp.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-03", volume=""))
        assert _run(csv_dir, monkeypatch) == 0
        assert (csv_dir / "ingested" / "bars.csv").exists()
        assert (csv_dir / "ingested" / "susp.csv").exists()


class TestCoverageHoles:
    def test_unexplained_hole_blocks_coverage(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        # 目标代码缺 01-04 且未观测到停牌 → 覆盖不得记录
        (csv_dir / "a.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02") + _csv("600519.SH", "2024-01-03")
            + _csv("600519.SH", "2024-01-05"))
        assert _run(csv_dir, monkeypatch) == 2
        rows, cov = _rows(db)
        assert len(rows) == 3
        assert cov == []  # 有未解释空洞，不记录覆盖

    def test_suspension_explains_hole(self, env, monkeypatch):
        csv_dir, db = env
        _seed_calendar(db, ["2024-01-02", "2024-01-03", "2024-01-04"])
        # 01-03 停牌（空成交量）→ 空洞可解释，覆盖正常记录
        (csv_dir / "a.csv").write_text(
            HEADER + _csv("600519.SH", "2024-01-02")
            + _csv("600519.SH", "2024-01-03", volume="")
            + _csv("600519.SH", "2024-01-04"))
        assert _run(csv_dir, monkeypatch) == 0
        _, cov = _rows(db)
        assert cov == [("2024-01-02", "2024-01-04")]
