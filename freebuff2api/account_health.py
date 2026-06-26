"""Account health tracking for Freebuff Unleash.

Tracks per-account quota state for the two upstream session pools:

1. **Premium daily pool** (`pacific_day`): DeepSeek V4 Pro, Kimi K2.6, MiMo 2.5 Pro.
   Tier-0 accounts get 5 sessions/day (FREEBUFF_PREMIUM_SESSION_LIMIT=5,
   reset at midnight Pacific).

2. **GLM weekly referral pool** (`pacific_week`): GLM 5.2 only.
   Entitlement = qualified referral count (capped at
   FREEBUFF_GLM_V52_REFERRAL_CAP=10). Tier-0 accounts (no referrals) get 0
   GLM sessions/week. Each referral grants 5 weekly sessions.

Both pools gate POST /api/v1/freebuff/session (status 429 `rate_limited`).
Chat completions on an active session are NOT quota-checked, so the
strategy is:

  - pick an account with quota for the requested pool
  - create one 1h session
  - reuse it for unlimited chat completions
  - pre-emptive refresh at the 55-min mark consumes another quota unit
  - when the account is exhausted, hand off to the next account with quota

GLM 5.2 `availability` is `'always'` upstream — there is NO deployment-hours
gate. The only gate is the weekly referral pool.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("freebuff2api.account_health")

# Pool identifiers (match upstream `period` field)
POOL_PREMIUM_DAILY = "pacific_day"
POOL_GLM_WEEKLY = "pacific_week"


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class PoolQuota:
    """Quota state for one pool on one account."""

    limit: int = 0
    remaining: int | None = None  # None = unknown (not yet observed)
    reset_at: float | None = None  # epoch seconds
    exhausted_at: float | None = None  # when we last hit 429

    def is_known_exhausted(self, now: float | None = None) -> bool:
        """True if we've observed 429 AND the reset window hasn't passed."""
        if self.remaining is not None and self.remaining > 0:
            return False
        if self.reset_at is None or self.exhausted_at is None:
            return self.remaining is not None and self.remaining == 0
        now = now or time.time()
        return now < self.reset_at

    def reset_passed(self, now: float | None = None) -> bool:
        """True if the reset window has passed (account should be retried)."""
        if self.reset_at is None:
            return False
        now = now or time.time()
        return now >= self.reset_at

    def reset(self) -> None:
        """Clear observed quota state so the next acquire re-probes upstream."""
        self.remaining = None
        self.exhausted_at = None
        # Keep reset_at for diagnostics; it will be refreshed on next 429.


@dataclass
class AccountHealth:
    """Per-account health across both pools."""

    index: int
    premium_daily: PoolQuota = field(default_factory=PoolQuota)
    glm_weekly: PoolQuota = field(default_factory=PoolQuota)
    banned: bool = False
    geo_blocked: bool = False
    last_error: str | None = None
    last_success_at: float | None = None

    def pool(self, pool: str) -> PoolQuota:
        return self.glm_weekly if pool == POOL_GLM_WEEKLY else self.premium_daily

    def is_available(self, pool: str, now: float | None = None) -> bool:
        """True if the account can likely serve a new session for this pool."""
        if self.banned or self.geo_blocked:
            return False
        q = self.pool(pool)
        if q.reset_passed(now):
            return True  # reset window passed; re-probe
        return not q.is_known_exhausted(now)

    def mark_success(self, pool: str) -> None:
        q = self.pool(pool)
        if q.remaining is not None:
            q.remaining = max(0, q.remaining - 1)
        self.last_success_at = time.time()

    def mark_exhausted(self, pool: str, reset_at: float | None = None) -> None:
        q = self.pool(pool)
        q.remaining = 0
        q.exhausted_at = time.time()
        if reset_at is not None:
            q.reset_at = reset_at
        logger.info(
            "account %s exhausted pool=%s reset_at=%s remaining=%s",
            self.index, pool, q.reset_at, q.remaining,
        )

    def mark_banned(self) -> None:
        self.banned = True
        self.last_error = "banned"

    def mark_geo_blocked(self) -> None:
        self.geo_blocked = True
        self.last_error = "geo_blocked"


def pool_for_model(model: str) -> str:
    """Map a model id to its upstream session pool.

    Delegates to FreebuffModel.pool when the model is recognized. Falls back
    to heuristic matching for aliases / unknown models.
    """
    from .models import resolve_model
    try:
        m = resolve_model(model)
        return m.pool
    except ValueError:
        pass
    # Heuristic fallback for unknown model ids
    if "glm" in model.lower():
        return POOL_GLM_WEEKLY
    model_lower = model.lower()
    if any(p in model_lower for p in ("deepseek-v4-pro", "kimi", "mimo-v2.5-pro")):
        return POOL_PREMIUM_DAILY
    return ""


def parse_rate_limited(body: dict[str, Any]) -> dict[str, Any]:
    """Extract quota fields from a 429 `rate_limited` response body.

    Upstream shape (from common/src/types/freebuff-session.ts):
      { status: 'rate_limited', model, limit, period, resetTimeZone,
        resetAt, windowHours, recentCount, retryAfterMs }
    """
    return {
        "pool": body.get("period") or "",
        "model": body.get("model") or "",
        "limit": body.get("limit") or 0,
        "reset_at": _parse_iso(body.get("resetAt")),
        "recent_count": body.get("recentCount") or 0,
        "retry_after_ms": body.get("retryAfterMs") or 0,
    }


def parse_rate_limits_by_model(
    rate_limits: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract per-model quota snapshots from `rateLimitsByModel`.

    Shape: { [modelId]: { model, limit, period, resetTimeZone, resetAt,
                          windowHours, recentCount } }
    """
    if not rate_limits:
        return []
    out = []
    for model_id, snap in rate_limits.items():
        if not isinstance(snap, dict):
            continue
        out.append({
            "model": model_id,
            "pool": snap.get("period") or "",
            "limit": snap.get("limit") or 0,
            "reset_at": _parse_iso(snap.get("resetAt")),
            "recent_count": snap.get("recentCount") or 0,
        })
    return out


