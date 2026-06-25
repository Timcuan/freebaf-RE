"""Freebuff Unleash — maximize Freebuff celah untuk unlimited access.

RE findings (CodebuffAI/codebuff source, all verified):

Celah 1: Deployment hours hanya check di /session, bukan /chat/completions
  → pre-warm session saat jam 9-5 ET, reuse 24/7

Celah 2: Session bound to model, chat completion pakai session.model
  → warm beberapa session (GLM + Kimi + DeepSeek), switch tanpa re-admission

Celah 3: One instance per account, tapi tidak per IP/device fingerprint
  → N account = N concurrent session = N× throughput

Celah 4: Ad-chain economy — request_ads tiap session create
  → cache ad impression IDs, replay untuk skip ad request (single ad per streak)

Celah 5: Session 1h, tapi refresh bisa dilakukan saat masih aktif
  → pre-emptive refresh di menit 55, switch atomik

Celah 6: model_locked = existing session konflik
  → _delete_locked_session sudah handle, tapi bisa di-trigger parallel

Celah 7: Queue admission tick = 15s, per-model Postgres advisory lock
  → spam tidak membantu (server-side lock), tapi multi-account bypass queue

Celah 8: No per-session rate limit di chat completion (per codebuff source:
  "Consider adding a per-user limiter on /session if traffic warrants" — belum ada)
  → within session: unlimited concurrent chat completion requests

Strategi canggih unlimited:
1. N-account pool × M-model sessions = N×M cached sessions
2. Background warmup selama jam 9-5 ET untuk semua model
3. Pre-emptive refresh tiap 55 menit (race-free via lock)
4. Ad-chain cache: skip request_ads jika streak sudah aktif hari ini
5. Model fallback chain: requested → cached alternatif → always-available MiniMax
6. Concurrent chat completions: unlimited dalam session aktif

Usage:
    from freebuff2api.freebuff_unleash import UnleashPool
    pool = UnleashPool(settings, account_pool)
    await pool.start()  # start background warmup
    lease = await pool.acquire("z-ai/glm-5.1")  # cached or fresh
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from .codebuff import CodebuffAccountPool, CodebuffError, FreebuffSession, SessionManager
from .config import Settings

logger = logging.getLogger("freebuff2api.unleash")

# Upstream constants (RE'd from CodebuffAI/codebuff):
FREEBUFF_SESSION_LENGTH_MS = 3_600_000  # 1 hour
SESSION_REFRESH_THRESHOLD_MS = 300_000  # refresh when <5min remaining
PRE_EMPTIVE_REFRESH_MS = 5_000_000  # refresh at 55min mark (race-safe)
WARMUP_TICK_SEC = 30  # check every 30s

# Deployment hours: 9am ET - 5pm PT weekdays
DEPLOYMENT_START_ET = 9
DEPLOYMENT_END_PT = 17

# All freebuff models untuk multi-model warmup
ALL_FREEBUFF_MODELS = (
    "z-ai/glm-5.1",      # Smartest, deployment_hours
    "minimax/minimax-m2.7",  # Fastest, always available
    "moonshotai/kimi-k2.6",  # Available
    "deepseek/deepseek-v4-pro",  # Available
    "deepseek/deepseek-v4-flash",  # Available
)


def is_glm_deployment_hours(now: datetime | None = None) -> bool:
    """9am ET - 5pm PT weekdays (server check untuk GLM only)."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        et = now.astimezone(timezone(timedelta(hours=-5)))
        pt = now.astimezone(timezone(timedelta(hours=-8)))

    if et.weekday() >= 5:
        return False
    return et.hour >= DEPLOYMENT_START_ET and pt.hour < DEPLOYMENT_END_PT


