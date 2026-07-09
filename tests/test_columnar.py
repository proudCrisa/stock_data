"""Slice 7: 列式返回组装（drop-in 契约）。

findesk 兼容格式：顶层 {data: {time,code,open,high,low,close,volume}}，
data 内各字段为等长并行数组；多标的按 (code,time) 平铺进同一组数组。
补齐 open（findesk 原缺）；去掉 findesk 的双重 JSON wrapper。
"""
from stockdata.columnar import to_columnar


ROWS_A = [
    {"date": "2024-01-02", "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 100},
    {"date": "2024-01-03", "open": 1.1, "high": 1.3, "low": 1.0, "close": 1.2, "volume": 200},
]
ROWS_B = [
    {"date": "2024-01-02", "open": 5.0, "high": 5.2, "low": 4.9, "close": 5.1, "volume": 900},
]


class TestSingleCode:
    def test_top_level_data_key(self):
        out = to_columnar({"600519.SH": ROWS_A})
        assert "data" in out
        assert set(out["data"]) == {"time", "code", "open", "high", "low", "close", "volume"}

    def test_parallel_arrays_equal_length(self):
        d = to_columnar({"600519.SH": ROWS_A})["data"]
        n = len(d["time"])
        assert all(len(d[k]) == n for k in d)
        assert n == 2

    def test_values_aligned(self):
        d = to_columnar({"600519.SH": ROWS_A})["data"]
        assert d["time"][0] == "2024-01-02"
        assert d["code"][0] == "600519.SH"
        assert d["close"][0] == 1.1
        assert d["open"][0] == 1.0     # open 补齐存在

    def test_no_double_wrapper(self):
        # 返回即为可直接消费的 dict，data 内是数组而非再次编码的字符串
        out = to_columnar({"600519.SH": ROWS_A})
        assert isinstance(out["data"]["close"], list)
        assert not isinstance(out["data"], str)


class TestMultiCode:
    def test_flattened_long_table(self):
        d = to_columnar({"600519.SH": ROWS_A, "000001.SZ": ROWS_B})["data"]
        assert len(d["time"]) == 3      # 2 + 1 平铺进同一组数组
        assert set(d["code"]) == {"600519.SH", "000001.SZ"}

    def test_regroupable_by_code(self):
        d = to_columnar({"600519.SH": ROWS_A, "000001.SZ": ROWS_B})["data"]
        # 用 code 分组可还原每只标的
        by_code = {}
        for i, c in enumerate(d["code"]):
            by_code.setdefault(c, []).append(d["close"][i])
        assert by_code["600519.SH"] == [1.1, 1.2]
        assert by_code["000001.SZ"] == [5.1]


class TestEmpty:
    def test_empty_input(self):
        d = to_columnar({})["data"]
        assert all(d[k] == [] for k in ("time", "code", "open", "high", "low", "close", "volume"))

    def test_code_with_no_rows(self):
        d = to_columnar({"600519.SH": []})["data"]
        assert d["time"] == []
