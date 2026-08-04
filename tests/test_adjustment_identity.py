from __future__ import annotations

from copy import deepcopy

import pytest

from stockdata.adjustment_identity import (
    EXECUTION_ADJUSTMENT_SCHEMA,
    SIGNAL_ADJUSTMENT_SCHEMA,
    verify_adjustment_identity,
)


def _identity(role: str, mode: str) -> dict[str, str]:
    return {
        "schema_version": (
            EXECUTION_ADJUSTMENT_SCHEMA
            if role == "execution"
            else SIGNAL_ADJUSTMENT_SCHEMA
        ),
        "price_role": role,
        "source": "baostock",
        "adjustment_mode": mode,
        "adjustment_version": f"baostock-{mode}-v1",
    }


def test_raw_execution_and_declared_signal_identities_are_separate() -> None:
    execution = verify_adjustment_identity(
        _identity("execution", "raw"), expected_price_role="execution"
    )
    signal = verify_adjustment_identity(
        _identity("signal", "qfq"), expected_price_role="signal"
    )

    assert execution.adjustment_mode == "raw"
    assert signal.adjustment_mode == "qfq"
    assert execution.identifier != signal.identifier


def test_execution_identity_rejects_adjusted_prices() -> None:
    with pytest.raises(ValueError, match="must use raw"):
        verify_adjustment_identity(
            _identity("execution", "qfq"),
            expected_price_role="execution",
        )


def test_roles_cannot_be_swapped_even_when_both_modes_are_raw() -> None:
    with pytest.raises(ValueError, match="wrong role or schema"):
        verify_adjustment_identity(
            _identity("signal", "raw"), expected_price_role="execution"
        )


def test_source_mode_or_version_changes_content_identity() -> None:
    original = _identity("signal", "raw")
    original_id = verify_adjustment_identity(
        original, expected_price_role="signal"
    ).identifier

    for field, replacement in (
        ("source", "tencent"),
        ("adjustment_mode", "qfq"),
        ("adjustment_version", "revised-v2"),
    ):
        changed = deepcopy(original)
        changed[field] = replacement
        assert (
            verify_adjustment_identity(
                changed, expected_price_role="signal"
            ).identifier
            != original_id
        )
