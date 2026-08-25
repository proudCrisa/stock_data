"""Execution-complete dated A-share market-rule payload validation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, timedelta
from typing import cast

MARKET_RULE_PAYLOAD_SCHEMA = "stockdata-market-rule-payload/1"

_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "source",
        "source_sha256",
        "security_type",
        "board",
        "exchange",
        "effective_from",
        "effective_until",
        "listing_age_min",
        "listing_age_max",
        "is_st",
        "lot_size",
        "t_plus_one",
        "reject_suspended",
        "reject_zero_volume",
        "price_limit_up",
        "price_limit_down",
        "price_limit_reference",
        "price_tick",
        "price_rounding",
        "locked_limit_order_policy",
        "commission_rate",
        "minimum_commission",
        "transfer_fee_rate",
        "stamp_duty_sell_rate",
        "slippage_model",
        "slippage_bps",
        "slippage_bounds",
        "time_in_force",
        "cancel_unfilled_at_close",
    }
)
_BOARD_EXCHANGES = {
    "MAIN": frozenset({"SH", "SZ"}),
    "CHINEXT": frozenset({"SZ"}),
    "STAR": frozenset({"SH"}),
    "BSE": frozenset({"BJ"}),
}


def _symbol_board(symbol: str) -> str:
    """Resolve an A-share board from the exact exchange-assigned security code."""

    digits, separator, exchange = symbol.partition(".")
    if (
        separator != "."
        or len(digits) != 6
        or not digits.isdigit()
        or exchange not in {"SH", "SZ", "BJ"}
    ):
        raise ValueError("market_rules panel symbol is not a supported A-share code")
    if exchange == "BJ":
        if digits.startswith(("4", "8", "9")):
            return "BSE"
    elif exchange == "SH":
        if digits.startswith(("688", "689")):
            return "STAR"
        if digits.startswith(("600", "601", "603", "605")):
            return "MAIN"
    elif exchange == "SZ":
        if digits.startswith(("300", "301")):
            return "CHINEXT"
        if digits.startswith(("000", "001", "002", "003")):
            return "MAIN"
    raise ValueError("market_rules panel symbol has no supported A-share board")


def _validate_status_scope(
    status: Mapping[str, object],
    *,
    payload: Mapping[str, object],
) -> None:
    if set(status) != {"is_st", "is_suspended", "listing_status"}:
        raise ValueError("market_rules instrument status authority is incomplete")
    if type(status["is_st"]) is not bool or type(status["is_suspended"]) is not bool:
        raise ValueError("market_rules instrument status authority flags are invalid")
    if status["listing_status"] not in {"listed", "suspended", "delisted"}:
        raise ValueError("market_rules instrument status authority listing state is invalid")
    if status["listing_status"] != "listed" or status["is_suspended"]:
        raise ValueError("market_rules cannot admit a non-tradable instrument status")
    if payload["is_st"] is not status["is_st"]:
        raise ValueError("market_rules ST exception differs from signed instrument status")


def _canonical_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"market_rules {field} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"market_rules {field} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"market_rules {field} must be a canonical ISO date")
    return parsed


def _non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"market_rules {field} must be non-empty")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"market_rules {field} must be lowercase SHA-256")
    return value


def _number(value: object, field: str, *, upper: float | None = None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0.0
        or (upper is not None and value > upper)
    ):
        raise ValueError(f"market_rules {field} is invalid")
    return float(value)


def validate_market_rule_payload(
    payload: object,
    *,
    panel_entry: str | None = None,
    instrument_status: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Validate one complete rule regime and its inclusive panel-date coverage."""

    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        raise ValueError("market_rules record payload is incomplete")
    if payload["schema_version"] != MARKET_RULE_PAYLOAD_SCHEMA:
        raise ValueError("market_rules payload schema version is invalid")
    _non_empty(payload["policy_id"], "policy_id")
    _non_empty(payload["source"], "source")
    _sha256(payload["source_sha256"], "source_sha256")
    if payload["security_type"] != "A_SHARE":
        raise ValueError("market_rules security_type must be A_SHARE")

    board = payload["board"]
    exchange = payload["exchange"]
    if not isinstance(board, str) or not isinstance(exchange, str):
        raise ValueError("market_rules board/exchange scope is invalid")
    if board not in _BOARD_EXCHANGES or exchange not in _BOARD_EXCHANGES[board]:
        raise ValueError("market_rules board/exchange scope is invalid")
    effective_from = _canonical_date(payload["effective_from"], "effective_from")
    effective_until = _canonical_date(payload["effective_until"], "effective_until")
    if effective_from > effective_until:
        raise ValueError("market_rules validity interval is reversed")

    listing_age_min = payload["listing_age_min"]
    listing_age_max = payload["listing_age_max"]
    if (
        isinstance(listing_age_min, bool)
        or not isinstance(listing_age_min, int)
        or listing_age_min < 0
        or (
            listing_age_max is not None
            and (
                isinstance(listing_age_max, bool)
                or not isinstance(listing_age_max, int)
                or listing_age_max < listing_age_min
            )
        )
    ):
        raise ValueError("market_rules listing-age interval is invalid")
    if payload["is_st"] is not None and type(payload["is_st"]) is not bool:
        raise ValueError("market_rules is_st must be boolean or null")
    if payload["lot_size"] != 100 or isinstance(payload["lot_size"], bool):
        raise ValueError("market_rules A-share lot_size must be 100")
    if payload["t_plus_one"] is not True:
        raise ValueError("market_rules A-share settlement must be T+1")
    if (
        payload["reject_suspended"] is not True
        or payload["reject_zero_volume"] is not True
    ):
        raise ValueError("market_rules suspension behavior is invalid")

    upper_limit = payload["price_limit_up"]
    lower_limit = payload["price_limit_down"]
    if (upper_limit is None) != (lower_limit is None):
        raise ValueError("market_rules price limits must be paired")
    for field, value in (
        ("price_limit_up", upper_limit),
        ("price_limit_down", lower_limit),
    ):
        if value is not None:
            _number(value, field, upper=1.0)
    if (
        payload["price_limit_reference"] != "RECORD_OR_PREVIOUS_CLOSE"
        or payload["price_tick"] != 0.01
        or isinstance(payload["price_tick"], bool)
        or payload["price_rounding"] != "HALF_UP"
        or payload["locked_limit_order_policy"] != "REJECT_SIDE"
    ):
        raise ValueError("market_rules price-limit behavior is invalid")

    for field in (
        "commission_rate",
        "minimum_commission",
        "transfer_fee_rate",
        "stamp_duty_sell_rate",
        "slippage_bps",
    ):
        _number(payload[field], field)
    if (
        payload["slippage_model"] != "OPEN_BPS"
        or payload["slippage_bounds"] != "BAR_AND_PRICE_LIMITS"
    ):
        raise ValueError("market_rules slippage model is invalid")
    if (
        payload["time_in_force"] != "DAY"
        or payload["cancel_unfilled_at_close"] is not True
    ):
        raise ValueError("market_rules order lifecycle is invalid")

    if panel_entry is not None:
        symbol, separator, day = panel_entry.partition("@")
        if not symbol or separator != "@" or "@" in day:
            raise ValueError("market_rules panel entry is invalid")
        panel_day = _canonical_date(day, "panel date")
        if not effective_from <= panel_day <= effective_until:
            raise ValueError("market_rules validity does not cover panel date")
        symbol_exchange = symbol.rsplit(".", 1)[-1].upper()
        if symbol_exchange != exchange:
            raise ValueError("market_rules exchange does not match panel symbol")
        if board != _symbol_board(symbol):
            raise ValueError("market_rules board does not match panel symbol")
        if instrument_status is not None:
            if payload["is_st"] is None:
                raise ValueError(
                    "market_rules execution authority cannot use an ST wildcard"
                )
            _validate_status_scope(instrument_status, payload=payload)
    elif instrument_status is not None:
        raise ValueError("market_rules instrument status requires a panel entry")
    return payload


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def validate_market_rule_regimes(
    payloads: list[Mapping[str, object]],
) -> None:
    """Require unambiguous, gap-free policy identities for each rule scope."""

    by_policy: dict[str, bytes] = {}
    unique_payloads: list[Mapping[str, object]] = []
    for payload in payloads:
        validate_market_rule_payload(payload)
        policy_id = str(payload["policy_id"])
        canonical = _canonical(payload)
        previous_canonical = by_policy.get(policy_id)
        if previous_canonical is not None:
            if previous_canonical != canonical:
                raise ValueError(
                    "market_rules policy_id maps to different canonical rules"
                )
            continue
        by_policy[policy_id] = canonical
        unique_payloads.append(payload)

    for index, first in enumerate(unique_payloads):
        for second in unique_payloads[index + 1 :]:
            if any(
                first[field] != second[field]
                for field in ("security_type", "board", "exchange")
            ):
                continue
            first_start = _canonical_date(first["effective_from"], "effective_from")
            first_end = _canonical_date(first["effective_until"], "effective_until")
            second_start = _canonical_date(
                second["effective_from"], "effective_from"
            )
            second_end = _canonical_date(second["effective_until"], "effective_until")
            dates_overlap = first_start <= second_end and second_start <= first_end
            first_age_min = cast(int, first["listing_age_min"])
            first_age_end = cast(int | None, first["listing_age_max"])
            second_age_min = cast(int, second["listing_age_min"])
            second_age_end = cast(int | None, second["listing_age_max"])
            ages_overlap = (
                second_age_end is None
                or first_age_min <= second_age_end
            ) and (
                first_age_end is None
                or second_age_min <= first_age_end
            )
            st_overlap = (
                first["is_st"] is None
                or second["is_st"] is None
                or first["is_st"] == second["is_st"]
            )
            if dates_overlap and ages_overlap and st_overlap:
                raise ValueError("market_rules policy regimes overlap")

    by_scope: dict[tuple[object, ...], list[tuple[date, date, str]]] = {}
    for payload in unique_payloads:
        policy_id = str(payload["policy_id"])
        scope = (
            payload["security_type"],
            payload["board"],
            payload["exchange"],
            payload["listing_age_min"],
            payload["listing_age_max"],
            payload["is_st"],
        )
        by_scope.setdefault(scope, []).append(
            (
                _canonical_date(payload["effective_from"], "effective_from"),
                _canonical_date(payload["effective_until"], "effective_until"),
                policy_id,
            )
        )

    for regimes in by_scope.values():
        ordered = sorted(regimes)
        for previous_regime, current_regime in zip(ordered, ordered[1:]):
            if current_regime[0] > previous_regime[1] + timedelta(days=1):
                raise ValueError("market_rules policy regimes contain a gap")


__all__ = [
    "MARKET_RULE_PAYLOAD_SCHEMA",
    "validate_market_rule_payload",
    "validate_market_rule_regimes",
]
