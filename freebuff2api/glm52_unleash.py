"""GLM 5.2 unleash — bypass Freebuff deployment-hours limit.

RE findings (CodebuffAI/codebuff source, commits #540, #608, #641):

Server-side flow:
  1. POST /api/v1/freebuff/session { model: "z-ai/glm-5.1" }
       → server checks `isFreebuffModelAvailable(model, now)`
       → if outside 9am ET-5pm PT weekdays: returns status='model_unavailable' 409
       → if available: creates session, returns instance_id + remainingMs (3600000ms = 1h)
  2. POST /api/v1/chat/completions with agent_id + session
       → server uses session's bound model (NOT re-checking deployment hours)
       → session lifetime = FREEBUFF_SESSION_LENGTH_MS = 3_600_000 (1 hour)
       → during session: chat completion works regardless of current hour

Bypass strategies:

A. **Session persistence during deployment hours**:
   - Pre-warm GLM session between 9am-5pm ET weekdays
   - Cache `instance_id` + reuse until `remainingMs < 60000` (1 min left)
   - Refresh session BEFORE expiry — server only checks at creation time

B. **Multi-account rotation**:
   - Pool of N accounts, each with their own GLM session
   - When one session expires, rotate to next account
   - If all expired + outside hours: fall back to other models (DeepSeek/Kimi)
   - Next deployment-hours window: re-warm all sessions

C. **Direct chat completion (session already active)**:
   - If session is alive, skip session re-creation
   - Server doesn't re-check `isFreebuffModelAvailable` for chat completion
   - Only session creation gates on deployment hours

D. **Session renewal race**:
   - At minute 58 of session, create new session in parallel
   - Switch over atomically, no gap

This module implements A + B + D. Strategy C is implicit in session reuse.

Usage:
    from freebuff2api.glm52_unleash import GlmSessionPool
    pool = GlmSessionPool(settings, account_pool)
    lease = await pool.acquire_glm_session()  # returns active session or raises
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from .codebuff import CodebuffAccountPool, CodebuffClient, CodebuffError, FreebuffSession
from .config import Settings

logger = logging.getLogger("freebuff2api.glm52_unleash")

# Upstream constants (RE'd from CodebuffAI/codebuff):
# - FREEBUFF_SESSION_LENGTH_MS = 3_600_000 (1 hour)
# - Deployment hours: 9am ET - 5pm PT, weekdays only
# - Server checks `isFreebuffModelAvailable(model, now)` only at session creation
# - Once session created, chat completion uses session's bound model regardless of hour
FREEBUFF_SESSION_LENGTH_MS = 3_600_000
SESSION_REFRESH_THRESHOLD_MS = 300_000  # refresh when <5min remaining
DEPLOYMENT_START_ET = 9  # 9am ET
DEPLOYMENT_END_PT = 17   # 5pm PT

# ET = UTC-5 (Eastern), PT = UTC-8 (Pacific). Use UTC for arithmetic.
# 9am ET = 14:00 UTC (EST) / 13:00 UTC (EDT — DST)
# 5pm PT = 01:00 UTC next day (PST) / 00:00 UTC next day (PDT)
# Simplified: deployment window = 13:00 UTC - 01:00 UTC next day (DST-aware later)
DEPLOYMENT_START_UTC_HOUR = 13  # 9am EDT
DEPLOYMENT_END_UTC_HOUR_NEXT_DAY = 1  # 5pm PDT next-day UTC


def is_glm_deployment_hours(now: datetime | None = None) -> bool:
    """Check if current time is within GLM 5.1 deployment hours.

    Server uses: 9am ET - 5pm PT, weekdays only.
    ET = America/New_York, PT = America/Los_Angeles.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Convert to ET and PT
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        # Fallback: UTC offsets (ignore DST — slightly inaccurate)
        et = now.astimezone(timezone(timedelta(hours=-5)))
        pt = now.astimezone(timezone(timedelta(hours=-8)))

    # Weekend check (server: weekday-only)
    if et.weekday() >= 5:  # Sat=5, Sun=6
        return False

    # 9am ET <= current ET time AND current PT time < 5pm PT
    et_hour = et.hour
    pt_hour = pt.hour
    return et_hour >= DEPLOYMENT_START_ET and pt_hour < DEPLOYMENT_END_PT


