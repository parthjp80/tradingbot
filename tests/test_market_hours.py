from datetime import datetime
from zoneinfo import ZoneInfo

from bot.market_hours import (
    EASTERN,
    is_0dte_entry_window,
    is_0dte_force_close_time,
    zero_dte_force_close_utc,
)


def test_is_0dte_entry_window_true_at_1015am():
    now = datetime(2026, 9, 14, 10, 15, tzinfo=EASTERN)  # Monday
    assert is_0dte_entry_window(now) is True


def test_is_0dte_entry_window_false_before_10am():
    now = datetime(2026, 9, 14, 9, 45, tzinfo=EASTERN)
    assert is_0dte_entry_window(now) is False


def test_is_0dte_entry_window_false_after_1130am():
    now = datetime(2026, 9, 14, 11, 45, tzinfo=EASTERN)
    assert is_0dte_entry_window(now) is False


def test_is_0dte_entry_window_boundaries_inclusive():
    assert is_0dte_entry_window(datetime(2026, 9, 14, 10, 0, tzinfo=EASTERN)) is True
    assert is_0dte_entry_window(datetime(2026, 9, 14, 11, 30, tzinfo=EASTERN)) is True


def test_is_0dte_force_close_time_true_at_230pm():
    now = datetime(2026, 9, 14, 14, 30, tzinfo=EASTERN)
    assert is_0dte_force_close_time(now) is True


def test_is_0dte_force_close_time_false_before_230pm():
    now = datetime(2026, 9, 14, 14, 0, tzinfo=EASTERN)
    assert is_0dte_force_close_time(now) is False


def test_zero_dte_force_close_utc_handles_dst():
    # January = EST = UTC-5; July = EDT = UTC-4. Same 14:30 ET wall-clock
    # time must therefore land at a different UTC hour, or the force-close
    # would fire an hour early/late for roughly half the year.
    winter = zero_dte_force_close_utc(datetime(2026, 1, 12, 10, 0, tzinfo=EASTERN))
    summer = zero_dte_force_close_utc(datetime(2026, 7, 13, 10, 0, tzinfo=EASTERN))
    assert winter.hour == 19  # 14:30 EST -> 19:30 UTC
    assert summer.hour == 18  # 14:30 EDT -> 18:30 UTC
    assert winter.tzinfo is None and summer.tzinfo is None  # naive UTC, matches Position.opened_at convention


def test_zero_dte_force_close_utc_same_day():
    now = datetime(2026, 9, 14, 10, 30, tzinfo=EASTERN)
    result = zero_dte_force_close_utc(now)
    # 14:30 EDT on 2026-09-14 -> 18:30 UTC on 2026-09-14
    assert result == datetime(2026, 9, 14, 18, 30)
