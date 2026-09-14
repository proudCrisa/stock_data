"""QMT 通道前复权日线同步:高置信补充数据源,独立身份,非主链路。

通道:mac 端经 frp 隧道连 Windows QMT 导出策略(QmtExport/2.0),
基址默认 http://127.0.0.1:8000,凭据仅经 ``QMT_TOKEN`` 环境变量或
``~/.stockdata/qmt-token``(0600)注入。

铁律(与 openspec qmt-supplemental-source 一致):
  - 只读交互:仅 GET 端点 + ``/fulldata`` 按需通道;**无池回退路径**——
    ``/fulldata`` 失败即报错,绝不替换 Windows 常驻标的池;
  - 身份隔离:写入固定 ``qmt/qfq/qmt-front-v1``,绝不混入他源身份;
  - 校验对齐 wind 入库:有限正价、OHLC 关系、停牌证据、覆盖声明可验证;
  - 停牌/volume 为 0 的日行不入库,但计为停牌证据(空洞不视为缺口)。
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from .finalization import latest_finalized_date
from .ticker import normalize

_SHANGHAI = ZoneInfo("Asia/Shanghai")

SOURCE = "qmt"
ADJ_MODE = "qfq"
ADJ_VERSION = "qmt-front-v1"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 120
POLL_INTERVAL = 5.0
_MAX_COUNT = 10000

# 伪造成分防御:单标的单次返回的合理上限(30 年交易日)。
_MAX_ROWS_PER_SYMBOL = 10000
# 响应体硬上限:10000 行列式返回约 1 MB 量级,超限即拒绝(防隧道投毒撑爆内存)。
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 快照新鲜度上限:v11 策略全天 60s 一跳(收盘后照常),超过即判停跳。
_MAX_SNAPSHOT_AGE_SEC = 900


class QmtChannelError(ValueError):
    """通道不可达、凭据缺失或线协议不符。

    ``status`` 携带 HTTP 状态码(若错误源于 HTTP 响应);连接级错误为 None。
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_qmt_token(
    env: dict | None = None,
    token_file: str | Path | None = None,
) -> str:
    """按优先级读取 token:``QMT_TOKEN`` env → token 文件(必须 0600)。

    两者皆无或文件权限不符时抛 :class:`QmtChannelError`,不发起任何请求。
    """
    env = os.environ if env is None else env
    token = (env.get("QMT_TOKEN") or "").strip()
    if token:
        return token
    path = Path(token_file) if token_file else Path.home() / ".stockdata" / "qmt-token"
    if not path.is_file():
        raise QmtChannelError(
            "缺少 QMT 凭据:设置 QMT_TOKEN 或写入 ~/.stockdata/qmt-token (0600)")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise QmtChannelError(
            f"token 文件权限必须为 0600,当前 {oct(mode)}: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise QmtChannelError(f"token 文件为空: {path}")
    return token


# 传输函数签名:(path, method, body) -> 解析后的 JSON dict。
Transport = Callable[[str, str, dict | None], dict]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切 30x:token 头部绝不跟随重定向外泄。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_loopback(base_url: str) -> None:
    """只接受裸 loopback HTTP URL(含端口,无用户信息/路径/查询)。"""
    parsed = urllib.parse.urlparse(base_url)
    loopback = parsed.hostname in _LOOPBACK_HOSTS
    if parsed.scheme != "http" or not loopback or parsed.port is None \
            or parsed.username or parsed.password \
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise QmtChannelError(
            f"QMT 基址必须是裸 loopback http URL,当前: {base_url!r}")


def _urllib_transport(base_url: str, token: str, timeout: float = 60.0) -> Transport:
    _validate_loopback(base_url)
    # ProxyHandler({}) 切断环境变量代理继承:loopback 请求绝不绕行代理,
    # X-Token 不会经代理外泄(与 qmt_pool_replay 同源加固)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())

    def transport(path: str, method: str, body: dict | None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("X-Token", token)
        try:
            with opener.open(req, timeout=timeout) as resp:
                blob = resp.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise QmtChannelError(f"HTTP {exc.code}: {path}",
                                  status=exc.code) from exc
        except (OSError, ValueError) as exc:
            raise QmtChannelError(f"连接失败 {path}: {exc}") from exc
        if len(blob) > _MAX_RESPONSE_BYTES:
            raise QmtChannelError(
                f"响应体超过 {_MAX_RESPONSE_BYTES} 字节上限: {path}")
        try:
            return json.loads(blob.decode())
        except (UnicodeDecodeError, ValueError) as exc:
            raise QmtChannelError(f"响应不是合法 JSON: {path}: {exc}") from exc

    return transport


def _iso_day(value: object) -> str:
    """把通道日期值规范为 ISO 日;接受 YYYY-MM-DD / YYYYMMDD / epoch 毫秒。"""
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError(f"非法日期: {value!r}") from exc
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # epoch 毫秒(QMT 常用)——值表示上海市场日,必须先落 Asia/Shanghai
        # 再取日期(UTC 直转会偏移一天);越界值(溢出/无穷)归为非法日期
        try:
            return datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).astimezone(_SHANGHAI).date().isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"非法 epoch 毫秒: {value!r}") from exc
    raise ValueError(f"无法识别的日期值: {value!r}")


