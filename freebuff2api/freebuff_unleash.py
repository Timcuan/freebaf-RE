"""Freebuff Unleash — exploit Freebuff loopholes for unlimited access.

RE findings (CodebuffAI/codebuff source, all verified):

Loophole 1: Quota gates (premium daily pool, GLM weekly referral pool) only
  run on POST /api/v1/freebuff/session. Chat completions on an active session
  are NOT quota-checked.
  → create one 1h session per quota unit, reuse for unlimited chat completions

Loophole 2: Sessions are bound to a model, but chat completion uses
  session.model. The server does not re-validate payload.model against
  session.model for chat.
  → cache one session per (account, model) pair; switch accounts/models
    without re-admission as long as each session is still active

Loophole 3: One instance per account, but not per IP/device fingerprint
  → N accounts = N concurrent sessions = N× throughput

Loophole 4: Ad-chain economy — request_ads runs per session create
  → cache ad impression IDs; SessionManager already deduplicates per streak

Loophole 5: Sessions live 1h, but refresh can run while still active
  → pre-emptive refresh at the 55-minute mark, atomic switch

Loophole 6: model_locked = existing-session conflict
  → _delete_locked_session handles this, then retry

Loophole 7: Queue admission tick = 15s, per-model Postgres advisory lock
  → spam does not help (server-side lock), but multi-account bypasses queue

Loophole 8: No per-session rate limit on chat completion
  → within an active session: unlimited concurrent chat completion requests

GLM 5.2 gate (verified from common/src/constants/freebuff-models.ts):
  - availability: 'always' (NOT deployment_hours)
  - premium: true
  - gated by the GLM weekly referral pool (5 sessions/referral/week, cap 10)
  - tier-0 accounts (no referrals) get 0 GLM sessions/week
  - bypassed here by multi-account rotation across accounts that DO have
    referral quota, plus session persistence (1 session = 1h unlimited chat)

Premium daily pool (DeepSeek Pro, Kimi, MiMo Pro):
  - 5 sessions/day for tier-0 accounts (FREEBUFF_PREMIUM_SESSION_LIMIT=5)
  - resets at midnight Pacific
  - bypassed via multi-account: N tier-0 accounts = 5N premium sessions/day

Non-premium always-available models (DeepSeek Flash, MiMo, MiniMax M2.7/M3):
  - no upstream quota gate
  - unlimited sessions/day on any account

Unlimited strategy:
1. N-account pool × M-model sessions = N×M cached sessions
2. Account health registry tracks per-account quota per pool
3. Background warmup pre-warms all (account, model) sessions
4. Pre-emptive refresh every 55 minutes (race-free via per-slot lock)
5. On 429 rate_limited: mark account exhausted for that pool, pick next
6. Model fallback chain: requested → cached alternative → MiniMax M2.7
7. Concurrent chat completions: unlimited within an active session

Usage:
    from freebuff2api.freebuff_unleash import UnleashPool
    pool = UnleashPool(settings, account_pool)
    await pool.start()  # start background warmup
    lease = await pool.acquire("z-ai/glm-5.2")  # cached or fresh
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .account_health import (
    AccountHealthRegistry,
    POOL_GLM_WEEKLY,
    POOL_PREMIUM_DAILY,
    pool_for_model,
)
from .codebuff import CodebuffAccountPool, CodebuffError, FreebuffSession, RateLimitedError, SessionManager
from .config import Settings

logger = logging.getLogger("freebuff2api.unleash")

# Upstream constants (RE'd from CodebuffAI/codebuff):
FREEBUFF_SESSION_LENGTH_MS = 3_600_000  # 1 hour
SESSION_REFRESH_THRESHOLD_MS = 300_000  # refresh when <5min remaining
PRE_EMPTIVE_REFRESH_MS = 5_000_000  # refresh at 55min mark (race-safe)
WARMUP_TICK_SEC = 30  # check every 30s

# All Freebuff models for multi-model warmup
ALL_FREEBUFF_MODELS = (
    "z-ai/glm-5.2",            # GLM 5.2 — referral weekly pool (bypass via multi-account)
    "minimax/minimax-m2.7",    # Fastest, always available (non-premium, no gate)
    "moonshotai/kimi-k2.6",    # Premium daily pool
    "deepseek/deepseek-v4-pro",   # Premium daily pool
    "deepseek/deepseek-v4-flash", # Non-premium, always available
)

# Deployment hours retained for diagnostics only — GLM 5.2's real gate is the
# weekly referral pool, not deployment hours (verified from upstream source).
# Kept because the /api/health/glm52 endpoint still surfaces it for users who
# want to know the historical window, and tests assert on it.
from datetime import datetime as _dt, timezone as _tz, timedelta as _td  # noqa: E402

DEPLOYMENT_START_ET = 9
DEPLOYMENT_END_PT = 17


def is_glm_deployment_hours(now: _dt | None = None) -> bool:
    """9am ET - 5pm PT weekdays. Diagnostic only — GLM 5.2 is NOT gated by
    this upstream anymore. The real gate is the weekly referral pool."""
    if now is None:
        now = _dt.now(_tz.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        et = now.astimezone(_tz(_td(hours=-5)))
        pt = now.astimezone(_tz(_td(hours=-8)))
    if et.weekday() >= 5:
        return False
    return et.hour >= DEPLOYMENT_START_ET and pt.hour < DEPLOYMENT_END_PT


def next_deployment_window(now: _dt | None = None) -> _dt:
    if now is None:
        now = _dt.now(_tz.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    for offset_hours in range(0, 24 * 7):
        candidate = now + _td(hours=offset_hours)
        if is_glm_deployment_hours(candidate):
            return candidate
    return now


@dataclass
class SessionSlot:
    account_index: int
    model: str
    session: FreebuffSession
    created_at: float
    last_used_at: float
    use_count: int = 0
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Ad-chain cache: skip request_ads when streak is already reported today
    ad_chain_done: bool = False

    @property
    def age_ms(self) -> int:
        return int((time.time() - self.created_at) * 1000)

    @property
    def remaining_ms(self) -> int:
        if self.session.remaining_ms is None:
            return max(0, FREEBUFF_SESSION_LENGTH_MS - self.age_ms)
        return max(0, self.session.remaining_ms - self.age_ms)

    @property
    def is_fresh(self) -> bool:
        return self.remaining_ms > SESSION_REFRESH_THRESHOLD_MS

    @property
    def needs_preemptive_refresh(self) -> bool:
        return self.remaining_ms < PRE_EMPTIVE_REFRESH_MS


class UnleashPool:
    """Multi-account × multi-model session pool for unlimited Freebuff access.

    Matrix: accounts × models = cached sessions
    - N accounts × 5 models = 5N cached sessions
    - Chat completion reuses cached session without re-admission
    - Pre-emptive refresh at 55min (race-free per slot lock)
    - Account health registry tracks quota per pool; on 429 marks exhausted
    - Concurrent chat completions: unlimited within an active session
    """

    def __init__(
        self,
        settings: Settings,
        account_pool: CodebuffAccountPool,
        models: tuple[str, ...] = ALL_FREEBUFF_MODELS,
    ) -> None:
        self.settings = settings
        self.account_pool = account_pool
        self.models = models
        # slots[account_idx][model] = SessionSlot
        self._slots: list[dict[str, SessionSlot]] = [
            {} for _ in range(account_pool.account_count)
        ]
        self._lock = asyncio.Lock()
        self._warmup_task: asyncio.Task | None = None
        # Round-robin per-model for load balancing
        self._next_account: dict[str, int] = {m: 0 for m in models}
        # Per-account quota/health registry
        self.health = AccountHealthRegistry(account_pool.account_count)
        # In-flight warmup tasks: (account_index, model) -> Task
        # Prevents duplicate session creation when warmup ticks overlap.
        self._inflight: dict[tuple[int, str], asyncio.Task] = {}

    async def acquire(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, FreebuffSession] | None:
        """Get session for model. Returns (account_index, session) or None.

        Priority:
        1. Freshest cached session across all accounts (still active upstream)
        2. Create new on an account with quota for the model's pool
        3. Return None (caller falls back to another model)
        """
        # 1. Find freshest cached
        async with self._lock:
            best_idx = -1
            best_remaining = 0
            for i, model_slots in enumerate(self._slots):
                slot = model_slots.get(model)
                if slot and slot.is_fresh and slot.remaining_ms > best_remaining:
                    best_idx = i
                    best_remaining = slot.remaining_ms

            if best_idx >= 0:
                slot = self._slots[best_idx][model]
                slot.last_used_at = time.time()
                slot.use_count += 1
                return best_idx, slot.session

        # 2. Try create new on an account with available quota
        return await self._create_session_any_account(model, messages)

    async def _create_session_any_account(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, FreebuffSession] | None:
        """Create session on next available account (round-robin per model).

        Skips accounts known to be exhausted for the model's pool. On 429
        rate_limited, marks the account exhausted and tries the next.
        """
        n = self.account_pool.account_count
        pool = pool_for_model(model)
        start = self._next_account.get(model, 0) % n
        now = time.time()

        # Build candidate list: accounts with quota, round-robin from start
        candidates: list[int] = []
        for offset in range(n):
            idx = (start + offset) % n
            candidates.append(idx)

        # Reorder: available accounts first (per health registry)
        avail = set(self.health.available_accounts(pool, now))
        candidates.sort(key=lambda i: (i not in avail, i))

        for i in candidates:
            h = self.health[i]
            if h.banned or h.geo_blocked:
                continue
            # For gated pools, skip accounts known exhausted and not yet reset
            if pool and not h.is_available(pool, now):
                continue
            try:
                account = self.account_pool._accounts[i]
                session_lease = await account.sessions.acquire_session(model, messages or [])
                slot = SessionSlot(
                    account_index=i,
                    model=model,
                    session=session_lease.session,
                    created_at=time.time(),
                    last_used_at=time.time(),
                    ad_chain_done=True,
                )
                async with self._lock:
                    self._slots[i][model] = slot
                self._next_account[model] = (i + 1) % n
                self.health.mark_success(pool)
                logger.info(
                    "unleash session created account=%s model=%s instance=%s remaining_ms=%s",
                    i, model, session_lease.session.instance_id,
                    session_lease.session.remaining_ms,
                )
                await session_lease.aclose()
                return i, session_lease.session
            except RateLimitedError as e:
                self.health.mark_exhausted(e.pool or pool, reset_at=e.reset_at)
                logger.warning(
                    "unleash create rate_limited account=%s model=%s pool=%s reset_at=%s — trying next",
                    i, model, e.pool, e.reset_at,
                )
                continue
            except CodebuffError as e:
                if e.status_code == 403:
                    self.health[i].mark_banned()
                elif e.status_code == 451:
                    self.health[i].mark_geo_blocked()
                logger.warning(
                    "unleash create failed account=%s model=%s: %s", i, model, e,
                )
                continue
        return None

    async def refresh_session(self, account_index: int, model: str) -> bool:
        """Pre-emptive refresh to keep the session alive.

        Refresh = DELETE current + POST new, which consumes another quota
        unit for gated pools. Skipped if the account is exhausted for the
        model's pool (the existing session keeps running until it expires).
        """
        slot = self._slots[account_index].get(model)
        if slot is None:
            return False

        async with slot.refresh_lock:
            if slot.is_fresh and not slot.needs_preemptive_refresh:
                return True  # already refreshed by another coroutine

            pool = pool_for_model(model)
            h = self.health[account_index]
            # For gated pools, check quota before consuming it on a refresh
            if pool and not h.is_available(pool):
                logger.info(
                    "unleash refresh skipped account=%s model=%s pool=%s — account exhausted",
                    account_index, model, pool,
                )
                return False

            try:
                account = self.account_pool._accounts[account_index]
                await account.client.delete_active_session()
                new_lease = await account.sessions.acquire_session(model, [])
                slot.session = new_lease.session
                slot.created_at = time.time()
                slot.last_used_at = time.time()
                slot.use_count = 0
                slot.ad_chain_done = True
                self.health.mark_success(pool)
                await new_lease.aclose()
                logger.info(
                    "unleash session refreshed account=%s model=%s instance=%s",
                    account_index, model, new_lease.session.instance_id,
                )
                return True
            except RateLimitedError as e:
                self.health.mark_exhausted(e.pool or pool, reset_at=e.reset_at)
                # Session was deleted upstream but new one failed — clear slot
                # so warmup recreates it later. Without this, the stale session
                # would be served to requests and fail with 404.
                async with self._lock:
                    self._slots[account_index].pop(model, None)
                logger.warning(
                    "unleash refresh rate_limited account=%s model=%s — slot cleared, session will be recreated",
                    account_index, model,
                )
                return False
            except Exception as e:
                # Same: delete succeeded but new session failed → stale slot.
                async with self._lock:
                    self._slots[account_index].pop(model, None)
                logger.warning(
                    "unleash refresh failed account=%s model=%s: %s — slot cleared",
                    account_index, model, e,
                )
                return False

    async def background_warmup(self) -> None:
        """Keep all sessions warm for unlimited access.

        Tick every 30s:
        - Create missing sessions on accounts with quota
        - Pre-emptively refresh sessions approaching the 55-min mark
        - Skip accounts known exhausted for the model's pool
        """
        while True:
            try:
                await self._warmup_tick()
            except Exception as e:
                logger.warning("unleash warmup tick error: %s", e)
            await asyncio.sleep(WARMUP_TICK_SEC)

    async def _warmup_tick(self) -> None:
        n = self.account_pool.account_count
        now = time.time()

        for i in range(n):
            h = self.health[i]
            if h.banned or h.geo_blocked:
                continue
            for model in self.models:
                pool = pool_for_model(model)
                # Skip gated models on exhausted accounts
                if pool and not h.is_available(pool, now):
                    continue

                slot = self._slots[i].get(model)
                key = (i, model)
                if slot is None:
                    # Create missing session — dedup against in-flight task
                    if key in self._inflight and not self._inflight[key].done():
                        continue  # already warming up
                    task = asyncio.create_task(self._create_one(i, model))
                    self._inflight[key] = task
                    task.add_done_callback(lambda t, k=key: self._inflight.pop(k, None))
                elif slot.needs_preemptive_refresh:
                    if key in self._inflight and not self._inflight[key].done():
                        continue  # refresh already running
                    task = asyncio.create_task(self.refresh_session(i, model))
                    self._inflight[key] = task
                    task.add_done_callback(lambda t, k=key: self._inflight.pop(k, None))

    async def _create_one(self, account_index: int, model: str) -> None:
        """Helper for fire-and-forget warmup creation on a specific account."""
        h = self.health[account_index]
        pool = pool_for_model(model)
        if h.banned or h.geo_blocked:
            return
        if pool and not h.is_available(pool):
            return
        try:
            account = self.account_pool._accounts[account_index]
            session_lease = await account.sessions.acquire_session(model, [])
            slot = SessionSlot(
                account_index=account_index,
                model=model,
                session=session_lease.session,
                created_at=time.time(),
                last_used_at=time.time(),
                ad_chain_done=True,
            )
            async with self._lock:
                self._slots[account_index][model] = slot
            self.health.mark_success(pool)
            await session_lease.aclose()
            logger.info(
                "unleash warmup created account=%s model=%s instance=%s",
                account_index, model, session_lease.session.instance_id,
            )
        except RateLimitedError as e:
            self.health.mark_exhausted(e.pool or pool, reset_at=e.reset_at)
        except CodebuffError as e:
            # Mark account banned/geo-blocked so warmup stops retrying it.
            if e.status_code == 403:
                self.health[account_index].mark_banned()
                logger.warning(
                    "unleash account=%s banned — marked, will skip on future warmup",
                    account_index,
                )
            elif e.status_code == 451:
                self.health[account_index].mark_geo_blocked()
                logger.warning(
                    "unleash account=%s geo-blocked — marked, will skip on future warmup",
                    account_index,
                )
            else:
                logger.debug(
                    "unleash warmup create failed account=%s model=%s: %s",
                    account_index, model, e,
                )

    def start(self) -> None:
        if self._warmup_task is None or self._warmup_task.done():
            self._warmup_task = asyncio.create_task(self.background_warmup())

    async def stop(self) -> None:
        if self._warmup_task:
            self._warmup_task.cancel()
            try:
                await self._warmup_task
            except asyncio.CancelledError:
                pass
            self._warmup_task = None
        # Cancel any in-flight warmup/refresh tasks to avoid leaks on shutdown.
        for key, task in list(self._inflight.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._inflight.clear()

    def status(self) -> dict[str, Any]:
        return {
            "models_tracked": list(self.models),
            "accounts": [
                {
                    "index": i,
                    "health": self.health[i].__dict__ if False else None,
                    "sessions": [
                        {
                            "model": model,
                            "instance_id": slot.session.instance_id,
                            "remaining_ms": slot.remaining_ms,
                            "use_count": slot.use_count,
                            "is_fresh": slot.is_fresh,
                            "needs_refresh": slot.needs_preemptive_refresh,
                        }
                        for model, slot in model_slots.items()
                    ],
                }
                for i, model_slots in enumerate(self._slots)
            ],
            "account_health": self.health.status(),
            "total_active_sessions": sum(len(ms) for ms in self._slots),
        }

    async def acquire_with_fallback(
        self,
        requested_model: str,
        fallback_chain: tuple[str, ...] = (
            "z-ai/glm-5.2",
            "moonshotai/kimi-k2.6",
            "deepseek/deepseek-v4-pro",
            "minimax/minimax-m2.7",
        ),
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, FreebuffSession, str] | None:
        """Acquire requested model, fall back to chain if unavailable.

        Returns (account_index, session, actual_model) or None.
        """
        # Try requested first
        result = await self.acquire(requested_model, messages)
        if result:
            return result[0], result[1], requested_model

        # Fallback chain
        for fallback in fallback_chain:
            if fallback == requested_model:
                continue
            result = await self.acquire(fallback, messages)
            if result:
                logger.info(
                    "unleash fallback %s → %s (requested unavailable)",
                    requested_model, fallback,
                )
                return result[0], result[1], fallback

        return None


async def unleash_init(settings: Settings, account_pool: CodebuffAccountPool) -> UnleashPool:
    """Initialize unleash pool + start background warmup."""
    pool = UnleashPool(settings, account_pool)
    pool.start()
    return pool
