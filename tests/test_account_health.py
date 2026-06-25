"""Tests for account health registry and quota-aware unleash behavior."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from freebuff2api.account_health import (
    AccountHealth,
    AccountHealthRegistry,
    POOL_GLM_WEEKLY,
    POOL_PREMIUM_DAILY,
    PoolQuota,
    parse_rate_limited,
    parse_rate_limits_by_model,
    pool_for_model,
)


class TestPoolForModel:
    def test_glm_maps_to_weekly_pool(self):
        assert pool_for_model("z-ai/glm-5.2") == POOL_GLM_WEEKLY
        assert pool_for_model("glm-5.2") == POOL_GLM_WEEKLY

    def test_premium_models_map_to_daily_pool(self):
        assert pool_for_model("deepseek/deepseek-v4-pro") == POOL_PREMIUM_DAILY
        assert pool_for_model("moonshotai/kimi-k2.6") == POOL_PREMIUM_DAILY
        assert pool_for_model("mimo/mimo-v2.5-pro") == POOL_PREMIUM_DAILY

    def test_non_premium_models_map_to_empty_pool(self):
        assert pool_for_model("deepseek/deepseek-v4-flash") == ""
        assert pool_for_model("minimax/minimax-m2.7") == ""
        assert pool_for_model("mimo/mimo-v2.5") == ""


class TestPoolQuota:
    def test_fresh_quota_not_exhausted(self):
        q = PoolQuota(limit=5, remaining=3)
        assert not q.is_known_exhausted()

    def test_zero_remaining_is_exhausted(self):
        q = PoolQuota(limit=5, remaining=0, exhausted_at=time.time())
        assert q.is_known_exhausted()

    def test_reset_passed_clears_exhaustion(self):
        past = time.time() - 100
        q = PoolQuota(limit=5, remaining=0, reset_at=past, exhausted_at=past - 50)
        assert q.reset_passed()
        # Once the reset window has passed, is_known_exhausted returns False
        # so the account becomes eligible for retry
        assert not q.is_known_exhausted()

    def test_reset_clears_state(self):
        q = PoolQuota(limit=5, remaining=0, exhausted_at=time.time())
        q.reset()
        assert q.remaining is None
        assert q.exhausted_at is None


class TestAccountHealth:
    def test_fresh_account_available_for_any_pool(self):
        h = AccountHealth(index=0)
        assert h.is_available(POOL_GLM_WEEKLY)
        assert h.is_available(POOL_PREMIUM_DAILY)

    def test_banned_account_unavailable(self):
        h = AccountHealth(index=0)
        h.mark_banned()
        assert not h.is_available(POOL_GLM_WEEKLY)
        assert not h.is_available(POOL_PREMIUM_DAILY)

    def test_geo_blocked_account_unavailable(self):
        h = AccountHealth(index=0)
        h.mark_geo_blocked()
        assert not h.is_available(POOL_GLM_WEEKLY)

    def test_mark_exhausted_blocks_pool(self):
        h = AccountHealth(index=0)
        h.mark_exhausted(POOL_GLM_WEEKLY, reset_at=time.time() + 3600)
        assert not h.is_available(POOL_GLM_WEEKLY)
        # Other pool still available
        assert h.is_available(POOL_PREMIUM_DAILY)

    def test_reset_passed_makes_account_available_again(self):
        h = AccountHealth(index=0)
        past = time.time() - 10
        h.mark_exhausted(POOL_PREMIUM_DAILY, reset_at=past)
        assert h.is_available(POOL_PREMIUM_DAILY)

    def test_mark_success_decrements_remaining(self):
        h = AccountHealth(index=0)
        h.premium_daily.remaining = 5
        h.mark_success(POOL_PREMIUM_DAILY)
        assert h.premium_daily.remaining == 4


class TestAccountHealthRegistry:
    def test_pick_account_round_robin(self):
        reg = AccountHealthRegistry(3)
        # All accounts healthy
        assert reg.pick_account(POOL_GLM_WEEKLY, start=0) == 0
        assert reg.pick_account(POOL_GLM_WEEKLY, start=1) == 1
        assert reg.pick_account(POOL_GLM_WEEKLY, start=2) == 2
        assert reg.pick_account(POOL_GLM_WEEKLY, start=3) == 0  # wraps

    def test_pick_account_skips_exhausted(self):
        reg = AccountHealthRegistry(3)
        reg[1].mark_exhausted(POOL_GLM_WEEKLY, reset_at=time.time() + 3600)
        # start=0 → 0 available, skip 1, return 0
        assert reg.pick_account(POOL_GLM_WEEKLY, start=0) == 0
        # start=1 → 1 exhausted, skip to 2
        assert reg.pick_account(POOL_GLM_WEEKLY, start=1) == 2

    def test_pick_account_returns_none_when_all_exhausted(self):
        reg = AccountHealthRegistry(2)
        reg[0].mark_exhausted(POOL_GLM_WEEKLY, reset_at=time.time() + 3600)
        reg[1].mark_exhausted(POOL_GLM_WEEKLY, reset_at=time.time() + 3600)
        assert reg.pick_account(POOL_GLM_WEEKLY) is None

    def test_pick_account_for_non_gated_pool_returns_any_non_banned(self):
        reg = AccountHealthRegistry(2)
        # Non-gated model (DeepSeek Flash) — pool="" → any account works
        assert reg.pick_account("") is not None
        reg[0].mark_banned()
        assert reg.pick_account("") == 1

    def test_available_accounts_lists_only_healthy(self):
        reg = AccountHealthRegistry(3)
        reg[0].mark_banned()
        reg[2].mark_exhausted(POOL_PREMIUM_DAILY, reset_at=time.time() + 3600)
        avail = reg.available_accounts(POOL_PREMIUM_DAILY)
        assert avail == [1]


class TestParseRateLimited:
    def test_parses_429_body(self):
        body = {
            "status": "rate_limited",
            "model": "z-ai/glm-5.2",
            "limit": 5,
            "period": "pacific_week",
            "resetTimeZone": "America/Los_Angeles",
            "resetAt": "2026-06-30T07:00:00Z",
            "windowHours": 168,
            "recentCount": 5,
            "retryAfterMs": 345600000,
        }
        info = parse_rate_limited(body)
        assert info["pool"] == "pacific_week"
        assert info["model"] == "z-ai/glm-5.2"
        assert info["limit"] == 5
        assert info["reset_at"] is not None
        assert info["recent_count"] == 5
        assert info["retry_after_ms"] == 345600000

    def test_handles_missing_fields(self):
        info = parse_rate_limited({})
        assert info["pool"] == ""
        assert info["reset_at"] is None
        assert info["limit"] == 0


class TestParseRateLimitsByModel:
    def test_parses_snapshot(self):
        snaps = parse_rate_limits_by_model({
            "z-ai/glm-5.2": {
                "model": "z-ai/glm-5.2",
                "limit": 5,
                "period": "pacific_week",
                "resetAt": "2026-06-30T07:00:00Z",
                "recentCount": 2,
            },
            "deepseek/deepseek-v4-pro": {
                "model": "deepseek/deepseek-v4-pro",
                "limit": 5,
                "period": "pacific_day",
                "resetAt": "2026-06-27T07:00:00Z",
                "recentCount": 4,
            },
        })
        assert len(snaps) == 2
        glm = next(s for s in snaps if s["model"] == "z-ai/glm-5.2")
        assert glm["pool"] == "pacific_week"
        assert glm["limit"] == 5
        assert glm["reset_at"] is not None

    def test_handles_none(self):
        assert parse_rate_limits_by_model(None) == []
        assert parse_rate_limits_by_model({}) == []


class TestUpdateFromSessionResponse:
    def test_updates_quota_from_rate_limits(self):
        reg = AccountHealthRegistry(1)
        reg.update_from_session_response(0, {
            "status": "active",
            "rateLimitsByModel": {
                "z-ai/glm-5.2": {
                    "model": "z-ai/glm-5.2",
                    "limit": 5,
                    "period": "pacific_week",
                    "resetAt": "2026-06-30T07:00:00Z",
                    "recentCount": 5,
                },
            },
        })
        h = reg[0]
        assert h.glm_weekly.limit == 5
        assert h.glm_weekly.remaining == 0  # 5 - 5
        assert h.glm_weekly.reset_at is not None

    def test_marks_banned(self):
        reg = AccountHealthRegistry(1)
        reg.update_from_session_response(0, {"status": "banned"})
        assert reg[0].banned is True

    def test_marks_geo_blocked(self):
        reg = AccountHealthRegistry(1)
        reg.update_from_session_response(0, {"status": "country_blocked"})
        assert reg[0].geo_blocked is True


class TestRateLimitedError:
    def test_error_carries_metadata(self):
        from freebuff2api.codebuff import RateLimitedError
        err = RateLimitedError(
            "test",
            pool="pacific_week",
            model="z-ai/glm-5.2",
            limit=5,
            reset_at=1234567890.0,
            retry_after_ms=345600000,
        )
        assert err.status_code == 429
        assert err.pool == "pacific_week"
        assert err.model == "z-ai/glm-5.2"
        assert err.limit == 5
        assert err.reset_at == 1234567890.0
        assert isinstance(err, Exception)