def parse_history_records(
    payload: dict,
    symbol: str,
) -> tuple[list[dict], set[str], list[str]]:
    """解析并校验 ``/fulldata history_kline`` 返回。

    返回 ``(bars, suspended, invalid)``:bars 为合法日线(日期升序),
    suspended 为停牌证据日集合,invalid 为逐条非法行描述。
    线协议不符(缺 index/columns、长度不齐)抛 :class:`QmtChannelError`。
    """
    if not isinstance(payload, dict):
        raise QmtChannelError("fulldata 返回不是 JSON 对象")
    data = payload.get("data")
    rec = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(rec, dict):
        raise QmtChannelError(f"{symbol} 本地无数据(需在 QMT 客户端订阅/下载)")
    index = rec.get("index")
    columns = rec.get("columns")
    if not isinstance(index, list) or not isinstance(columns, dict):
        raise QmtChannelError(f"{symbol} 返回缺 index/columns,线协议不符")
    try:
        fields = {k: columns[k] for k in ("open", "high", "low", "close", "volume")}
    except KeyError as exc:
        raise QmtChannelError(f"{symbol} columns 缺字段: {exc}") from exc
    flags = columns.get("suspendFlag")
    for name, values in fields.items():
        if not isinstance(values, list) or len(values) != len(index):
            raise QmtChannelError(
                f"{symbol} 字段 {name} 与 index({len(index)}) 长度不齐")
    if flags is not None and (not isinstance(flags, list)
                              or len(flags) != len(index)):
        raise QmtChannelError(f"{symbol} suspendFlag 与 index 长度不齐")
    if len(index) > _MAX_ROWS_PER_SYMBOL:
        raise QmtChannelError(f"{symbol} 返回 {len(index)} 行,超出合理上限")

    bars: list[dict] = []
    suspended: set[str] = set()
    invalid: list[str] = []
    seen: set[str] = set()
    for pos, day_value in enumerate(index):
        try:
            day = _iso_day(day_value)
        except ValueError:
            invalid.append(f"{symbol}: 非法日期 {day_value!r}")
            continue
        if day in seen:
            invalid.append(f"{symbol} {day}: 重复交易日")
            continue
        seen.add(day)
        volume = fields["volume"][pos]
        if (flags and flags[pos]) or volume in (None, ""):
            suspended.add(day)
            continue
        try:
            o, h, l, c, v = (float(fields[k][pos])
                             for k in ("open", "high", "low", "close", "volume"))
        except (TypeError, ValueError):
            invalid.append(f"{symbol} {day}: 字段非数值")
            continue
        if not all(math.isfinite(x) for x in (o, h, l, c, v)):
            invalid.append(f"{symbol} {day}: 非有限值")
            continue
        if min(o, h, l, c) <= 0 or v < 0:
            invalid.append(f"{symbol} {day}: 非正价或负量")
            continue
        if v == 0:
            # 零成交日 = 无交易发生,按停牌证据处理,不入库
            suspended.add(day)
            continue
        if not (l <= o <= h and l <= c <= h):
            invalid.append(f"{symbol} {day}: OHLC 关系被破坏")
            continue
        bars.append({"date": day, "open": o, "high": h, "low": l,
                     "close": c, "volume": v})
    bars.sort(key=lambda b: b["date"])
    return bars, suspended, invalid


