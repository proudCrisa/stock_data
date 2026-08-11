"""Bounded, non-authoritative JQData bootstrap collection."""
from __future__ import annotations

import contextlib
import io
from collections.abc import Iterable
from datetime import date, datetime

EVIDENCE_GRADE = "VENDOR_BOOTSTRAP_ONLY"
MAX_RUN_ROWS = 100_000
UNIVERSE_ROW_BUDGET_PER_DATE = 10_000


class JQDataBootstrapError(RuntimeError):
    """A redacted, fail-closed JQData bootstrap failure."""


def close_session(sdk: object) -> None:
    """Best-effort removal of credentials retained by the SDK session."""
    logout = getattr(sdk, "logout", None)
    if callable(logout):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                logout()
        except Exception:  # noqa: BLE001,S110
            pass


def authenticate(sdk: object, account: str, secret: str) -> None:
    """Authenticate without allowing SDK output or exceptions to expose credentials."""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            sdk.auth(account, secret)
    except Exception:  # noqa: BLE001
        close_session(sdk)
        account = secret = ""
        raise JQDataBootstrapError("JQData authentication failed") from None


def _local_symbol(provider_symbol: object) -> str:
    value = str(provider_symbol)
    if value.endswith(".XSHE"):
        return value[:-5] + ".SZ"
    if value.endswith(".XSHG"):
        return value[:-5] + ".SH"
    raise JQDataBootstrapError("JQData returned an unsupported stock symbol")


def _provider_symbol(local_symbol: str) -> str:
    value = local_symbol.strip().upper()
    if value.endswith(".SZ") and value[:-3].startswith(("0", "3")):
        return value[:-3] + ".XSHE"
    if value.endswith(".SH") and value[:-3].startswith("6"):
        return value[:-3] + ".XSHG"
    raise JQDataBootstrapError("panel contains a non-A-share stock symbol")


def _is_supported_provider_stock(provider_symbol: object) -> bool:
    value = str(provider_symbol)
    return (
        value.endswith(".XSHE") and value[:-5].startswith(("0", "3"))
    ) or (value.endswith(".XSHG") and value[:-5].startswith("6"))


def _iso_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _validate_inputs(
    panel: Iterable[tuple[str, str]], observed_at: str, max_rows: int
) -> set[tuple[str, str]]:
    if isinstance(max_rows, bool) or not 0 < max_rows <= MAX_RUN_ROWS:
        raise JQDataBootstrapError("JQData row limit is invalid")
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError
        normalized = {
            (_local_symbol(_provider_symbol(symbol)), date.fromisoformat(day).isoformat())
            for symbol, day in panel
        }
    except (TypeError, ValueError):
        raise JQDataBootstrapError("JQData bootstrap input is invalid") from None
    if not normalized:
        raise JQDataBootstrapError("JQData bootstrap panel is empty")
    planned_rows = (
        len({day for _, day in normalized}) * UNIVERSE_ROW_BUDGET_PER_DATE
        + len(normalized)
    )
    if planned_rows > max_rows:
        raise JQDataBootstrapError("JQData row limit is too small for the panel")
    return normalized


def _check_quota(sdk: object, max_rows: int) -> None:
    try:
        quota = sdk.get_query_count()
        spare = int(quota["spare"])
    except Exception:  # noqa: BLE001
        raise JQDataBootstrapError("JQData quota check failed") from None
    if spare < max_rows:
        raise JQDataBootstrapError("JQData free quota is insufficient")


def _universe_rows(sdk: object, day: str) -> list[dict[str, object]]:
    try:
        frame = sdk.get_all_securities(types=["stock"], date=day)
        rows = []
        for provider_symbol, source in frame.iterrows():
            if str(source["type"]) != "stock":
                raise JQDataBootstrapError("JQData returned a non-stock universe row")
            if not _is_supported_provider_stock(provider_symbol):
                continue
            rows.append(
                {
                    "display_name": str(source["display_name"]),
                    "effective_date": day,
                    "end_date": _iso_date(source["end_date"]),
                    "name": str(source["name"]),
                    "start_date": _iso_date(source["start_date"]),
                    "symbol": _local_symbol(provider_symbol),
                }
            )
        return rows
    except JQDataBootstrapError:
        raise
    except Exception:  # noqa: BLE001
        raise JQDataBootstrapError("JQData universe query failed") from None


def _status_rows(
    sdk: object, symbols: list[str], day: str
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        for symbol in symbols:
            frame = sdk.get_price(
                _provider_symbol(symbol),
                start_date=day,
                end_date=day,
                frequency="daily",
                fields=["paused"],
                skip_paused=False,
                fq=None,
            )
            if len(frame) != 1 or "paused" not in frame.columns:
                raise JQDataBootstrapError("JQData returned invalid status rows")
            if _iso_date(frame.index[0]) != day:
                raise JQDataBootstrapError("JQData returned a non-exact status date")
            paused = int(frame.iloc[0]["paused"])
            if paused not in (0, 1):
                raise JQDataBootstrapError("JQData returned invalid status rows")
            rows.append(
                {
                    "effective_date": day,
                    "is_tradable": paused == 0,
                    "status": "active" if paused == 0 else "suspended",
                    "symbol": symbol,
                }
            )
    except JQDataBootstrapError:
        raise
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if all(token in message for token in ("账号", "权限", "数据")) or (
            "权限仅能获取" in message and "时间参数" in message
        ):
            raise JQDataBootstrapError(
                "JQData account cannot access the requested status date"
            ) from None
        raise JQDataBootstrapError("JQData status query failed") from None
    if {str(row["symbol"]) for row in rows} != set(symbols):
        raise JQDataBootstrapError("JQData status rows do not cover the panel")
    return sorted(rows, key=lambda row: str(row["symbol"]))


def build_bootstrap_artifact(
    sdk: object,
    *,
    panel: Iterable[tuple[str, str]],
    observed_at: str,
    max_rows: int,
) -> dict[str, object]:
    """Collect exact stock universe/status rows without granting authority."""
    normalized = _validate_inputs(panel, observed_at, max_rows)
    _check_quota(sdk, max_rows)

    by_day: dict[str, list[str]] = {}
    for symbol, day in sorted(normalized):
        by_day.setdefault(day, []).append(symbol)

    universe: list[dict[str, object]] = []
    statuses: list[dict[str, object]] = []
    for day, symbols in sorted(by_day.items()):
        day_statuses = _status_rows(sdk, symbols, day)
        day_universe = _universe_rows(sdk, day)
        if not set(symbols).issubset(
            {str(row["symbol"]) for row in day_universe}
        ):
            raise JQDataBootstrapError("JQData universe does not cover the panel")
        universe.extend(day_universe)
        if len(universe) > max_rows:
            raise JQDataBootstrapError("JQData row limit exceeded")
        statuses.extend(day_statuses)
        if len(universe) + len(statuses) > max_rows:
            raise JQDataBootstrapError("JQData row limit exceeded")

    return {
        "authoritative": False,
        "evidence_grade": EVIDENCE_GRADE,
        "instrument_status": sorted(
            statuses, key=lambda row: (str(row["effective_date"]), str(row["symbol"]))
        ),
        "observed_at": observed_at,
        "panel": [f"{symbol}@{day}" for symbol, day in sorted(normalized)],
        "provider": "jqdata",
        "universe_scope": "mainland_a_shares_xshg_xshe",
        "universe": sorted(
            universe, key=lambda row: (str(row["effective_date"]), str(row["symbol"]))
        ),
    }
