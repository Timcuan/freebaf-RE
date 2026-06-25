"""Tests for GLM 5.2 unleash + Cloudflare Workers AI integration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from freebuff2api.config import Settings
from freebuff2api.glm52_unleash import (
    is_glm_deployment_hours,
    next_deployment_window,
    GlmSessionSlot,
    SESSION_REFRESH_THRESHOLD_MS,
)
from freebuff2api.models import (
    all_models_with_cf,
    CLOUDFLARE_FREE_MODELS,
    resolve_model,
    FreebuffModel,
)


class TestGlmDeploymentHours:
    def test_weekday_morning_utc_in_window(self):
        # 2025-03-19 14:30 UTC = 10:30 ET (Wed) — in window
        dt = datetime(2025, 3, 19, 14, 30, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is True

    def test_weekday_late_utc_outside_window(self):
        # 2025-03-19 06:00 UTC = 02:00 ET (Wed) — before 9am ET
        dt = datetime(2025, 3, 19, 6, 0, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False

    def test_saturday_always_outside(self):
        # Saturday — never available
        dt = datetime(2025, 3, 22, 14, 30, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False

    def test_sunday_always_outside(self):
        dt = datetime(2025, 3, 23, 14, 30, tzinfo=timezone.utc)
        assert is_glm_deployment_hours(dt) is False

    def test_weekday_after_5pm_pt_outside(self):
        # 2025-03-19 02:00 UTC next day = 6pm PT Wed — outside
        dt = datetime(2025, 3, 20, 2, 0, tzinfo=timezone.utc)
        # Thursday 02:00 UTC = Wed 9pm ET — outside (after 5pm PT)
        assert is_glm_deployment_hours(dt) is False


class TestNextDeploymentWindow:
    def test_returns_future_when_outside(self):
        dt = datetime(2025, 3, 22, 14, 30, tzinfo=timezone.utc)  # Saturday
        nxt = next_deployment_window(dt)
        assert nxt > dt
        # Should land on Monday morning UTC (13:00 = 9am ET)
        assert is_glm_deployment_hours(nxt) is True


class TestModelsCfIntegration:
    def test_cf_models_present_when_enabled(self):
        all_m = all_models_with_cf(cf_enabled=True)
        cf_ids = [m.id for m in all_m if m.provider == "cloudflare"]
        assert "cf/glm-5.2" in cf_ids
        assert "cf/glm-5.2-fp8" in cf_ids

    def test_cf_models_absent_when_disabled(self):
        # Free CF + Z.ai models always included now; paid only when cf_enabled
        all_m = all_models_with_cf(cf_enabled=False)
        paid = [m for m in all_m if m.provider == "zai" and "paid" in m.id]
        assert paid == []  # no paid Z.ai models without config

    def test_resolve_cf_model(self):
        m = resolve_model("cf/glm-5.2", cf_enabled=True)
        assert m.provider == "cloudflare"
        assert m.upstream_id == "@cf/zai-org/glm-5.2"

    def test_resolve_glm_5_2_falls_back_to_codebuff(self):
        # glm-5.2 without cf prefix should resolve to codebuff path
        m = resolve_model("glm-5.2", cf_enabled=True)
        assert m.provider == "codebuff"
        assert "glm" in m.id.lower()

    def test_glm_5_2_alias_resolves(self):
        for alias in ("zai/glm-5.2", "z-ai/glm-5.2", "glm-5.2"):
            m = resolve_model(alias, cf_enabled=False)
            assert "glm-5.2" in m.id.lower() or "glm-5.1" in m.id.lower()


class TestCfSettingsLoad:
    def test_cf_settings_default_none(self):
        s = Settings(codebuff_token="t", local_api_key="k")
        assert s.cf_account_ids is None
        assert s.cf_api_tokens is None
        assert s.cf_fallback_to_codebuff is True

    def test_cf_neuron_budget_default(self):
        s = Settings(codebuff_token="t", local_api_key="k")
        assert s.cf_neuron_budget_daily == 9000


class TestGlmSessionSlot:
    def test_fresh_session_is_fresh(self):
        import time as _time
        from freebuff2api.codebuff import FreebuffSession
        session = FreebuffSession(
            instance_id="x",
            model="z-ai/glm-5.1",
            remaining_ms=3_600_000,
        )
        slot = GlmSessionSlot(
            account_index=0,
            session=session,
            created_at=_time.time(),
            last_used_at=_time.time(),
        )
        assert slot.is_fresh is True
        assert slot.remaining_ms > SESSION_REFRESH_THRESHOLD_MS

    def test_expiring_session_not_fresh(self):
        import time as _time
        from freebuff2api.codebuff import FreebuffSession
        session = FreebuffSession(
            instance_id="x",
            model="z-ai/glm-5.1",
            remaining_ms=60_000,  # less than threshold
        )
        slot = GlmSessionSlot(
            account_index=0,
            session=session,
            created_at=_time.time(),
            last_used_at=_time.time(),
        )
        # remaining_ms from session directly, < threshold
        assert slot.is_fresh is False
