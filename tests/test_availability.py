from datetime import datetime

from stockdata.availability import price_availability_error


def test_same_day_after_close_is_available_for_next_open():
    assert price_availability_error(
        "2020-01-03", datetime.fromisoformat("2020-01-03T15:06:00+08:00"), None
    ) is None


def test_weekend_capture_uses_frozen_next_session():
    assert price_availability_error(
        "2020-01-03",
        datetime.fromisoformat("2020-01-06T09:24:59+08:00"),
        "2020-01-06",
    ) is None


def test_intermediate_day_and_open_cutoff_are_rejected():
    assert price_availability_error(
        "2020-01-03",
        datetime.fromisoformat("2020-01-04T12:00:00+08:00"),
        "2020-01-06",
    ) == "post_hoc_availability"
    assert price_availability_error(
        "2020-01-03",
        datetime.fromisoformat("2020-01-06T09:25:00+08:00"),
        "2020-01-06",
    ) == "post_hoc_availability"


def test_final_bar_cannot_claim_unknown_cross_day_session():
    assert price_availability_error(
        "2020-01-03", datetime.fromisoformat("2020-01-06T09:00:00+08:00"), None
    ) == "unknown_next_session"


def test_final_bar_cannot_exist_before_close():
    assert price_availability_error(
        "2020-01-03", datetime.fromisoformat("2020-01-03T14:59:59+08:00"), None
    ) == "availability_precedes_finalization"