class AccountHealthRegistry:
    """Tracks AccountHealth for every account in the pool.

    Indexing matches CodebuffAccountPool._accounts order.
    """

    def __init__(self, account_count: int) -> None:
        self._health: list[AccountHealth] = [
            AccountHealth(index=i) for i in range(account_count)
        ]

    def __getitem__(self, idx: int) -> AccountHealth:
        return self._health[idx]

    def __len__(self) -> int:
        return len(self._health)

    def available_accounts(
        self,
        pool: str,
        now: float | None = None,
    ) -> list[int]:
        """Indices of accounts likely able to serve a new session for `pool`."""
        if not pool:
            # Non-gated model — any non-banned, non-geo-blocked account works
            return [h.index for h in self._health if not h.banned and not h.geo_blocked]
        now = now or time.time()
        return [h.index for h in self._health if h.is_available(pool, now)]

    def pick_account(
        self,
        pool: str,
        start: int = 0,
        now: float | None = None,
    ) -> int | None:
        """Round-robin pick the next available account for `pool`.

        Returns the account index, or None if all accounts are exhausted/banned.
        """
        avail = self.available_accounts(pool, now)
        if not avail:
            return None
        n = len(self._health)
        for offset in range(n):
            idx = (start + offset) % n
            if idx in avail:
                return idx
        return avail[0]

    def update_from_session_response(
        self,
        account_index: int,
        data: dict[str, Any],
    ) -> None:
        """Refresh quota state from a /session response body.

        Active/queued/ended responses carry `rateLimitsByModel` snapshots.
        """
        if account_index < 0 or account_index >= len(self._health):
            return
        h = self._health[account_index]
        if data.get("status") == "banned":
            h.mark_banned()
            return
        if data.get("status") == "country_blocked":
            h.mark_geo_blocked()
            return
        snaps = parse_rate_limits_by_model(data.get("rateLimitsByModel"))
        for snap in snaps:
            pool = snap.get("pool") or ""
            if not pool:
                continue
            q = h.pool(pool)
            q.limit = snap.get("limit") or q.limit
            used = snap.get("recent_count") or 0
            q.remaining = max(0, q.limit - used)
            if snap.get("reset_at"):
                q.reset_at = snap["reset_at"]

    def status(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {
                "index": h.index,
                "banned": h.banned,
                "geo_blocked": h.geo_blocked,
                "premium_daily": {
                    "limit": h.premium_daily.limit,
                    "remaining": h.premium_daily.remaining,
                    "reset_at": h.premium_daily.reset_at,
                    "exhausted": h.premium_daily.is_known_exhausted(now),
                },
                "glm_weekly": {
                    "limit": h.glm_weekly.limit,
                    "remaining": h.glm_weekly.remaining,
                    "reset_at": h.glm_weekly.reset_at,
                    "exhausted": h.glm_weekly.is_known_exhausted(now),
                },
                "last_error": h.last_error,
                "last_success_at": h.last_success_at,
            }
            for h in self._health
        ]
