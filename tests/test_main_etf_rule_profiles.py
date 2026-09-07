from copy import deepcopy

import pytest

from stockdata.market_rules import ETF_MARKET_RULE_PAYLOAD_SCHEMA, ETF_RULE_SCOPES, validate_market_rule_payload
from test_provider_market_rules import _rule


@pytest.mark.parametrize("symbol,t_plus_one,limit,exchange", [
    ("561980.SH", True, .10, "SH"), ("588730.SH", True, .20, "SH"),
    ("560900.SH", True, .10, "SH"), ("159350.SZ", True, .10, "SZ"),
    ("518880.SH", False, .10, "SH"), ("511010.SH", False, .10, "SH"),
    ("513650.SH", False, .10, "SH"), ("159980.SZ", False, .10, "SZ"),
])
def test_exact_main_etf_profile(symbol, t_plus_one, limit, exchange):
    rule = _rule(schema_version=ETF_MARKET_RULE_PAYLOAD_SCHEMA, security_type="ETF", board="ETF",
                 instrument_id=symbol, **ETF_RULE_SCOPES[symbol])
    rule["effective_until"] = "2026-09-04"
    assert rule["t_plus_one"] is t_plus_one
    assert rule["price_limit_up"] == limit
    assert rule["exchange"] == exchange
    assert validate_market_rule_payload(rule, panel_entry=f"{symbol}@2026-09-04") == rule
    for field, wrong in [("t_plus_one", not t_plus_one), ("price_limit_up", .10 if limit == .20 else .20),
                         ("exchange", "SZ" if exchange == "SH" else "SH"), ("lot_size", 10),
                         ("price_tick", .01), ("classification_source", "https://example.com/guess")]:
        changed = deepcopy(rule)
        changed[field] = wrong
        with pytest.raises(ValueError):
            validate_market_rule_payload(changed, panel_entry=f"{symbol}@2026-09-04")


def test_unknown_etf_cannot_borrow_known_profile():
    rule = _rule(schema_version=ETF_MARKET_RULE_PAYLOAD_SCHEMA, security_type="ETF", board="ETF",
                 instrument_id="512800.SH", **ETF_RULE_SCOPES["561980.SH"])
    with pytest.raises(ValueError, match="primary sources"):
        validate_market_rule_payload(rule)


def test_159992_price_qualification_does_not_grant_market_rule_scope():
    from stockdata import local_daily_snapshot
    assert local_daily_snapshot.PROFILE == "trading-current-local-prices/2"
    assert "159992.SZ" in local_daily_snapshot.ETF_SYMBOLS
    assert "159992.SZ" not in ETF_RULE_SCOPES
    rule = _rule(schema_version=ETF_MARKET_RULE_PAYLOAD_SCHEMA, security_type="ETF", board="ETF",
                 instrument_id="159992.SZ", **ETF_RULE_SCOPES["159350.SZ"])
    with pytest.raises(ValueError, match="primary sources"):
        validate_market_rule_payload(rule, panel_entry="159992.SZ@2026-09-07")
