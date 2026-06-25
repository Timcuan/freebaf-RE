"""Tests for Freebuff Unleash — multi-account × multi-model session pool."""
from __future__ import annotations

import time as _time
from datetime import datetime, timezone

import pytest

from freebuff2api.config import Settings
from freebuff2api.freebuff_unleash import (
    is_glm_deployment_hours,
    next_deployment_window,
    SessionSlot,
    UnleashPool,
    SESSION_REFRESH_THRESHOLD_MS,
    PRE_EMPTIVE_REFRESH_MS,
    ALL_FREEBUFF_MODELS,
)
from freebuff2api.models import ALL_MODELS, resolve_model
from freebuff2api.codebuff import FreebuffSession


class TestDeploymentHours:
    def test_weekday_morning_utc_in_window(self):
        dt = datetime(2025, 3, 19, 14, 30, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is True

    def test_weekday_late_utc_outside_window(self):
        dt = datetime(2025, 3, 19, 6, 0, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False

    def test_saturday_always_outside(self):
        dt = datetime(2025, 3, 22, 14, 30, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False

    def test_sunday_always_outside(self):
        dt = datetime(2025, 3, 23, 14, 30, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False

    def test_weekday_after_5pm_pt_outside(self):
        dt = datetime(2025, 3, 20, 2, 0, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False


class TestNextWindow:
    def test_returns_future_when_outside(self):
        dt = datetime(2025, 3, 22, 14, 30, tzinfo=timezone.utc)
        nxt = next_deployment_window(dt)
        assert nxt > dt
        assert is_glm_deployment_hours(nxt) is True


class TestModels:
    def test_all_freebuff_models_tracked(self):
        # Pool tracks the 5 freebuff models
        assert "z-ai/glm-5.1" in ALL_FREEBUFF_MODELS
        assert "minimax/minimax-m2.7" in ALL_FREEBUFF_MODELS
        assert "moonshotai/kimi-k2.6" in ALL_FREEBUFF_MODELS
        assert "deepseek/deepseek-v4-pro" in ALL_FREEBUFF_MODELS

    def test_glm_5_2_alias_resolves_to_glm_5_1_upstream(self):
        m = resolve_model("zai/glm-5.2")
        assert m.upstream_model_id == "z-ai/glm-5.1"
        assert m.agent_id == "base2-free-glm-5-1"

    def test_z_ai_alias_resolves(self):
        m = resolve_model("z-ai/glm-5.1")
        assert m.id == "z-ai/glm-5.1"

    def test_no_external_providers(self):
        # Freebuff-only — no CF/Zai API providers (zai/glm-5.1 is a Freebuff alias
        # for the Z.AI model served via Codebuff, not a direct Z.ai API call)
        assert not any(m.id.startswith("cf/") for m in ALL_MODELS)
        # zai/glm-* are Freebuff aliases, not external Z.ai API routing
        zai_models = [m for m in ALL_MODELS if m.id.startswith("zai/")]
        for m in zai_models:
            # All should be owned by Freebuff/codebuff, served via Codebuff unleash
            assert m.owned_by in ("zai", "freebuff")


class TestSessionSlot:
    def test_fresh_session(self):
        session = FreebuffSession(
            instance_id="x", model="z-ai/glm-5.1", remaining_ms=3_600_000,
        )
        slot = SessionSlot(
            account_index=0, model="z-ai/glm-5.1", session=session,
            created_at=_time.time(), last_used_at=_time.time(),
        )
        assert slot.is_fresh is True
        # 3.6M ms remaining > 5M preemptive threshold? No, so no refresh needed yet
        # Actually 3.6M < 5M, so needs_preemptive_refresh IS True
        assert slot.needs_preemptive_refresh is True

    def test_expiring_session(self):
        session = FreebuffSession(
            instance_id="x", model="z-ai/glm-5.1", remaining_ms=60_000,
        )
        slot = SessionSlot(
            account_index=0, model="z-ai/glm-5.1", session=session,
            created_at=_time.time(), last_used_at=_time.time(),
        )
        assert slot.is_fresh is False
        assert slot.needs_preemptive_refresh is True

    def test_preemptive_refresh_threshold(self):
        # Session with remaining_ms just above preemptive threshold
        session = FreebuffSession(
            instance_id="x", model="z-ai/glm-5.1",
            remaining_ms=PRE_EMPTIVE_REFRESH_MS + 100_000,
        )
        slot = SessionSlot(
            account_index=0, model="z-ai/glm-5.1", session=session,
            created_at=_time.time(), last_used_at=_time.time(),
        )
        assert slot.needs_preemptive_refresh is False
        assert slot.is_fresh is True


class TestUnleashPool:
    def test_pool_init(self):
        from freebuff2api.codebuff import CodebuffAccountPool
        s = Settings(codebuff_token="t1,t2,t3", local_api_key="k")
        pool = CodebuffAccountPool(s)
        unleash = UnleashPool(s, pool)
        assert unleash.account_pool.account_count == 3
        assert len(unleash._slots) == 3
        assert unleash.models == ALL_FREEBUFF_MODELS
        # All slot dicts empty initially
        assert all(slots == {} for slots in unleash._slots)

    def test_round_robin_state(self):
        from freebuff2api.codebuff import CodebuffAccountPool
        s = Settings(codebuff_token="t1,t2", local_api_key="k")
        pool = CodebuffAccountPool(s)
        unleash = UnleashPool(s, pool)
        # Round-robin starts at 0 for each model
        for model in ALL_FREEBUFF_MODELS:
            assert unleash._next_account[model] == 0
