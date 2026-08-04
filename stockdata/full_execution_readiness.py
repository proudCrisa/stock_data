"""Composite, read-only readiness for all execution snapshot components."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Iterable

from .execution_readiness import check_execution_readiness
from .forward_context import check_forward_context_readiness
from .forward_corporate_actions import check_forward_corporate_action_readiness
from .rqgm_provider_contract import REQUIRED_COMPONENTS
from .ticker import normalize


SCHEMA_VERSION = "stockdata-full-execution-readiness/1"


def check_full_execution_readiness(
    database: str | Path,
    *,
    source: str,
    adjustment_mode: str,
    adjustment_version: str,
    signal_adjustment_mode: str | None = None,
    signal_adjustment_version: str | None = None,
    panel: Iterable[tuple[str, str]],
) -> dict[str, object]:
    """Require every evidence class; unavailable collectors stay explicit blockers."""
    frozen_panel = frozenset(
        (normalize(symbol), date.fromisoformat(day).isoformat())
        for symbol, day in panel
    )
    canonical_panel = [f"{symbol}@{day}" for symbol, day in sorted(frozen_panel)]
    panel_sha256 = hashlib.sha256(
        json.dumps(canonical_panel, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    if (signal_adjustment_mode is None) != (signal_adjustment_version is None):
        raise ValueError(
            "signal adjustment mode and version must be provided together"
        )
    signal_mode = signal_adjustment_mode or adjustment_mode
    signal_version = signal_adjustment_version or adjustment_version
    execution_price = check_execution_readiness(
        database,
        source=source,
        adjustment_mode=adjustment_mode,
        adjustment_version=adjustment_version,
        panel=frozen_panel,
    )
    if adjustment_mode != "raw":
        execution_price = {
            **execution_price,
            "ready": False,
            "blockers": [
                *execution_price.get("blockers", []),
                {"code": "execution_prices_require_raw_adjustment", "count": 1},
            ],
        }
    signal_price = check_execution_readiness(
        database,
        source=source,
        adjustment_mode=signal_mode,
        adjustment_version=signal_version,
        panel=frozen_panel,
    )
    try:
        context = check_forward_context_readiness(
            str(Path(database).expanduser().resolve()), frozen_panel
        )
    except (OSError, ValueError):
        context = {
            "ready": False,
            "blockers": [{"code": "context_database_unreadable", "count": 1}],
        }
    try:
        corporate_actions = check_forward_corporate_action_readiness(
            str(Path(database).expanduser().resolve()), frozen_panel
        )
    except (OSError, ValueError):
        corporate_actions = {
            "ready": False,
            "integrity_ready": False,
            "blockers": [
                {"code": "corporate_action_database_unreadable", "count": 1}
            ],
        }
    universe = {
        **context,
        "ready": False,
        "blockers": [
            *context.get("blockers", []),
            {"code": "forward_universe_publisher_key_not_enrolled", "count": 1},
        ],
    }
    instrument_status = {
        **context,
        "ready": False,
        "blockers": [
            *context.get("blockers", []),
            {"code": "instrument_status_is_activity_proxy", "count": 1},
        ],
    }
    unavailable = {
        "trading_calendar": {
            "ready": False,
            "blockers": [
                {"code": "signed_trading_calendar_not_enrolled", "count": 1}
            ],
        },
        "market_rules": {
            "ready": False,
            "blockers": [
                {"code": "official_rulebook_bundle_not_enrolled", "count": 1}
            ],
        },
        "availability_records": {
            "ready": False,
            "blockers": [
                {
                    "code": "complete_component_availability_records_not_bound",
                    "count": 1,
                }
            ],
        },
    }
    components = {
        "execution_prices": execution_price,
        "signal_prices": signal_price,
        "decision_context": context,
        "universe": universe,
        "instrument_status": instrument_status,
        "corporate_actions": corporate_actions,
        **unavailable,
    }
    if set(components) != set(REQUIRED_COMPONENTS):
        raise AssertionError("full readiness component set drifted")
    blockers = []
    for component, report in components.items():
        for blocker in report.get("blockers", []):
            blockers.append({**blocker, "component": component})
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": not blockers and all(
            item.get("ready") is True for item in components.values()
        ),
        "database": str(Path(database).expanduser()),
        "panel_size": len(frozen_panel),
        "request": {
            "source": source,
            "execution_adjustment": {
                "mode": adjustment_mode,
                "version": adjustment_version,
            },
            "signal_adjustment": {
                "mode": signal_mode,
                "version": signal_version,
            },
            "panel_size": len(frozen_panel),
            "panel_sha256": panel_sha256,
        },
        "blockers": blockers,
        "components": components,
    }
