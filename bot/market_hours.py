"""
Time-of-day windows for the 0DTE strategy: when it's allowed to enter new
positions, and when open 0DTE positions must be force-closed regardless of
P&L to avoid end-of-day gamma risk.

Position.opened_at/closed_at and the broker check_exits() comparisons are
naive UTC throughout the codebase (datetime.utcnow()), so
zero_dte_force_close_utc() converts via a real ZoneInfo-aware conversion
rather than a fixed offset -- US/Eastern shifts between UTC-4 (EDT) and
UTC-5 (EST), and a hardcoded offset would force-close at the wrong
wall-clock time for roughly half the year.
"""
from __future__ import annotations
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

ZERO_DTE_SYMBOLS = {"SPY", "QQQ", "IWM"}

ZERO_DTE_ENTRY_START = time(10, 0)
ZERO_DTE_ENTRY_END = time(11, 30)
ZERO_DTE_FORCE_CLOSE = time(14, 30)


def is_0dte_entry_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    now_et = now.astimezone(EASTERN)
    return ZERO_DTE_ENTRY_START <= now_et.time() <= ZERO_DTE_ENTRY_END


def is_0dte_force_close_time(now: datetime | None = None) -> bool:
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    now_et = now.astimezone(EASTERN)
    return now_et.time() >= ZERO_DTE_FORCE_CLOSE


def zero_dte_force_close_utc(now: datetime | None = None) -> datetime:
    """Today's 14:30 ET, as a naive UTC datetime -- matches the naive-UTC
    convention used by Position.opened_at/closed_at and check_exits()."""
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    now_et = now.astimezone(EASTERN)
    close_et = now_et.replace(
        hour=ZERO_DTE_FORCE_CLOSE.hour, minute=ZERO_DTE_FORCE_CLOSE.minute, second=0, microsecond=0
    )
    return close_et.astimezone(timezone.utc).replace(tzinfo=None)
