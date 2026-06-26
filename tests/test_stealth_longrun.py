"""Long-run stealth tests — memory bounds, governor drift, unleash health."""
from __future__ import annotations

import time
import unittest

from freebuff2api.account_health import AccountHealthRegistry, POOL_GLM_WEEKLY
from freebuff2api.codebuff import _prune_ad_chain_cache
from freebuff2api.models import unleash_warmup_models
from freebuff2api.rate_governor import (
    AccountUsage,
    RateGovernor,
    _local_hour,
    _next_midnight_local,
)


class AdChainCacheLongrunTests(unittest.TestCase):
    def test_prune_drops_expired_entries(self) -> None:
        cache = {"old": 100.0, "fresh": 2000.0}
        _prune_ad_chain_cache(cache, now=2000.0, ttl_seconds=1800.0)
        self.assertNotIn("old", cache)
        self.assertIn("fresh", cache)

    def test_prune_caps_max_entries(self) -> None:
        cache = {f"s{i}": float(i) for i in range(50)}
        _prune_ad_chain_cache(cache, now=99999.0, ttl_seconds=1800.0, max_entries=32)
        self.assertLessEqual(len(cache), 32)

    def test_simulated_week_of_refreshes_bounded(self) -> None:
        """55min refresh × 7 days × 5 models ≈ 1008 entries — prune keeps ≤32."""
        cache: dict[str, float] = {}
        now = time.time()
        for day in range(7):
            for model in range(5):
                for refresh in range(3):
                    inst = f"acc0-{model}-d{day}-r{refresh}"
                    cache[inst] = now + day * 86400 + refresh * 3300
                    _prune_ad_chain_cache(cache, cache[inst], ttl_seconds=1800.0, max_entries=32)
        self.assertLessEqual(len(cache), 32)


class RateGovernorLongrunTests(unittest.TestCase):
    def test_distinct_hours_use_local_offset(self) -> None:
        acc = AccountUsage(account_index=0, local_offset_hours=-5)
        # UTC noon = 7am ET → hour 7 local
        utc_noon = 12 * 3600.0
        acc.record(utc_noon)
        self.assertEqual(acc.distinct_hours, {_local_hour(utc_noon, -5)})

    def test_daily_reset_at_midnight_not_drift(self) -> None:
        acc = AccountUsage(account_index=0, local_offset_hours=0)
        noon = 12 * 3600.0  # local hour 12
        acc._maybe_reset_daily(noon)
        self.assertAlmostEqual(acc.daily_reset_at - noon, 12 * 3600, delta=1)

    def test_activity_phase_stagger(self) -> None:
        gov = RateGovernor(
            account_count=2,
            activity_phases=[0, 30],
            activity_stagger_minutes=15,
        )
        # At minute 0, account 0 in phase, account 1 not
        t = 1000 * 60  # minute 0 of some hour
        self.assertTrue(gov._accounts[0].in_activity_phase(t, 15))
        self.assertFalse(gov._accounts[1].in_activity_phase(t, 15))

    def test_hard_cap_only_returns_minus_one(self) -> None:
        gov = RateGovernor(account_count=2, daily_msg_cap=1)
        now = time.time()
        for i in range(2):
            gov._accounts[i].daily_msg_count = 1
            gov._accounts[i].daily_reset_at = now + 99999
        import asyncio
        self.assertEqual(asyncio.run(gov.pick_account()), -1)


class UnleashWarmupModelsTests(unittest.TestCase):
    def test_includes_minimax_m3(self) -> None:
        models = unleash_warmup_models()
        self.assertIn("minimax/minimax-m3", models)

    def test_includes_glm_and_premium(self) -> None:
        models = unleash_warmup_models()
        self.assertIn("z-ai/glm-5.2", models)
        self.assertIn("deepseek/deepseek-v4-pro", models)
        self.assertIn("moonshotai/kimi-k2.6", models)

    def test_excludes_thinker_tier(self) -> None:
        models = unleash_warmup_models()
        self.assertNotIn("google/gemini-3.1-pro-preview", models)


class AccountHealthRegistryTests(unittest.TestCase):
    def test_mark_success_on_registry(self) -> None:
        reg = AccountHealthRegistry(2)
        reg[0].glm_weekly.remaining = 5
        reg.mark_success(0, POOL_GLM_WEEKLY)
        self.assertEqual(reg[0].glm_weekly.remaining, 4)

    def test_mark_exhausted_on_registry(self) -> None:
        reg = AccountHealthRegistry(1)
        reg.mark_exhausted(0, POOL_GLM_WEEKLY, reset_at=time.time() + 3600)
        self.assertEqual(reg[0].glm_weekly.remaining, 0)


class MidnightHelperTests(unittest.TestCase):
    def test_next_midnight_advances_correctly(self) -> None:
        # 23:00 UTC → midnight in 1h
        t = 23 * 3600.0
        nxt = _next_midnight_local(t, local_offset_hours=0)
        self.assertAlmostEqual(nxt - t, 3600, delta=1)


if __name__ == "__main__":
    unittest.main()
