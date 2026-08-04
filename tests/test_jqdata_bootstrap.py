from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stockdata.jqdata_bootstrap import (
    EVIDENCE_GRADE,
    JQDataBootstrapError,
    authenticate,
    build_bootstrap_artifact,
)


class FakeJQData:
    def __init__(self, *, spare: int = 100_000, status_day: str = "2026-07-22"):
        self.spare = spare
        self.status_day = status_day
        self.auth_calls: list[tuple[str, str]] = []
        self.universe_calls: list[str] = []
        self.status_calls: list[tuple[str, str]] = []

    def auth(self, account: str, secret: str) -> None:
        self.auth_calls.append((account, secret))

    def get_query_count(self) -> dict[str, int]:
        return {"spare": self.spare, "total": 500_000}

    def get_all_securities(self, *, types: list[str], date: str) -> pd.DataFrame:
        assert types == ["stock"]
        self.universe_calls.append(date)
        return pd.DataFrame(
            {
                "display_name": ["平安银行", "浦发银行"],
                "name": ["PAYH", "PFYH"],
                "start_date": [date_from("1991-04-03"), date_from("1999-11-10")],
                "end_date": [date_from("2200-01-01"), date_from("2200-01-01")],
                "type": ["stock", "stock"],
            },
            index=["000001.XSHE", "600000.XSHG"],
        )

    def get_price(self, security: str, **kwargs: object) -> pd.DataFrame:
        day = str(kwargs["start_date"])
        assert kwargs == {
            "start_date": day,
            "end_date": day,
            "frequency": "daily",
            "fields": ["paused"],
            "skip_paused": False,
            "fq": None,
        }
        assert isinstance(security, str)
        self.status_calls.append((security, day))
        paused = 0 if security == "000001.XSHE" else 1
        return pd.DataFrame(
            {"paused": [paused]},
            index=pd.DatetimeIndex([self.status_day], name="time"),
        )


def date_from(value: str) -> date:
    return date.fromisoformat(value)


def test_authentication_failure_is_redacted() -> None:
    class RejectingJQData:
        logged_out = False

        def auth(self, account: str, secret: str) -> None:
            raise RuntimeError(f"rejected {account} with {secret}")

        def logout(self) -> None:
            self.logged_out = True

    account = "private-account"
    secret = "private-secret"
    sdk = RejectingJQData()
    with pytest.raises(JQDataBootstrapError) as captured:
        authenticate(sdk, account, secret)

    assert str(captured.value) == "JQData authentication failed"
    assert account not in repr(captured.value)
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert sdk.logged_out is True
    traceback = captured.value.__traceback__
    while traceback and traceback.tb_frame.f_code.co_name != "authenticate":
        traceback = traceback.tb_next
    assert traceback is not None
    assert traceback.tb_frame.f_locals["account"] == ""
    assert traceback.tb_frame.f_locals["secret"] == ""


def test_quota_failure_happens_before_any_history_query() -> None:
    sdk = FakeJQData(spare=10)
    panel = {("000001.SZ", "2026-07-22"), ("600000.SH", "2026-07-22")}

    with pytest.raises(JQDataBootstrapError, match="free quota is insufficient"):
        build_bootstrap_artifact(
            sdk,
            panel=panel,
            observed_at="2026-08-04T15:00:00+08:00",
            max_rows=20_000,
        )

    assert sdk.universe_calls == []
    assert sdk.status_calls == []


def test_small_row_limit_fails_before_any_provider_query() -> None:
    sdk = FakeJQData()

    with pytest.raises(JQDataBootstrapError, match="row limit is too small"):
        build_bootstrap_artifact(
            sdk,
            panel={("000001.SZ", "2026-07-22")},
            observed_at="2026-08-04T15:00:00+08:00",
            max_rows=5_000,
        )

    assert sdk.universe_calls == []
    assert sdk.status_calls == []


def test_fund_symbol_is_rejected_before_any_provider_query() -> None:
    sdk = FakeJQData()

    with pytest.raises(JQDataBootstrapError, match="non-A-share"):
        build_bootstrap_artifact(
            sdk,
            panel={("510300.SH", "2026-07-22")},
            observed_at="2026-08-04T15:00:00+08:00",
            max_rows=20_000,
        )

    assert sdk.universe_calls == []
    assert sdk.status_calls == []


def test_non_stock_universe_row_is_rejected() -> None:
    class InvalidUniverseJQData(FakeJQData):
        def get_all_securities(self, *, types: list[str], date: str) -> pd.DataFrame:
            frame = super().get_all_securities(types=types, date=date)
            frame.loc["000001.XSHE", "type"] = "fund"
            return frame

    with pytest.raises(JQDataBootstrapError, match="non-stock universe row"):
        build_bootstrap_artifact(
            InvalidUniverseJQData(),
            panel={("000001.SZ", "2026-07-22")},
            observed_at="2026-08-04T15:00:00+08:00",
            max_rows=20_000,
        )


def test_bootstrap_is_exact_deterministic_and_non_authoritative() -> None:
    sdk = FakeJQData()
    panel = {("600000.SH", "2026-07-22"), ("000001.SZ", "2026-07-22")}

    artifact = build_bootstrap_artifact(
        sdk,
        panel=panel,
        observed_at="2026-08-04T15:00:00+08:00",
        max_rows=20_000,
    )

    assert artifact["provider"] == "jqdata"
    assert artifact["evidence_grade"] == EVIDENCE_GRADE
    assert artifact["authoritative"] is False
    assert artifact["panel"] == ["000001.SZ@2026-07-22", "600000.SH@2026-07-22"]
    assert [row["symbol"] for row in artifact["universe"]] == [
        "000001.SZ",
        "600000.SH",
    ]
    assert artifact["instrument_status"] == [
        {
            "effective_date": "2026-07-22",
            "is_tradable": True,
            "status": "active",
            "symbol": "000001.SZ",
        },
        {
            "effective_date": "2026-07-22",
            "is_tradable": False,
            "status": "suspended",
            "symbol": "600000.SH",
        },
    ]
    encoded = repr(artifact)
    assert "account" not in encoded
    assert "secret" not in encoded


def test_neighboring_status_date_is_rejected() -> None:
    sdk = FakeJQData(status_day="2026-07-21")

    with pytest.raises(JQDataBootstrapError, match="non-exact status date"):
        build_bootstrap_artifact(
            sdk,
            panel={("000001.SZ", "2026-07-22")},
            observed_at="2026-08-04T15:00:00+08:00",
            max_rows=20_000,
        )
