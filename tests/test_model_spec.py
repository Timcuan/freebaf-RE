"""Tests for model spec accuracy — verified against upstream Codebuff source.

Source of truth:
  - common/src/constants/freebuff-models.ts (June 2026)
  - common/src/constants/model-config.ts
"""
from __future__ import annotations

import unittest

from freebuff2api.models import (
    ALL_MODELS,
    DEFAULT_MODEL,
    models_by_tier,
    models_response,
    latest_model_per_provider,
    resolve_model,
)


class ModelSpecTests(unittest.TestCase):
    """Every model field matches upstream spec exactly."""

    def test_deepseek_v4_pro_spec(self) -> None:
        m = resolve_model("deepseek/deepseek-v4-pro")
        self.assertEqual(m.display_name, "DeepSeek V4 Pro")
        self.assertEqual(m.tagline, "Smartest")
        self.assertEqual(m.tier, 1)
        self.assertTrue(m.premium)
        self.assertFalse(m.multimodal)
        self.assertTrue(m.data_collection)  # DeepSeek direct trains on data
        self.assertEqual(m.context_window, 1_000_000)
        self.assertTrue(m.can_spawn_gemini_thinker)
        self.assertEqual(m.agent_id, "base2-free-deepseek")

    def test_deepseek_v4_flash_spec(self) -> None:
        m = resolve_model("deepseek/deepseek-v4-flash")
        self.assertEqual(m.display_name, "DeepSeek V4 Flash")
        self.assertEqual(m.tagline, "Smart & Fast")
        self.assertEqual(m.tier, 0)
        self.assertFalse(m.premium)
        self.assertFalse(m.multimodal)
        self.assertTrue(m.data_collection)
        self.assertEqual(m.agent_id, "base2-free-deepseek-flash")

    def test_kimi_k26_spec(self) -> None:
        m = resolve_model("moonshotai/kimi-k2.6")
        self.assertEqual(m.display_name, "Kimi K2.6")
        self.assertEqual(m.tagline, "Balanced")
        self.assertEqual(m.tier, 1)
        self.assertTrue(m.premium)
        self.assertTrue(m.multimodal)
        self.assertFalse(m.data_collection)
        self.assertEqual(m.context_window, 256_000)
        self.assertTrue(m.can_spawn_gemini_thinker)
        self.assertEqual(m.agent_id, "base2-free-kimi")

    def test_mimo_v25_pro_spec(self) -> None:
        m = resolve_model("mimo/mimo-v2.5-pro")
        self.assertEqual(m.display_name, "MiMo 2.5 Pro")
        self.assertEqual(m.tagline, "Smartest & Slow")
        self.assertEqual(m.tier, 1)
        self.assertTrue(m.premium)
        self.assertTrue(m.multimodal)
        self.assertEqual(m.agent_id, "base2-free-mimo-pro")

    def test_mimo_v25_spec(self) -> None:
        """MiMo 2.5 (non-pro) = Tier 0 unlimited, multimodal."""
        m = resolve_model("mimo/mimo-v2.5")
        self.assertEqual(m.display_name, "MiMo 2.5")
        self.assertEqual(m.tagline, "Multimodal")
        self.assertEqual(m.tier, 0)
        self.assertFalse(m.premium)  # NOT premium
        self.assertTrue(m.multimodal)
        self.assertEqual(m.agent_id, "base2-free-mimo")

    def test_minimax_m3_spec(self) -> None:
        """MiniMax M3 = Tier 0 unlimited, multimodal, 1M context, no data collection."""
        m = resolve_model("minimax/minimax-m3")
        self.assertEqual(m.display_name, "MiniMax M3")
        self.assertEqual(m.tagline, "Smartest & Fastest")
        self.assertEqual(m.tier, 0)
        self.assertFalse(m.premium)  # NOT premium despite being newest
        self.assertTrue(m.multimodal)
        self.assertFalse(m.data_collection)  # Fireworks-served
        self.assertEqual(m.context_window, 1_000_000)
        self.assertTrue(m.can_spawn_gemini_thinker)
        self.assertEqual(m.agent_id, "base2-free-minimax-m3")

    def test_minimax_m27_spec(self) -> None:
        """MiniMax M2.7 = legacy, Tier 0, not multimodal."""
        m = resolve_model("minimax/minimax-m2.7")
        self.assertEqual(m.display_name, "MiniMax M2.7")
        self.assertEqual(m.tier, 0)
        self.assertFalse(m.premium)
        self.assertFalse(m.multimodal)
        self.assertFalse(m.data_collection)

    def test_glm_v52_spec(self) -> None:
        """GLM 5.2 = Tier 2 referral-gated, premium badge, not multimodal."""
        m = resolve_model("z-ai/glm-5.2")
        self.assertEqual(m.display_name, "GLM 5.2")
        self.assertEqual(m.tagline, "Unlock by referring friends")
        self.assertEqual(m.tier, 2)
        self.assertTrue(m.premium)  # badge
        self.assertFalse(m.multimodal)
        self.assertFalse(m.data_collection)  # Fireworks-served
        self.assertEqual(m.agent_id, "base2-free-glm")
        self.assertEqual(m.pool, "pacific_week")

    def test_glm_v52_alias_spec(self) -> None:
        m = resolve_model("glm-5.2")
        self.assertEqual(m.upstream_id, "z-ai/glm-5.2")
        self.assertEqual(m.tier, 2)
        self.assertEqual(m.agent_id, "base2-free-glm")

    def test_gemini_pro_thinker_spec(self) -> None:
        m = resolve_model("google/gemini-3.1-pro-preview")
        self.assertEqual(m.tier, 3)
        self.assertTrue(m.multimodal)
        self.assertEqual(m.agent_id, "thinker-with-files-gemini")

    def test_gemini_flash_lite_spec(self) -> None:
        m = resolve_model("google/gemini-2.5-flash-lite")
        self.assertEqual(m.tier, 0)
        self.assertEqual(m.agent_id, "file-picker")