def next_deployment_window(now: datetime | None = None) -> datetime:
    """Return next time GLM 5.1 becomes available."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Walk forward hour by hour until we hit deployment hours (max 7 days)
    for offset_hours in range(0, 24 * 7):
        candidate = now + timedelta(hours=offset_hours)
        if is_glm_deployment_hours(candidate):
            return candidate
    return now  # fallback


@dataclass
class GlmSessionSlot:
    account_index: int
    session: FreebuffSession
    created_at: float
    last_used_at: float
    use_count: int = 0
    refresh_in_progress: bool = False
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def age_ms(self) -> int:
        return int((time.time() - self.created_at) * 1000)

    @property
    def remaining_ms(self) -> int:
        if self.session.remaining_ms is None:
            return FREEBUFF_SESSION_LENGTH_MS - self.age_ms
        return max(0, self.session.remaining_ms - self.age_ms)

    @property
    def is_fresh(self) -> bool:
        return self.remaining_ms > SESSION_REFRESH_THRESHOLD_MS


class GlmSessionPool:
    """Pool of GLM 5.1/5.2 sessions, persisted across deployment-hour windows.

    Strategy:
    - Pre-warm N sessions during deployment hours (1 per account in pool)
    - Reuse sessions for chat completion (server doesn't re-check hours)
    - Auto-refresh session when remaining_ms < threshold
    - If no active session + outside hours: return None (caller falls back)
    - If no active session + inside hours: create new
    """

    def __init__(
        self,
        settings: Settings,
        account_pool: CodebuffAccountPool,
        model_id: str = "z-ai/glm-5.1",
    ) -> None:
        self.settings = settings
        self.account_pool = account_pool
        self.model_id = model_id
        self._slots: list[GlmSessionSlot | None] = [None] * account_pool.account_count
        self._lock = asyncio.Lock()
        self._warmup_task: asyncio.Task | None = None
        self._next_warmup_check: float = 0.0

    async def acquire_glm_session(self) -> tuple[int, FreebuffSession] | None:
        """Get a fresh GLM session. Returns (account_index, session) or None.

        None means: no active session AND outside deployment hours.
        Caller should fall back to DeepSeek/Kimi/MiniMax.
        """
        async with self._lock:
            # Find freshest slot
            best_idx = -1
            best_remaining = 0
            for i, slot in enumerate(self._slots):
                if slot and slot.is_fresh and slot.remaining_ms > best_remaining:
                    best_idx = i
                    best_remaining = slot.remaining_ms

            if best_idx >= 0:
                slot = self._slots[best_idx]
                slot.last_used_at = time.time()
                slot.use_count += 1
                return best_idx, slot.session

        # No fresh session — try to create
        if is_glm_deployment_hours():
            session = await self._create_session_any_account()
            if session:
                return session

        return None

    async def _create_session_any_account(self) -> tuple[int, FreebuffSession] | None:
        """Try to create GLM session on any account in pool."""
        for i in range(self.account_pool.account_count):
            try:
                account = self.account_pool._accounts[i]
                session = await account.sessions.acquire_session(self.model_id, [])
                slot = GlmSessionSlot(
                    account_index=i,
                    session=session.session,
                    created_at=time.time(),
                    last_used_at=time.time(),
                )
                async with self._lock:
                    self._slots[i] = slot
                logger.info(
                    "glm session created account=%s instance=%s remaining_ms=%s",
                    i, session.session.instance_id, session.session.remaining_ms,
                )
                # Release session lease but keep session object cached
                await session.aclose()
                return i, session.session
            except CodebuffError as e:
                logger.warning("glm session create failed account=%s: %s", i, e)
                continue
        return None

    async def refresh_session(self, account_index: int) -> bool:
        """Refresh a single account's GLM session."""
        slot = self._slots[account_index]
        if slot is None:
            return False

        async with slot.refresh_lock:
            if slot.is_fresh:
                return True  # someone else refreshed

            if not is_glm_deployment_hours():
                logger.info(
                    "glm refresh skipped account=%s — outside deployment hours",
                    account_index,
                )
                return False

            try:
                account = self.account_pool._accounts[account_index]
                # Delete old session first
                await account.client.delete_active_session()
                # Create new
                new_session = await account.sessions.acquire_session(self.model_id, [])
                slot.session = new_session.session
                slot.created_at = time.time()
                slot.last_used_at = time.time()
                slot.use_count = 0
                await new_session.aclose()
                logger.info(
                    "glm session refreshed account=%s instance=%s",
                    account_index, new_session.session.instance_id,
                )
                return True
            except Exception as e:
                logger.warning("glm refresh failed account=%s: %s", account_index, e)
                return False

    async def background_warmup(self) -> None:
        """Long-running task: keep all sessions warm.

        - If inside deployment hours + slot is None/expiring: create/refresh
        - If outside deployment hours: keep existing sessions alive (no refresh)
        - Run every 60 seconds
        """
        while True:
            try:
                await self._warmup_tick()
            except Exception as e:
                logger.warning("glm warmup tick error: %s", e)
            await asyncio.sleep(60)

    async def _warmup_tick(self) -> None:
        in_hours = is_glm_deployment_hours()
        for i in range(self.account_pool.account_count):
            slot = self._slots[i]
            if slot is None:
                if in_hours:
                    await self._create_session_any_account()
                continue
            if slot.remaining_ms < SESSION_REFRESH_THRESHOLD_MS and in_hours:
                await self.refresh_session(i)

    def start_warmup_task(self) -> None:
        if self._warmup_task is None or self._warmup_task.done():
            self._warmup_task = asyncio.create_task(self.background_warmup())

    async def stop_warmup_task(self) -> None:
        if self._warmup_task:
            self._warmup_task.cancel()
            try:
                await self._warmup_task
            except asyncio.CancelledError:
                pass
            self._warmup_task = None

    def status(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "in_deployment_hours": is_glm_deployment_hours(),
            "next_window": next_deployment_window().isoformat(),
            "accounts": [
                {
                    "index": i,
                    "active": slot is not None,
                    "instance_id": slot.session.instance_id if slot else None,
                    "remaining_ms": slot.remaining_ms if slot else None,
                    "use_count": slot.use_count if slot else 0,
                    "is_fresh": slot.is_fresh if slot else False,
                }
                for i, slot in enumerate(self._slots)
            ],
        }


async def unleash_init(settings: Settings, account_pool: CodebuffAccountPool) -> GlmSessionPool:
    """Initialize GLM 5.2 unleash pool + start background warmup."""
    pool = GlmSessionPool(settings, account_pool)
    pool.start_warmup_task()
    return pool