class QmtChannelClient:
    """QMT 导出通道的只读客户端(GET + /fulldata,无池回退)。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        transport: Transport | None = None,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url
        self._transport = transport or _urllib_transport(base_url, token or "")
        self._poll_interval = poll_interval
        self._sleep = sleep

    def status(self) -> dict:
        return self._transport("/", "GET", None)

    def is_alive(self) -> bool:
        try:
            return bool(self.status().get("latest_exists"))
        except QmtChannelError:
            return False

    def assert_ready(self) -> dict:
        """批量前置健康门:服务标识 + 快照存在且新鲜(v11 全天 60s 一跳)。

        任一不合格立即抛 :class:`QmtChannelError`——防止「HTTP 导出进程活着
        但 Windows 策略已停」时整批标的逐个轮询干等。
        """
        status = self.status()
        if not isinstance(status, dict) or not status.get("server"):
            raise QmtChannelError("状态返回缺 server 标识,线协议不符")
        if not status.get("latest_exists"):
            raise QmtChannelError("尚无 latest.json 快照(策略从未导出)")
        age = status.get("latest_age_sec")
        if not isinstance(age, (int, float)) or isinstance(age, bool):
            raise QmtChannelError("状态返回缺 latest_age_sec,无法判定新鲜度")
        if age > _MAX_SNAPSHOT_AGE_SEC:
            raise QmtChannelError(
                f"快照已 {age}s 未更新(>{_MAX_SNAPSHOT_AGE_SEC}s),"
                "Windows 端策略疑似停跳")
        return status

    def history_front(
        self,
        symbol: str,
        start: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        """经 ``/fulldata`` 按需通道取前复权日线原始返回。

        失败(提交被拒/服务端错误/超时)抛 :class:`QmtChannelError`;
        没有、也绝不发起任何会更改常驻标的池的回退请求。
        """
        params = {
            "symbol": symbol,
            "period": "1d",
            "start": (start or "").replace("-", ""),
            "end": "",
            "count": _MAX_COUNT,
            "dividend_type": "front",
        }
        submitted = self._transport(
            "/fulldata", "POST", {"type": "history_kline", "params": params})
        req_id = submitted.get("id") if isinstance(submitted, dict) else None
        if not req_id:
            raise QmtChannelError(f"{symbol} fulldata 提交未返回请求ID")
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self._transport(f"/fulldata/{req_id}", "GET", None)
            except QmtChannelError as exc:
                # 仅登记期的瞬时 404 按 pending 处理;401/500/连接断等
                # 立即失败,避免每个标的干等满超时、拖住整批
                if exc.status != 404:
                    raise
                result = None
            if isinstance(result, dict) and "id" in result:
                status = result.get("status")
                # 服务端物化期间会瞬时返回 ok 但 data 为空:按 pending 继续等
                if status == "ok" and result.get("data") is not None:
                    return result
                if status is not None and status != "ok":
                    raise QmtChannelError(
                        f"{symbol} 查询失败: {result.get('error', status)}")
            if time.monotonic() >= deadline:
                raise QmtChannelError(f"{symbol} fulldata 超时({timeout}s)")
            self._sleep(self._poll_interval)


def sync_qmt_daily(
    cache,
    client: QmtChannelClient,
    codes: Sequence[str],
    start: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """把 QMT 前复权日线以独立身份增量同步进 ``daily`` 表。

    前置检查(通道不可达)抛 :class:`QmtChannelError`;单标的失败不抛出,
    记入返回结果的 ``errors``。返回::

        {"rows": int, "codes_ok": [...], "errors": {code: msg},
         "invalid": [...], "coverage_holes": {code: [...]},
         "synced_at": iso}
    """
    date.fromisoformat(start)  # 提前拒绝非法窗口
    client.assert_ready()  # 通道不可达/快照不新鲜:快速失败,不进标的循环

    # 未收盘的当日 bar 是演化中的值,绝不可以 is_final=True 入库;
    # 窗口上界钉在最新已定稿交易日,盘中运行只落到前一交易日。
    trade_calendar = cache.trading_calendar
    cutoff = (latest_finalized_date(calendar=trade_calendar)
              if trade_calendar.has_data() else latest_finalized_date())

    # 交易日历只取他源行:本身份的行不能自证覆盖完整。
    calendar = {r[0] for r in cache._conn.execute(
        "SELECT DISTINCT date FROM daily WHERE source != ?", (SOURCE,))}

    result: dict = {"rows": 0, "codes_ok": [], "errors": {}, "invalid": [],
                    "coverage_holes": {}, "synced_at": _utc_now()}
    for raw_code in codes:
        code = normalize(raw_code)
        try:
            payload = client.history_front(code, start=start, timeout=timeout)
            bars, suspended, invalid = parse_history_records(payload, code)
        except QmtChannelError as exc:
            result["errors"][code] = str(exc)
            continue
        # 通道可能忽略 start 返回更早历史:本地按窗口过滤,只 upsert 窗口内行
        bars = [b for b in bars if start <= b["date"] <= cutoff]
        suspended = {d for d in suspended if start <= d <= cutoff}
        result["invalid"].extend(invalid)
        if not bars:
            result["errors"][code] = "窗口内无合法日线"
            continue
        result["rows"] += cache.upsert(
            code, bars, source=SOURCE, adjustment_mode=ADJ_MODE,
            adjustment_version=ADJ_VERSION)
        lo, hi = bars[0]["date"], bars[-1]["date"]
        if not calendar:
            result["coverage_holes"][code] = ["trading calendar empty"]
            continue
        have = {b["date"] for b in bars} | suspended
        unexplained = [d for d in calendar if lo <= d <= hi and d not in have]
        if unexplained:
            result["coverage_holes"][code] = unexplained[:5]
        else:
            cache.record_sync_coverage(code, SOURCE, ADJ_MODE, ADJ_VERSION, lo, hi)
        result["codes_ok"].append(code)
    return result