class TierTests(unittest.TestCase):
    def test_tier_0_unlimited(self) -> None:
        t0 = models_by_tier(0)
        # MiniMax M3, MiMo 2.5, DeepSeek Flash, MiniMax M2.7, Gemini Flash Lite ×2
        ids = {m.id for m in t0}
        self.assertIn("minimax/minimax-m3", ids)
        self.assertIn("mimo/mimo-v2.5", ids)
        self.assertIn("deepseek/deepseek-v4-flash", ids)
        self.assertIn("minimax/minimax-m2.7", ids)
        for m in t0:
            self.assertFalse(m.premium, f"{m.id} should not be premium at tier 0")

    def test_tier_1_premium_daily(self) -> None:
        t1 = models_by_tier(1)
        ids = {m.id for m in t1}
        self.assertEqual(ids, {
            "deepseek/deepseek-v4-pro",
            "moonshotai/kimi-k2.6",
            "mimo/mimo-v2.5-pro",
        })
        for m in t1:
            self.assertTrue(m.premium)
            self.assertEqual(m.pool, "pacific_day")

    def test_tier_2_glm_weekly(self) -> None:
        t2 = models_by_tier(2)
        ids = {m.id for m in t2}
        self.assertEqual(ids, {"z-ai/glm-5.2", "glm-5.2"})
        for m in t2:
            self.assertEqual(m.pool, "pacific_week")

    def test_tier_3_thinker(self) -> None:
        t3 = models_by_tier(3)
        ids = {m.id for m in t3}
        self.assertEqual(ids, {"google/gemini-3.1-pro-preview"})


class DefaultModelTests(unittest.TestCase):
    def test_default_is_minimax_m3(self) -> None:
        """Upstream default = MiniMax M3 (smartest & fastest, unlimited)."""
        self.assertEqual(DEFAULT_MODEL.id, "minimax/minimax-m3")
        self.assertEqual(DEFAULT_MODEL.tier, 0)
        self.assertFalse(DEFAULT_MODEL.premium)

    def test_default_no_quota_gate(self) -> None:
        """Default model must be Tier 0 (no quota gate) for always-available."""
        self.assertEqual(DEFAULT_MODEL.pool, "")


class ProviderPriorityTests(unittest.TestCase):
    """Latest model per provider (newest first)."""

    def test_minimax_latest_is_m3(self) -> None:
        latest = latest_model_per_provider()
        self.assertEqual(latest["minimax"].id, "minimax/minimax-m3")

    def test_deepseek_latest_is_pro(self) -> None:
        latest = latest_model_per_provider()
        # Tier 0 preferred for "latest" (unlimited); DeepSeek Flash wins
        self.assertEqual(latest["deepseek"].id, "deepseek/deepseek-v4-flash")

    def test_mimo_latest_is_v25(self) -> None:
        latest = latest_model_per_provider()
        # MiMo 2.5 (tier 0) preferred over MiMo 2.5 Pro (tier 1)
        self.assertEqual(latest["mimo"].id, "mimo/mimo-v2.5")

    def test_kimi_only_one(self) -> None:
        latest = latest_model_per_provider()
        self.assertEqual(latest["moonshot"].id, "moonshotai/kimi-k2.6")


class ModelsResponseTests(unittest.TestCase):
    def test_models_response_includes_spec(self) -> None:
        resp = models_response()
        for item in resp["data"]:
            self.assertIn("tier", item)
            self.assertIn("tier_name", item)
            self.assertIn("premium", item)
            self.assertIn("multimodal", item)
            self.assertIn("data_collection", item)
            self.assertIn("context_window", item)
            self.assertIn("display_name", item)
            self.assertIn("tagline", item)

    def test_models_response_tier_names(self) -> None:
        resp = models_response()
        tier_names = {item["tier_name"] for item in resp["data"]}
        self.assertIn("unlimited", tier_names)
        self.assertIn("premium_daily", tier_names)
        self.assertIn("glm_weekly", tier_names)


class GeminiThinkerParentTests(unittest.TestCase):
    """Gemini thinker can be spawned by Tier 1 parents + MiniMax M3."""

    def test_thinker_parents(self) -> None:
        from freebuff2api.models import ALL_MODELS
        can_spawn = [m for m in ALL_MODELS if m.can_spawn_gemini_thinker]
        ids = {m.id for m in can_spawn}
        self.assertIn("deepseek/deepseek-v4-pro", ids)
        self.assertIn("moonshotai/kimi-k2.6", ids)
        self.assertIn("mimo/mimo-v2.5-pro", ids)
        self.assertIn("minimax/minimax-m3", ids)
        # Non-premium models (except M3) cannot spawn thinker
        self.assertNotIn("deepseek/deepseek-v4-flash", ids)
        self.assertNotIn("mimo/mimo-v2.5", ids)
        self.assertNotIn("minimax/minimax-m2.7", ids)


if __name__ == "__main__":
    unittest.main()
