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
  3. Hard cap: per-account daily msg limit (default 180) — never exceed
  4. Idle windows: optional per-account "sleep" hours (simulates user sleep)
  5. Activity phase: per-account minute offset (stagger concurrent usage)
  6. Jitter: small random delay before each request to break burst patterns

The governor is best-effort: if all accounts hit hard daily cap, returns -1.
If only soft-cap/idle-blocked, still picks least-loaded to avoid blind
round-robin that bypasses stealth entirely.
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
# Idle window disabled by default — opt-in via FREEBUFF_IDLE_WINDOW_HOURS.
DEFAULT_IDLE_WINDOW_HOURS: tuple[int, int] | None = None


def _local_hour(now: float, local_offset_hours: int, activity_phase_minutes: int = 0) -> int:
    """Hour-of-day (0-23) in the configured local timezone + phase offset."""
    shifted = now + local_offset_hours * 3600 + activity_phase_minutes * 60
    return int((shifted % 86400) // 3600)


def _next_midnight_local(now: float, local_offset_hours: int) -> float:
    """Epoch seconds of the next local midnight (for daily cap reset)."""
    shifted = now + local_offset_hours * 3600
    seconds_into_day = shifted % 86400
    return now + (86400 - seconds_into_day)


@dataclass
class AccountUsage:
    """24h rolling window usage for one account."""

    account_index: int
    msg_timestamps: list[float] = field(default_factory=list)
    distinct_hours: set[int] = field(default_factory=set)
    daily_msg_count: int = 0
    daily_reset_at: float = 0.0  # epoch seconds; next local midnight
    idle_window: tuple[int, int] | None = DEFAULT_IDLE_WINDOW_HOURS
    activity_phase_minutes: int = 0
    local_offset_hours: int = 0
    last_used_at: float = 0.0

    def _purge_old(self, now: float, window_seconds: float = 86400.0) -> None:
        cutoff = now - window_seconds
        self.msg_timestamps = [t for t in self.msg_timestamps if t >= cutoff]
        self.distinct_hours = {
            _local_hour(t, self.local_offset_hours, self.activity_phase_minutes)
            for t in self.msg_timestamps
        }

    def _maybe_reset_daily(self, now: float) -> None:
        if self.daily_reset_at <= 0:
            self.daily_reset_at = _next_midnight_local(now, self.local_offset_hours)
        if now >= self.daily_reset_at:
            self.daily_msg_count = 0
            self.daily_reset_at = _next_midnight_local(now, self.local_offset_hours)

    def in_idle_window(self, now: float) -> bool:
        if self.idle_window is None:
            return False
        local_hour = _local_hour(now, self.local_offset_hours, self.activity_phase_minutes)
        start, end = self.idle_window
        if start <= end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end

    def in_activity_phase(self, now: float, stagger_minutes: int = 15) -> bool:
        """True if this account's activity phase allows routing now.

        Spreads account usage across the hour so N accounts don't all fire
        at :00 — reduces temporal correlation in bot-sweep.
        """
        if stagger_minutes <= 0 or self.activity_phase_minutes <= 0:
            return True
        minute = int((now // 60) % 60)
        # Allow a 3-minute window centered on the phase minute.
        diff = (minute - self.activity_phase_minutes) % 60
        return diff <= 2 or diff >= 58

    def at_hard_daily_cap(self, daily_cap: int) -> bool:
        return self.daily_msg_count >= daily_cap

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
        self.distinct_hours.add(
            _local_hour(now, self.local_offset_hours, self.activity_phase_minutes)
        )
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
            "in_activity_phase": self.in_activity_phase(now),
            "activity_phase_minutes": self.activity_phase_minutes,
            "last_used_at": self.last_used_at,
        }


class RateGovernor:
    """Per-account usage tracker + account picker."""

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
        activity_phases: list[int] | None = None,
        activity_stagger_minutes: int = 15,
    ) -> None:
        phases = activity_phases or [0] * account_count
        self._accounts: list[AccountUsage] = [
            AccountUsage(
                account_index=i,
                idle_window=idle_window_hours if idle_window_hours is not None else DEFAULT_IDLE_WINDOW_HOURS,
                activity_phase_minutes=phases[i] if i < len(phases) else 0,
                local_offset_hours=local_offset_hours,
                daily_reset_at=_next_midnight_local(time.time(), local_offset_hours),
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
        self.activity_stagger_minutes = activity_stagger_minutes

    def _strict_eligible(self, acc: AccountUsage, now: float) -> bool:
        if acc.at_hard_daily_cap(self.daily_msg_cap):
            return False
        if acc.in_idle_window(now):
            return False
        if not acc.in_activity_phase(now, self.activity_stagger_minutes):
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

        Returns -1 only when ALL accounts hit hard daily cap (caller may
        delay/retry). Never returns -1 for soft-cap-only — picks least-loaded
        instead of forcing blind round-robin bypass.
        """
        now = time.time()
        async with self._lock:
            not_hard_capped = [
                (i, acc) for i, acc in enumerate(self._accounts)
                if not acc.at_hard_daily_cap(self.daily_msg_cap)
            ]
            if not not_hard_capped:
                logger.warning(
                    "rate_governor: all %s accounts at hard daily cap",
                    len(self._accounts),
                )
                return -1

            strict = [(i, acc) for i, acc in not_hard_capped if self._strict_eligible(acc, now)]
            pool = strict if strict else not_hard_capped

            healthy = [(i, acc) for i, acc in pool if not self._approaching_soft_cap(acc, now)]
            candidates = healthy if healthy else pool

            if not strict and strict != not_hard_capped:
                logger.debug(
                    "rate_governor: no strict-eligible account — using soft fallback "
                    "(idle/phase/soft-cap bypass)"
                )

            return min(candidates, key=lambda x: x[1].msgs_24h(now))[0]

    async def record_request(self, account_index: int) -> None:
        now = time.time()
        async with self._lock:
            if 0 <= account_index < len(self._accounts):
                self._accounts[account_index].record(now)

    async def jitter_delay(self) -> None:
        delay_ms = random.randint(self.min_jitter_ms, self.max_jitter_ms)
        await asyncio.sleep(delay_ms / 1000.0)

    async def backoff_when_exhausted(self) -> None:
        """Brief pause when all accounts at hard cap — avoids hot retry loops."""
        await asyncio.sleep(random.uniform(2.0, 8.0))

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "daily_msg_cap": self.daily_msg_cap,
            "soft_msg_cap": self.soft_msg_cap,
            "soft_hours_cap": self.soft_hours_cap,
            "jitter_ms": [self.min_jitter_ms, self.max_jitter_ms],
            "local_offset_hours": self.local_offset_hours,
            "activity_stagger_minutes": self.activity_stagger_minutes,
            "accounts": [
                {**acc.snapshot(now), "daily_cap": self.daily_msg_cap}
                for acc in self._accounts
            ],
        }

    async def reset_account(self, account_index: int) -> None:
        async with self._lock:
            if 0 <= account_index < len(self._accounts):
                old = self._accounts[account_index]
                self._accounts[account_index] = AccountUsage(
                    account_index=account_index,
                    idle_window=old.idle_window,
                    activity_phase_minutes=old.activity_phase_minutes,
                    local_offset_hours=old.local_offset_hours,
                    daily_reset_at=_next_midnight_local(time.time(), old.local_offset_hours),
                )

    async def aclose(self) -> None:
        pass