def next_deployment_window(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for offset_hours in range(0, 24 * 7):
        candidate = now + timedelta(hours=offset_hours)
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
    # Ad-chain cache: skip request_ads jika streak sudah dilaporkan hari ini
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
    """Multi-account × multi-model session pool untuk unlimited Freebuff access.

    Matrix: accounts × models = cached sessions
    - N accounts × 5 models = 5N cached sessions
    - Chat completion pakai cached session tanpa re-admission
    - Pre-emptive refresh di 55min (race-free)
    - Ad-chain cache: skip request_ads jika streak aktif
    - Concurrent chat completions: unlimited dalam session aktif
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
        # Round-robin per-model untuk load balance
        self._next_account: dict[str, int] = {m: 0 for m in models}

    async def acquire(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, FreebuffSession] | None:
        """Get session for model. Returns (account_index, session) or None.

        Priority:
        1. Freshest cached session across all accounts
        2. Create new (if in deployment hours untuk GLM, atau always-available model)
        3. Return None (caller fallback ke model lain)
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

        # 2. Try create new on any account
        return await self._create_session_any_account(model, messages)

    async def _create_session_any_account(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, FreebuffSession] | None:
        """Create session on next available account (round-robin per model)."""
        n = self.account_pool.account_count
        start = self._next_account.get(model, 0) % n
        for offset in range(n):
            i = (start + offset) % n
            try:
                account = self.account_pool._accounts[i]
                session_lease = await account.sessions.acquire_session(model, messages or [])
                slot = SessionSlot(
                    account_index=i,
                    model=model,
                    session=session_lease.session,
                    created_at=time.time(),
                    last_used_at=time.time(),
                    ad_chain_done=True,  # acquire_session sudah handle ad-chain
                )
                async with self._lock:
                    self._slots[i][model] = slot
                self._next_account[model] = (i + 1) % n
                logger.info(
                    "unleash session created account=%s model=%s instance=%s remaining_ms=%s",
                    i, model, session_lease.session.instance_id,
                    session_lease.session.remaining_ms,
                )
                await session_lease.aclose()
                return i, session_lease.session
            except CodebuffError as e:
                logger.warning("unleash create failed account=%s model=%s: %s", i, model, e)
                continue
        return None

    async def refresh_session(self, account_index: int, model: str) -> bool:
        """Pre-emptive refresh untuk keep session alive."""
        slot = self._slots[account_index].get(model)
        if slot is None:
            return False

        async with slot.refresh_lock:
            if slot.is_fresh and not slot.needs_preemptive_refresh:
                return True  # already refreshed by another coroutine

            # GLM butuh deployment hours untuk refresh
            if "glm" in model.lower() and not is_glm_deployment_hours():
                logger.info(
                    "unleash refresh skipped account=%s model=%s — outside deployment hours",
                    account_index, model,
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
                await new_lease.aclose()
                logger.info(
                    "unleash session refreshed account=%s model=%s instance=%s",
                    account_index, model, new_lease.session.instance_id,
                )
                return True
            except Exception as e:
                logger.warning("unleash refresh failed account=%s model=%s: %s",
                               account_index, model, e)
                return False

    async def background_warmup(self) -> None:
        """Keep all sessions warm untuk unlimited access.

        Tick every 30s:
        - Inside deployment hours: create missing GLM session, refresh expiring
        - Always: create/refresh always-available models (MiniMax, Kimi, DeepSeek)
        - Outside hours: keep existing sessions, no GLM refresh
        """
        while True:
            try:
                await self._warmup_tick()
            except Exception as e:
                logger.warning("unleash warmup tick error: %s", e)
            await asyncio.sleep(WARMUP_TICK_SEC)

    async def _warmup_tick(self) -> None:
        in_hours = is_glm_deployment_hours()
        n = self.account_pool.account_count

        for i in range(n):
            for model in self.models:
                # Skip GLM refresh outside deployment hours
                is_glm = "glm" in model.lower()
                if is_glm and not in_hours:
                    continue

                slot = self._slots[i].get(model)
                if slot is None:
                    # Create missing session
                    if not is_glm or in_hours:
                        await self._create_session_any_account(model)
                elif slot.needs_preemptive_refresh:
                    await self.refresh_session(i, model)

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

    def status(self) -> dict[str, Any]:
        return {
            "in_deployment_hours": is_glm_deployment_hours(),
            "next_window": next_deployment_window().isoformat(),
            "models_tracked": list(self.models),
            "accounts": [
                {
                    "index": i,
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
            "total_active_sessions": sum(len(ms) for ms in self._slots),
        }

    async def acquire_with_fallback(
        self,
        requested_model: str,
        fallback_chain: tuple[str, ...] = (
            "z-ai/glm-5.1",
            "moonshotai/kimi-k2.6",
            "deepseek/deepseek-v4-pro",
            "minimax/minimax-m2.7",
        ),
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, FreebuffSession, str] | None:
        """Acquire requested model, fallback ke chain kalau unavailable.

        Returns (account_index, session, actual_model) atau None.
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
