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
import sqlite3
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
# /latest 全池快照(309 标的 × 1300 行 × 3 种复权)实测约 59 MB,留 4 倍余量。
_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024


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


# 传输函数签名:(path, method, body, max_bytes) -> 解析后的 JSON dict。
# max_bytes 缺省 _MAX_RESPONSE_BYTES;/latest 全池快照用更大上限。
Transport = Callable[[str, str, "dict | None", int | None], dict]


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

    def transport(path: str, method: str, body: dict | None,
                  max_bytes: int | None = None) -> dict:
        limit = _MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("X-Token", token)
        try:
            with opener.open(req, timeout=timeout) as resp:
                blob = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise QmtChannelError(f"HTTP {exc.code}: {path}",
                                  status=exc.code) from exc
        except (OSError, ValueError) as exc:
            raise QmtChannelError(f"连接失败 {path}: {exc}") from exc
        if len(blob) > limit:
            raise QmtChannelError(f"响应体超过 {limit} 字节上限: {path}")
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
    cutoff: str | None = None,
) -> tuple[list[dict], set[str], list[str], set[str]]:
    """解析并校验 ``history_kline`` 返回(列式线形),全量不截底。

    返回 ``(bars, suspended, invalid, nonpositive)``:
    - bars:合法日线(日期升序)——**全量**,供单因子版本整体刷新;
    - suspended:停牌/零成交证据日集合;
    - invalid:逐条非法行描述(非有限值、负量、OHLC 关系破坏等);
    - nonpositive:前复权深历史因累计除权转为非正的行——复权口径产物,
      不算数据错误,但也不入库(Cache 的 bar 级不变量要求正价);
      作为「已观测」证据参与覆盖声明,并触发同日日行删除(清掉旧因子
      版本的残留正价行)。
    给定 ``cutoff``(最新已定稿交易日)时,晚于它的行整行跳过——
    盘中演化中的当日 bar 绝不入库。线协议不符抛 :class:`QmtChannelError`。
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
    nonpositive: set[str] = set()
    seen: set[str] = set()
    for pos, day_value in enumerate(index):
        try:
            day = _iso_day(day_value)
        except ValueError:
            invalid.append(f"{symbol}: 非法日期 {day_value!r}")
            continue
        if cutoff is not None and day > cutoff:
            continue  # 未定稿的当日 bar:整行跳过
        if day in seen:
            invalid.append(f"{symbol} {day}: 重复交易日")
            continue
        seen.add(day)
        if flags is not None and flags[pos] not in (0, 1):
            # 协议约定 suspendFlag 恰为 0/1;畸形值不得作为停牌证据
            invalid.append(f"{symbol} {day}: suspendFlag 非法值 {flags[pos]!r}")
            continue
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
        if v < 0:
            invalid.append(f"{symbol} {day}: 负量")
            continue
        if v == 0:
            # 零成交日 = 无交易发生,按停牌证据处理,不入库
            suspended.add(day)
            continue
        if not (l <= o <= h and l <= c <= h):
            invalid.append(f"{symbol} {day}: OHLC 关系被破坏")
            continue
        if min(o, h, l, c) <= 0:
            # 前复权深历史累计除权产物:观测但不入库(Cache 正价不变量)
            nonpositive.add(day)
            continue
        bars.append({"date": day, "open": o, "high": h, "low": l,
                     "close": c, "volume": v})
    bars.sort(key=lambda b: b["date"])
    return bars, suspended, invalid, nonpositive


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

    def latest_snapshot(self) -> dict:
        """原始 ``/latest`` 快照(大响应上限)。调用方应先过 assert_ready()。"""
        snap = self._transport("/latest", "GET", None, _MAX_SNAPSHOT_BYTES)
        if not isinstance(snap, dict):
            raise QmtChannelError("快照不是 JSON 对象")
        return snap

    def snapshot_front(self) -> dict[str, dict]:
        """一次 GET 取全池前复权 K 线:{symbol: {index, columns}}。

        主 dividend_type 即 front 时读 ``market``,否则读 ``market_adj.front``;
        两者皆无抛 :class:`QmtChannelError`。这是日更主路径:
        一次请求覆盖整个常驻池,不逐标的走 /fulldata。
        """
        snap = self.latest_snapshot()
        if snap.get("dividend_type") == "front":
            src = snap.get("market")
        else:
            src = (snap.get("market_adj") or {}).get("front")
        if not isinstance(src, dict) or not src:
            raise QmtChannelError("快照不含 front 复权数据(需在 Windows 端配置导出)")
        # 保留全部键:畸形记录(空 index 等)交由解析层分类为协议/数据错误,
        # 不能在这里静默丢弃而被下游误判为 not_in_pool
        return dict(src)

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


def _prepare_sync(cache, client: QmtChannelClient):
    """共享前置:健康门、定稿截断、他源日历。"""
    client.assert_ready()  # 通道不可达/快照不新鲜:快速失败,不进标的循环

    # 未收盘的当日 bar 是演化中的值,绝不可以 is_final=True 入库;
    # 区间上界钉在最新已定稿交易日,盘中运行只落到前一交易日。
    trade_calendar = cache.trading_calendar
    cutoff = (latest_finalized_date(calendar=trade_calendar)
              if trade_calendar.has_data() else latest_finalized_date())

    # 交易日历只取他源行:本身份的行不能自证覆盖完整。
    calendar = {r[0] for r in cache._conn.execute(
        "SELECT DISTINCT date FROM daily WHERE source != ?", (SOURCE,))}
    result: dict = {"rows": 0, "codes_ok": [], "errors": {}, "invalid": [],
                    "nonpositive": {}, "coverage_holes": {},
                    "synced_at": _utc_now()}
    return cutoff, calendar, result


def _absorb(cache, code: str, bars: list[dict], suspended: set[str],
            invalid: list[str], nonpositive: set[str], cutoff: str,
            calendar: set[str], result: dict) -> None:
    """原子化全量刷新单标的:先完整验证,通过才写库;写则替换式覆盖声明。

    fail-closed 顺序(任一步不过,该标的整批不写,库内保持旧因子版本的一致序列):
    1. 本次返回含非法行 → 整标的新版本不可信,拒收;
    2. 库内他源日历为空 → 无法验证完整性,拒收;
    3. [lo,hi] 内存在无证据解释的日历交易日 → 版本不完整,拒收。

    通过后才写库(单事务原子):
    - upsert 全部正价行(同一复权因子版本);
    - 删除 ``date < 首个正价行``(通道够不到的更早日行);
    - 删除停牌/非正价证据日的残留行(旧因子版本的正价行);
    - 覆盖声明**替换**为本次实际验证的 [lo, hi](lo/hi 取所有已观测日,
      含尾部的停牌/零成交证据),不做 MIN/MAX 合并——刷新是破坏性的,
      合并会让覆盖声明超出库内实际数据。
    另:通道返回右界回退于库内已存右界(数据倒退)时整标的拒收——
    否则库内会残留覆盖声明之外的旧因子行。
    """
    result["invalid"].extend(invalid)
    if nonpositive:
        result["nonpositive"][code] = len(nonpositive)
    if invalid:
        result["errors"][code] = f"返回含 {len(invalid)} 条非法行,整标的拒收"
        return
    if not bars:
        result["errors"][code] = "无合法日线"
        return
    observed = {b["date"] for b in bars} | suspended | nonpositive
    lo, hi = min(observed), max(observed)
    if not calendar:
        result["coverage_holes"][code] = ["trading calendar empty"]
        return
    unexplained = [d for d in calendar if lo <= d <= hi and d not in observed]
    if unexplained:
        result["coverage_holes"][code] = unexplained[:5]
        return
    stored_hi = cache._conn.execute(
        "SELECT MAX(date) FROM daily WHERE code=? AND source=?"
        " AND adjustment_mode=? AND adjustment_version=?",
        (code, SOURCE, ADJ_MODE, ADJ_VERSION)).fetchone()[0]
    if stored_hi and stored_hi > hi:
        result["errors"][code] = (
            f"通道返回右界 {hi} 回退于库内已存 {stored_hi},整标的拒收")
        return

    try:
        result["rows"] += cache.replace_identity_range(
            code, bars, SOURCE, ADJ_MODE, ADJ_VERSION,
            delete_before=bars[0]["date"],
            delete_dates=sorted(suspended | nonpositive),
            coverage_start=lo, coverage_end=hi)
    except (sqlite3.Error, OSError) as exc:
        # 单事务回滚,库内保持旧的一致状态;按标的隔离失败
        result["errors"][code] = f"写入失败(已回滚): {exc}"
        return
    result["codes_ok"].append(code)


def sync_qmt_daily(
    cache,
    client: QmtChannelClient,
    codes: Sequence[str],
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """逐标的 ``/fulldata`` 路径(回填/池外标的用;日更请走快照路径)。

    前置检查不合格抛 :class:`QmtChannelError`;单标的失败不抛出,
    记入返回结果的 ``errors``。返回::

        {"rows": int, "codes_ok": [...], "errors": {code: msg},
         "invalid": [...], "nonpositive": {code: n},
         "coverage_holes": {code: [...]}, "synced_at": iso}
    """
    cutoff, calendar, result = _prepare_sync(cache, client)
    for raw_code in codes:
        code = normalize(raw_code)
        try:
            payload = client.history_front(code, timeout=timeout)
            bars, suspended, invalid, nonpositive = parse_history_records(
                payload, code, cutoff=cutoff)
        except QmtChannelError as exc:
            result["errors"][code] = str(exc)
            continue
        _absorb(cache, code, bars, suspended, invalid, nonpositive,
                cutoff, calendar, result)
    return result


def sync_qmt_daily_from_snapshot(
    cache,
    client: QmtChannelClient,
    codes: Sequence[str],
) -> dict:
    """快照主路径:一次 ``/latest`` 取全池前复权 K 线,同步池内标的。

    面板中不在常驻池的标的记入 ``not_in_pool``(非错误——QMT 是补充源,
    池外标的仍由 baostock 主链路覆盖);需纳入时先在 Windows 端扩充标的池,
    或用 :func:`sync_qmt_daily` 对该标的走 fulldata 回填。
    """
    cutoff, calendar, result = _prepare_sync(cache, client)
    result["not_in_pool"] = []
    pool = client.snapshot_front()
    for raw_code in codes:
        code = normalize(raw_code)
        if code not in pool:
            result["not_in_pool"].append(code)
            continue
        rec = pool[code]
        if not isinstance(rec, dict) or not rec.get("index"):
            result["errors"][code] = "快照中该标的记录缺失/畸形"
            continue
        try:
            bars, suspended, invalid, nonpositive = parse_history_records(
                {"data": {code: rec}}, code, cutoff=cutoff)
        except QmtChannelError as exc:
            result["errors"][code] = str(exc)
            continue
        _absorb(cache, code, bars, suspended, invalid, nonpositive,
                cutoff, calendar, result)
    return result
