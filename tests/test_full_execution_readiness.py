from datetime import datetime
from zoneinfo import ZoneInfo

from stockdata.cache import Cache
from stockdata.forward_capture import _bind_cohort
from stockdata.forward_context import (
    SOURCE,
    CapturedMarketRows,
    capture_forward_context,
)
from stockdata.full_execution_readiness import check_full_execution_readiness
from stockdata.rqgm_provider_contract import REQUIRED_COMPONENTS


def test_price_or_context_readiness_cannot_unlock_full_readiness(tmp_path):
    database = tmp_path / "evidence.sqlite"
    cache = Cache(database)
    _bind_cohort(cache, {
        "symbols": ["000001.SZ"],
        "start": "2026-07-27",
        "source": "tencent",
        "adjustment_mode": "raw",
        "adjustment_version": "tencent-qt-daily-v1",
    })
    now = datetime(2026, 7, 27, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    rows = [{"symbol": "sz000001", "name": "平安银行", "trade": "10", "volume": 1}]
    captured = CapturedMarketRows(rows, {
        "observed_at": now.isoformat(timespec="seconds"),
        "source": SOURCE,
        "request": {"node": "hs_a"},
        "response": {
            "advertised_count": 1,
            "rows": rows,
            "raw_pages": [__import__("json").dumps(rows)],
        },
    })
    capture_forward_context(
        cache, "2026-07-27", fetcher=lambda: captured, now=now
    )
    cache.close()

    report = check_full_execution_readiness(
        database,
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        panel={("000001.SZ", "2026-07-27")},
    )

    assert report["ready"] is False
    assert set(report["components"]) == set(REQUIRED_COMPONENTS)
    assert isinstance(report["components"]["decision_context"]["ready"], bool)
    assert report["components"]["trading_calendar"]["ready"] is False
    assert report["components"]["universe"]["ready"] is False
    assert report["components"]["instrument_status"]["ready"] is False
    assert report["components"]["corporate_actions"]["ready"] is False
    assert report["components"]["market_rules"]["ready"] is False
    assert report["components"]["availability_records"]["ready"] is False
    assert {
        (item["component"], item["code"]) for item in report["blockers"]
    } >= {
        ("universe", "forward_universe_publisher_key_not_enrolled"),
        ("instrument_status", "instrument_status_is_activity_proxy"),
        ("corporate_actions", "missing_corporate_action_tables"),
        ("market_rules", "official_rulebook_bundle_not_enrolled"),
        ("trading_calendar", "signed_trading_calendar_not_enrolled"),
        (
            "availability_records",
            "complete_component_availability_records_not_bound",
        ),
    }


def test_full_readiness_is_read_only_for_legacy_database(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE daily (code TEXT,date TEXT)")
    connection.commit()
    connection.close()
    before = database.read_bytes()

    report = check_full_execution_readiness(
        database,
        source="tencent",
        adjustment_mode="raw",
        adjustment_version="tencent-qt-daily-v1",
        panel={("000001.SZ", "2026-07-27")},
    )

    assert report["ready"] is False
    assert database.read_bytes() == before


def test_execution_and_signal_adjustments_are_declared_separately(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE daily (code TEXT,date TEXT)")
    connection.commit()
    connection.close()

    report = check_full_execution_readiness(
        database,
        source="baostock",
        adjustment_mode="qfq",
        adjustment_version="baostock-qfq-v1",
        signal_adjustment_mode="raw",
        signal_adjustment_version="baostock-raw-v1",
        panel={("000001.SZ", "2026-07-27")},
    )

    assert report["request"]["execution_adjustment"] == {
        "mode": "qfq",
        "version": "baostock-qfq-v1",
    }
    assert report["request"]["signal_adjustment"] == {
        "mode": "raw",
        "version": "baostock-raw-v1",
    }
    assert {
        blocker["code"]
        for blocker in report["components"]["execution_prices"]["blockers"]
    } >= {"execution_prices_require_raw_adjustment"}
