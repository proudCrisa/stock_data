"""Slice 1: ticker 归一化。

内部规范形态: 600519.SH（6位数字 + '.' + 大写交易所后缀）。
派生各数据源所需格式。
"""
import pytest

from stockdata.ticker import (
    normalize,
    to_mootdx,
    to_tencent,
    to_baostock,
    is_index_or_etf,
)


class TestNormalize:
    def test_already_normal(self):
        assert normalize("600519.SH") == "600519.SH"

    def test_shenzhen_normal(self):
        assert normalize("000001.SZ") == "000001.SZ"

    def test_lowercase_suffix(self):
        assert normalize("600519.sh") == "600519.SH"

    def test_prefix_form_sh(self):
        assert normalize("sh600519") == "600519.SH"

    def test_prefix_form_sz(self):
        assert normalize("sz000001") == "000001.SZ"

    def test_bare_code_6xx_is_shanghai(self):
        # 6 开头 → 上交所
        assert normalize("600519") == "600519.SH"

    def test_bare_code_000_is_shenzhen(self):
        # 000/002/300 开头 → 深交所
        assert normalize("000001") == "000001.SZ"

    def test_bare_code_300_is_shenzhen(self):
        assert normalize("300750") == "300750.SZ"

    def test_bare_code_51x_etf_is_shanghai(self):
        # 51x ETF → 上交所
        assert normalize("510050") == "510050.SH"

    def test_bare_code_159_etf_is_shenzhen(self):
        # 159x ETF → 深交所
        assert normalize("159941") == "159941.SZ"

    def test_whitespace_stripped(self):
        assert normalize("  600519.SH  ") == "600519.SH"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize("NOTACODE")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalize("")


class TestToMootdx:
    def test_strips_suffix(self):
        assert to_mootdx("600519.SH") == "600519"

    def test_accepts_bare(self):
        assert to_mootdx("000001") == "000001"


class TestToTencent:
    def test_shanghai_prefix(self):
        assert to_tencent("600519.SH") == "sh600519"

    def test_shenzhen_prefix(self):
        assert to_tencent("000001.SZ") == "sz000001"


class TestToBaostock:
    def test_shanghai_dotted(self):
        assert to_baostock("600519.SH") == "sh.600519"

    def test_shenzhen_dotted(self):
        assert to_baostock("000001.SZ") == "sz.000001"

    def test_accepts_bare(self):
        assert to_baostock("510050") == "sh.510050"


class TestIsIndexOrEtf:
    def test_index_000300(self):
        # 沪深300 指数
        assert is_index_or_etf("000300.SH") is True

    def test_etf_510050(self):
        assert is_index_or_etf("510050.SH") is True

    def test_etf_159941(self):
        assert is_index_or_etf("159941.SZ") is True

    def test_regular_stock_false(self):
        assert is_index_or_etf("600519.SH") is False

    def test_regular_stock_sz_false(self):
        assert is_index_or_etf("000001.SZ") is False
