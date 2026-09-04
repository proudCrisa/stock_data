from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json

import pytest

from stockdata.liquidity_amount_product import (
    LiquidityAmountProductError,
    build_liquidity_amounts_product,
    load_liquidity_amounts_product,
    verify_liquidity_amounts_product,
    write_liquidity_amounts_product,
)


TARGET_ETFS = ("159980.SZ", "561980.SH")
DECISION_CUTOFF = "2026-08-31T09:25:00+08:00"


def _sessions() -> list[str]:
    current = date(2026, 8, 3)
    values = []
    while len(values) < 20:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _receipt(code: str, sessions: list[str], *, include_amount: bool = True) -> dict:
    fields = "date,open,high,low,close,volume"
    if include_amount:
        fields += ",amount"
    rows = []
    for index, day in enumerate(sessions):
        row = [day, "1.00", "1.10", "0.90", "1.05", str(1000 + index)]
        if include_amount:
            row.append(str(8_000_000 + index * 100_000))
        rows.append(row)
    exchange = code[-2:].lower()
    return {
        "observed_at": "2026-08-28T16:05:00+08:00",
        "source": "baostock",
        "request": {
            "method": "query_history_k_data_plus",
            "code": f"{exchange}.{code[:6]}",
            "fields": fields,
            "start_date": sessions[0],
            "end_date": sessions[-1],
            "frequency": "d",
            "adjustflag": "3",
        },
        "response": {"fields": fields, "rows": rows},
    }


def _captures(*, missing: tuple[str, str] | None = None,
              include_amount: bool = True):
    sessions = _sessions()
    captures = []
    for code in TARGET_ETFS:
        receipt = _receipt(code, sessions, include_amount=include_amount)
        receipt["response"]["rows"] = [
            row for row in receipt["response"]["rows"]
            if missing != (code, row[0])
        ]
        captures.append(receipt)
    return captures


def _panel() -> list[tuple[str, str]]:
    return [(code, day) for code in TARGET_ETFS for day in _sessions()]


def _build(captures, *, watermark: str | None = None):
    return build_liquidity_amounts_product(
        captures,
        panel=_panel(),
        decision_cutoff=DECISION_CUTOFF,
        expected_watermark=watermark or _sessions()[-1],
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def test_builds_formal_cny_liquidity_authority_from_native_amount(tmp_path):
    product = _build(_captures())

    assert product["component"] == "liquidity_amounts"
    assert product["authority_grade"] == "formal"
    assert product["decision_eligible"] is True
    assert product["decision_authority"] is True
    assert product["amount_unit"] == "CNY"
    assert product["liquidity_identity"] == {
        "source": "baostock",
        "field": "amount",
        "amount_unit": "CNY",
        "adjustment_mode": "raw",
        "adjustment_version": "baostock-adjustflag-3",
    }
    assert product["instrument_scope"] == {
        "instrument_type": "ETF",
        "codes": list(TARGET_ETFS),
        "minimum_observations_per_code": 20,
    }
    assert product["finality"] == {
        "status": "decision_watermark_bound",
        "watermark": _sessions()[-1],
    }
    assert len(product["records"]) == 40
    assert product["records"][0]["payload"] == {
        "amount": 8_000_000.0,
        "amount_unit": "CNY",
    }
    assert product["records"][0]["payload"]["amount"] != 1.05 * 1000
    assert len(product["source_receipts"]) == 2
    assert verify_liquidity_amounts_product(
        product,
        expected_panel=_panel(),
        decision_cutoff=DECISION_CUTOFF,
        expected_watermark=_sessions()[-1],
    ) == product


def test_content_addressed_write_and_canonical_load_are_idempotent(tmp_path):
    product = _build(_captures())

    first = write_liquidity_amounts_product(tmp_path / "products", product)
    second = write_liquidity_amounts_product(tmp_path / "products", product)

    assert first == second
    assert first.name == f"{product['product_sha256']}.json"
    assert load_liquidity_amounts_product(first) == product


def test_rejects_missing_target_etf_panel_entry(tmp_path):
    missing = (TARGET_ETFS[1], _sessions()[7])
    captures = _captures(missing=missing)

    with pytest.raises(LiquidityAmountProductError, match="exact panel"):
        _build(captures)


def test_rejects_non_etf_target(tmp_path):
    captures = _captures()
    panel = [*_panel(), *(('600519.SH', day) for day in _sessions())]

    with pytest.raises(LiquidityAmountProductError, match="ETF"):
        build_liquidity_amounts_product(
            captures,
            panel=panel,
            decision_cutoff=DECISION_CUTOFF,
            expected_watermark=_sessions()[-1],
        )


def test_rejects_less_than_twenty_observations_per_etf(tmp_path):
    captures = _captures()
    panel = [(code, day) for code in TARGET_ETFS for day in _sessions()[1:]]

    with pytest.raises(LiquidityAmountProductError, match="20 observations"):
        build_liquidity_amounts_product(
            captures,
            panel=panel,
            decision_cutoff=DECISION_CUTOFF,
            expected_watermark=_sessions()[-1],
        )


def test_rejects_receipt_without_native_amount(tmp_path):
    captures = _captures(include_amount=False)

    with pytest.raises(LiquidityAmountProductError, match="native amount"):
        _build(captures)


def test_rejects_stale_watermark(tmp_path):
    captures = _captures()

    with pytest.raises(LiquidityAmountProductError, match="watermark"):
        _build(captures, watermark="2026-08-31")


@pytest.mark.parametrize("amount", ["", "NaN", "inf", "0", "-1", True])
def test_rejects_invalid_native_amount(amount):
    captures = _captures()
    captures[0]["response"]["rows"][0][-1] = amount
    with pytest.raises(LiquidityAmountProductError, match="native amount"):
        _build(captures)


def test_rejects_receipt_after_cutoff_and_before_final_close():
    captures = _captures()
    captures[0]["observed_at"] = "2026-08-31T09:26:00+08:00"
    with pytest.raises(LiquidityAmountProductError, match="decision cutoff"):
        _build(captures)
    captures[0]["observed_at"] = "2026-08-28T14:59:00+08:00"
    with pytest.raises(LiquidityAmountProductError, match="not final"):
        _build(captures)


def test_rejects_duplicate_native_rows():
    captures = _captures()
    captures[0]["response"]["rows"].append(captures[0]["response"]["rows"][0])
    with pytest.raises(LiquidityAmountProductError, match="duplicate"):
        _build(captures)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("hash", "product hash"),
        ("unit", "amount unit"),
        ("source", "liquidity identity"),
        ("amount", "native amount"),
    ],
)
def test_verify_rejects_tamper_even_when_outer_hash_is_resealed(
    tmp_path, mutation, error
):
    product = _build(_captures())
    tampered = deepcopy(product)
    if mutation == "hash":
        tampered["records"][0]["payload"]["amount"] += 1
    elif mutation == "unit":
        tampered["amount_unit"] = "RMB"
    elif mutation == "source":
        tampered["liquidity_identity"]["source"] = "tencent"
    else:
        tampered["records"][0]["payload"]["amount"] += 1
        payload = tampered["records"][0]["payload"]
        tampered["records"][0]["record_sha256"] = hashlib.sha256(
            _canonical(payload)
        ).hexdigest()
    if mutation != "hash":
        unsigned = {k: v for k, v in tampered.items() if k != "product_sha256"}
        tampered["product_sha256"] = hashlib.sha256(_canonical(unsigned)).hexdigest()

    with pytest.raises(LiquidityAmountProductError, match=error):
        verify_liquidity_amounts_product(tampered)
