"""ticker 归一化。

内部规范形态: ``600519.SH`` —— 6 位数字 + '.' + 大写交易所后缀（SH/SZ）。
派生 mootdx（纯码）、腾讯/百度（sh/sz 前缀）所需格式，并识别指数/ETF。
"""
from __future__ import annotations

import re

_PREFIX_RE = re.compile(r"^(sh|sz)(\d{6})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^(\d{6})\.(sh|sz)$", re.IGNORECASE)
_BARE_RE = re.compile(r"^\d{6}$")


def _market_for_bare(code: str) -> str:
    """裸 6 位码 → 交易所后缀。

    上交所(SH): 6(主板/科创), 5(ETF/基金), 9(B股)
    深交所(SZ): 0(主板/中小), 3(创业), 1(ETF/LOF/债)
    """
    head = code[0]
    if head in ("6", "5", "9"):
        return "SH"
    if head in ("0", "3", "1"):
        return "SZ"
    raise ValueError(f"无法判定交易所: {code}")


def normalize(code: str) -> str:
    """任意输入形态 → 规范形态 ``600519.SH``。"""
    if not code or not code.strip():
        raise ValueError("空代码")
    s = code.strip()

    m = _SUFFIX_RE.match(s)
    if m:
        return f"{m.group(1)}.{m.group(2).upper()}"

    m = _PREFIX_RE.match(s)
    if m:
        return f"{m.group(2)}.{m.group(1).upper()}"

    if _BARE_RE.match(s):
        return f"{s}.{_market_for_bare(s)}"

    raise ValueError(f"无法识别的代码格式: {code!r}")


def _split(code: str) -> tuple[str, str]:
    """规范化后拆成 (6位码, 交易所)。"""
    norm = normalize(code)
    digits, market = norm.split(".")
    return digits, market


def to_mootdx(code: str) -> str:
    """→ mootdx symbol（6 位纯码）。"""
    return _split(code)[0]


def to_tencent(code: str) -> str:
    """→ 腾讯/百度前缀形态 ``sh600519`` / ``sz000001``。"""
    digits, market = _split(code)
    return f"{market.lower()}{digits}"


def to_baostock(code: str) -> str:
    """→ baostock 形态 ``sh.600519`` / ``sz.000001``。"""
    digits, market = _split(code)
    return f"{market.lower()}.{digits}"


def is_index_or_etf(code: str) -> bool:
    """识别指数/ETF（决定拉取源分支）。

    - ETF/基金: 深 15x/16x、沪 51x/58x
    - 指数: 沪 000xxx（.SH）、深 399xxx（.SZ）
    """
    digits, market = _split(code)
    # ETF / LOF
    if digits[:2] in ("15", "16", "51", "58"):
        return True
    # 指数
    if market == "SH" and digits.startswith("000"):
        return True
    if market == "SZ" and digits.startswith("399"):
        return True
    return False
