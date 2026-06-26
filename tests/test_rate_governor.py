"""Tests for freebuff2api/rate_governor.py — anti 24/7 pattern evasion."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from freebuff2api.rate_governor import (
    AccountUsage,
    DEFAULT_DAILY_MSG_CAP,
    DEFAULT_SOFT_MSG_CAP,
    DEFAULT_SOFT_HOURS_CAP,
    HIGH_TIER_HOURS_24H,
    HIGH_TIER_MSGS_24H,
    RateGovernor,
)


def test_account_usage_records_msgs():
    acc = AccountUsage(account_index=0)
    now = 1000.0
    acc.record(now)
    acc.record(now + 60)
    acc.record(now + 120)
    assert acc.msgs_24h(now + 200) == 3
    assert acc.daily_msg_count == 3


def test_account_usage_distinct_hours():
    acc = AccountUsage(account_index=0)
    # 3 timestamps in 3 different hours
    base = 0.0
    acc.record(base)                       # hour 0
    acc.record(base + 3600)               # hour 1
    acc.record(base + 7200)               # hour 2
    assert acc.hours_24h(base + 8000) == 3


def test_account_usage_purges_old_timestamps():
    acc = AccountUsage(account_index=0)
    now = 1_000_000.0
    acc.record(now - 100_000)  # ~27h ago, should be purged
    acc.record(now - 50)       # recent
    assert acc.msgs_24h(now) == 1


def test_account_usage_idle_window():
    # idle 0-7
    acc = AccountUsage(account_index=0, idle_window=(0, 7))
    # 03:00 local → in idle
    # Use a timestamp where hour-of-day = 3
    t = 3 * 3600  # 03:00 UTC, local_offset=0
    assert acc.in_idle_window(t, local_offset_hours=0) is True
    # 12:00 local → not in idle
    t = 12 * 3600
    assert acc.in_idle_window(t, local_offset_hours=0) is False


def test_account_usage_idle_window_wraps_midnight():
    acc = AccountUsage(account_index=0, idle_window=(22, 7))
    # 23:00 → in idle
    assert acc.in_idle_window(23 * 3600, local_offset_hours=0) is True
    # 03:00 → in idle
    assert acc.in_idle_window(3 * 3600, local_offset_hours=0) is True
    # 12:00 → not in idle
    assert acc.in_idle_window(12 * 3600, local_offset_hours=0) is False


def test_account_usage_idle_window_disabled():
    """None idle_window → never in idle (default — gateway always usable)."""
    acc = AccountUsage(account_index=0, idle_window=None)
    for hour in (0, 3, 6, 12, 18, 23):
        assert acc.in_idle_window(hour * 3600, local_offset_hours=0) is False


def test_account_usage_daily_cap_reset():
    acc = AccountUsage(account_index=0)
    now = 1000.0
    acc.daily_reset_at = now + 10  # reset in 10 seconds
    acc.record(now)
    assert acc.daily_msg_count == 1
    # After reset time
    acc.record(now + 20)
    # Should have reset to 0 then counted 1
    assert acc.daily_msg_count == 1


def test_rate_governor_pick_distributes_load():
    gov = RateGovernor(account_count=3)
    # Use a time at 12:00 UTC so no account is in idle window (idle=0-7)
    now = 12 * 3600.0
    # Pre-fill account 0 with 10 msgs, account 1 with 5, account 2 with 0
    for _ in range(10):
        gov._accounts[0].record(now)
    for _ in range(5):
        gov._accounts[1].record(now)
    # Reset daily_msg_count to 0 so cap doesn't interfere (record increments it)
    for acc in gov._accounts:
        acc.daily_msg_count = 0

    with patch("freebuff2api.rate_governor.time.time", return_value=now):
        picked = asyncio.run(gov.pick_account())
    # Account 2 has fewest msgs → picked
    assert picked == 2


def test_rate_governor_skips_idle_accounts():
    # Idle window opt-in — default is None (disabled)
    gov = RateGovernor(account_count=3, idle_window_hours=(0, 7))
    # Force all accounts into idle window (use a time at 03:00 local)
    t = 3 * 3600
    with patch("freebuff2api.rate_governor.time.time", return_value=t):
        picked = asyncio.run(gov.pick_account())
    # All idle → return -1 (signal caller to fall back to default round-robin)
    assert picked == -1


def test_rate_governor_skips_daily_cap():
    gov = RateGovernor(account_count=2, daily_msg_cap=2)
    now = time.time()
    # Burn through account 0's daily cap
    gov._accounts[0].daily_msg_count = 2
    gov._accounts[0].daily_reset_at = now + 99999  # not reset yet
    picked = asyncio.run(gov.pick_account())
    assert picked == 1  # account 0 is at cap, pick account 1


def test_rate_governor_record_request():
    gov = RateGovernor(account_count=2)
    asyncio.run(gov.record_request(1))
    assert gov._accounts[1].daily_msg_count == 1
    assert len(gov._accounts[1].msg_timestamps) == 1


def test_rate_governor_status_snapshot():
    gov = RateGovernor(account_count=2)
    asyncio.run(gov.record_request(0))
    status = gov.status()
    assert status["daily_msg_cap"] == DEFAULT_DAILY_MSG_CAP
    assert status["soft_msg_cap"] == DEFAULT_SOFT_MSG_CAP
    assert len(status["accounts"]) == 2
    assert status["accounts"][0]["daily_msg_count"] == 1
    assert status["accounts"][1]["daily_msg_count"] == 0


def test_rate_governor_reset_account():
    gov = RateGovernor(account_count=2)
    asyncio.run(gov.record_request(0))
    asyncio.run(gov.record_request(0))
    assert gov._accounts[0].daily_msg_count == 2
    asyncio.run(gov.reset_account(0))
    assert gov._accounts[0].daily_msg_count == 0
    assert gov._accounts[0].msg_timestamps == []


def test_rate_governor_defaults_below_bot_sweep_thresholds():
    # Critical: defaults MUST stay below the HIGH-tier thresholds
    assert DEFAULT_DAILY_MSG_CAP < 200  # new-account burst flag
    assert DEFAULT_SOFT_MSG_CAP < HIGH_TIER_MSGS_24H  # 50
    assert DEFAULT_SOFT_HOURS_CAP < HIGH_TIER_HOURS_24H  # 20


def test_rate_governor_jitter_delay_returns():
    gov = RateGovernor(account_count=1, min_jitter_ms=10, max_jitter_ms=20)
    # Just verify it doesn't raise and completes quickly
    asyncio.run(asyncio.wait_for(gov.jitter_delay(), timeout=1.0))


def test_rate_governor_all_exhausted_falls_back_to_lru():
    gov = RateGovernor(account_count=2, daily_msg_cap=1)
    now = time.time()
    # Both at cap
    for i in range(2):
        gov._accounts[i].daily_msg_count = 1
        gov._accounts[i].daily_reset_at = now + 99999
    # Set last_used_at different
    gov._accounts[0].last_used_at = now - 100
    gov._accounts[1].last_used_at = now - 200
    picked = asyncio.run(gov.pick_account())
    # All exhausted → return -1 (signal caller to fall back to default round-robin)
    assert picked == -1


def test_rate_governor_prefer_healthy_over_soft_cap_approaching():
    gov = RateGovernor(account_count=2, soft_msg_cap=10)
    now = time.time()
    # Account 0 at soft cap (10 msgs), account 1 at 5 msgs
    for _ in range(10):
        gov._accounts[0].record(now)
    for _ in range(5):
        gov._accounts[1].record(now)
    picked = asyncio.run(gov.pick_account())
    # Account 0 approaching soft cap → prefer account 1
    assert picked == 1
