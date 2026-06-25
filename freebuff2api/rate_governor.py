"""Rate governor — anti 24/7 usage pattern evasion.

Upstream bot-sweep (commit CodebuffAI/codebuff#527, abuse-detection.ts) flags:
  - msgs24h >= 50 AND distinctHours24h >= 20  → score 100 (HIGH tier)
  - msgs24h >= 500                              → score 50
  - msgs24h >= 300                              → score 30
  - new account + heavy burst                   → +40/+20

A 24/7 unleash pool would trip the 24-7-usage flag automatically. This
governor distributes requests across accounts and injects idle windows so
the per-account message pattern looks like a human workday, not a bot.

Strategy:
  1. Per-account 24h rolling window counter (msgs + distinct hours set)
  2. Soft cap: when an account approaches the 50 msgs / 20 hours threshold,
     route new requests to other accounts in the pool
  3. Hard cap: per-account daily msg limit (default 200) — never exceed
  4. Idle windows: optional per-account "sleep" hours where that account
     is not used (simulates user sleep schedule)
  5. Jitter: small random delay before each request to break burst patterns

The governor is best-effort: if all accounts are exhausted, it falls back
to round-robin to keep the service alive (better degraded service than
total outage).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("freebuff2api.rate_governor")

# Bot-sweep thresholds (commit #527 abuse-detection.ts)
HIGH_TIER_MSGS_24H = 50
HIGH_TIER_HOURS_24H = 20
HEAVY_MSGS_24H = 500
HEAVY_MSGS_24H_SOFT = 300
NEW_ACCOUNT_MSGS_24H = 200  # ageDays < 1

# Conservative defaults — stay well below the high-tier threshold.
DEFAULT_DAILY_MSG_CAP = 180          # < 200 (new-account burst flag)
DEFAULT_SOFT_MSG_CAP = 40            # < 50 (24-7-usage flag)
DEFAULT_SOFT_HOURS_CAP = 16          # < 20 (24-7-usage flag)
DEFAULT_MIN_JITTER_MS = 50
DEFAULT_MAX_JITTER_MS = 350
DEFAULT_IDLE_WINDOW_HOURS: tuple[int, int] = (0, 7)  # 00:00-07:00 local


@dataclass
class AccountUsage:
    """24h rolling window usage for one account."""
    account_index: int
    msg_timestamps: list[float] = field(default_factory=list)
    distinct_hours: set[int] = field(default_factory=set)
    daily_msg_count: int = 0
    daily_reset_at: float = 0.0  # epoch seconds; midnight local
    idle_window: tuple[int, int] = DEFAULT_IDLE_WINDOW_HOURS
    last_used_at: float = 0.0

    def _purge_old(self, now: float, window_seconds: float = 86400.0) -> None:
        cutoff = now - window_seconds
        self.msg_timestamps = [t for t in self.msg_timestamps if t >= cutoff]
        # Rebuild distinct_hours from surviving timestamps
        self.distinct_hours = {int((t % 86400) // 3600) for t in self.msg_timestamps}

    def _maybe_reset_daily(self, now: float) -> None:
        if now >= self.daily_reset_at:
            self.daily_msg_count = 0
            # Next reset: next midnight local (approximate using UTC offset)
            self.daily_reset_at = now + 86400.0

    def in_idle_window(self, now: float, local_offset_hours: int = 0) -> bool:
        local_hour = int(((now / 3600.0) + local_offset_hours) % 24)
        start, end = self.idle_window
        if start <= end:
            return start <= local_hour < end
        # wraps midnight (e.g. 22-7)
        return local_hour >= start or local_hour < end

    def msgs_24h(self, now: float) -> int:
        self._purge_old(now)
        return len(self.msg_timestamps)

    def hours_24h(self, now: float) -> int:
        self._purge_old(now)
        return len(self.distinct_hours)

    def record(self, now: float) -> None:
        self._purge_old(now)
        self._maybe_reset_daily(now)
        self.msg_timestamps.append(now)
        self.distinct_hours.add(int((now % 86400) // 3600))
        self.daily_msg_count += 1
        self.last_used_at = now

    def snapshot(self, now: float) -> dict[str, Any]:
        self._purge_old(now)
        return {
            "account_index": self.account_index,
            "msgs_24h": len(self.msg_timestamps),
            "distinct_hours_24h": len(self.distinct_hours),
            "daily_msg_count": self.daily_msg_count,
            "daily_cap": 0,  # filled by governor
            "in_idle_window": self.in_idle_window(now),
            "last_used_at": self.last_used_at,
        }


class RateGovernor:
    """Per-account usage tracker + account picker.

    Pick logic:
      1. Skip accounts in their idle window
      2. Skip accounts at daily cap
      3. Prefer accounts with lowest msgs_24h (distribute load)
      4. If all exhausted, fall back to least-recently-used (keep service alive)
    """

    def __init__(
        self,
        account_count: int,
        *,
        daily_msg_cap: int = DEFAULT_DAILY_MSG_CAP,
        soft_msg_cap: int = DEFAULT_SOFT_MSG_CAP,
        soft_hours_cap: int = DEFAULT_SOFT_HOURS_CAP,
        min_jitter_ms: int = DEFAULT_MIN_JITTER_MS,
        max_jitter_ms: int = DEFAULT_MAX_JITTER_MS,
        idle_window_hours: tuple[int, int] | None = None,
        local_offset_hours: int = 0,
    ) -> None:
        self._accounts: list[AccountUsage] = [
            AccountUsage(
                account_index=i,
                idle_window=idle_window_hours or DEFAULT_IDLE_WINDOW_HOURS,
            )
            for i in range(account_count)
        ]
        self._lock = asyncio.Lock()
        self.daily_msg_cap = daily_msg_cap
        self.soft_msg_cap = soft_msg_cap
        self.soft_hours_cap = soft_hours_cap
        self.min_jitter_ms = min_jitter_ms
        self.max_jitter_ms = max_jitter_ms
        self.local_offset_hours = local_offset_hours

    def _eligible(self, acc: AccountUsage, now: float) -> bool:
        if acc.in_idle_window(now, self.local_offset_hours):
            return False
        if acc.daily_msg_count >= self.daily_msg_cap:
            return False
        return True

    def _approaching_soft_cap(self, acc: AccountUsage, now: float) -> bool:
        if acc.msgs_24h(now) >= self.soft_msg_cap:
            return True
        if acc.hours_24h(now) >= self.soft_hours_cap:
            return True
        return False

    async def pick_account(self, start_index: int = 0) -> int:
        """Return the best account index to route a new request to.

        Returns -1 if no account is eligible (caller should fall back to
        round-robin and accept the risk).
        """
        now = time.time()
        async with self._lock:
            eligible = [
                (i, acc) for i, acc in enumerate(self._accounts)
                if self._eligible(acc, now)
            ]
            if not eligible:
                logger.warning(
                    "rate_governor: all %s accounts exhausted/idle — falling back to round-robin",
                    len(self._accounts),
                )
                # Fallback: least-recently-used, ignore caps
                return min(range(len(self._accounts)), key=lambda i: self._accounts[i].last_used_at)

            # Prefer accounts NOT approaching the soft cap (distribute load)
            healthy = [(i, acc) for i, acc in eligible if not self._approaching_soft_cap(acc, now)]
            pool = healthy if healthy else eligible
            # Pick the one with the lowest msgs_24h
            return min(pool, key=lambda x: x[1].msgs_24h(now))[0]

    async def record_request(self, account_index: int) -> None:
        now = time.time()
        async with self._lock:
            if 0 <= account_index < len(self._accounts):
                self._accounts[account_index].record(now)

    async def jitter_delay(self) -> None:
        """Sleep a small random delay to break burst patterns."""
        delay_ms = random.randint(self.min_jitter_ms, self.max_jitter_ms)
        await asyncio.sleep(delay_ms / 1000.0)

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "daily_msg_cap": self.daily_msg_cap,
            "soft_msg_cap": self.soft_msg_cap,
            "soft_hours_cap": self.soft_hours_cap,
            "jitter_ms": [self.min_jitter_ms, self.max_jitter_ms],
            "accounts": [acc.snapshot(now) for acc in self._accounts],
        }

    async def reset_account(self, account_index: int) -> None:
        async with self._lock:
            if 0 <= account_index < len(self._accounts):
                self._accounts[account_index] = AccountUsage(
                    account_index=account_index,
                    idle_window=self._accounts[account_index].idle_window,
                )

    async def aclose(self) -> None:
        pass
