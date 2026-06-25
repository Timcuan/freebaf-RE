"""Tests for refactor: model aliases, egress proxy priority, deploy mode detection."""
import os
import unittest
from unittest.mock import patch

from freebuff2api.config import (
    PREMIUM_REGIONS,
    Settings,
    _detect_deploy_mode,
    load_settings,
)
from freebuff2api.models import (
    ALL_MODELS,
    FREEBUFF_MODELS,
    resolve_model,
)


class ModelAliasTests(unittest.TestCase):
    def test_glm_5_2_registered(self) -> None:
        ids = {m.id for m in ALL_MODELS}
        self.assertIn("z-ai/glm-5.2", ids)

    def test_glm_5_2_resolves_to_correct_agent(self) -> None:
        m = resolve_model("z-ai/glm-5.2")
        self.assertEqual(m.id, "z-ai/glm-5.2")
        self.assertEqual(m.agent_id, "base2-free-glm")

    def test_glm_short_alias_resolves(self) -> None:
        m = resolve_model("glm-5.2")
        self.assertEqual(m.upstream_model_id, "z-ai/glm-5.2")
        self.assertEqual(m.agent_id, "base2-free-glm")

    def test_short_alias_resolves(self) -> None:
        m = resolve_model("glm-5.2")
        self.assertEqual(m.id, "glm-5.2")

    def test_claude_alias_falls_back_to_default(self) -> None:
        m = resolve_model("claude-sonnet-4")
        self.assertEqual(m.id, "deepseek/deepseek-v4-flash")

    def test_gpt5_alias_resolves_to_pro(self) -> None:
        m = resolve_model("gpt-5")
        self.assertEqual(m.id, "deepseek/deepseek-v4-pro")

    def test_gemini_pro_alias_resolves(self) -> None:
        m = resolve_model("gemini-pro")
        self.assertEqual(m.id, "google/gemini-3.1-pro-preview")

    def test_unknown_model_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_model("nonexistent/model-xyz")

    def test_default_model_when_none(self) -> None:
        m = resolve_model(None)
        self.assertEqual(m.id, "deepseek/deepseek-v4-flash")

    def test_all_models_count(self) -> None:
        # 7 freebuff models (deepseek×2, kimi, minimax×2, mimo×2) + 2 GLM (z-ai/glm-5.2, glm-5.2) + 3 gemini = 12
        self.assertGreaterEqual(len(ALL_MODELS), 12)


class EgressProxyPriorityTests(unittest.TestCase):
    def test_egress_proxy_takes_priority(self) -> None:
        s = Settings(
            codebuff_token="t",
            local_api_key=None,
            proxy_enabled=True,
            proxy_url="http://old:8080",
            egress_proxy_url="socks5://new:1080",
        )
        self.assertEqual(s.upstream_proxy_url, "socks5://new:1080")

    def test_legacy_proxy_used_when_no_egress(self) -> None:
        s = Settings(
            codebuff_token="t",
            local_api_key=None,
            proxy_enabled=True,
            proxy_url="http://legacy:8080",
        )
        self.assertEqual(s.upstream_proxy_url, "http://legacy:8080")

    def test_legacy_proxy_ignored_when_disabled(self) -> None:
        s = Settings(
            codebuff_token="t",
            local_api_key=None,
            proxy_enabled=False,
            proxy_url="http://legacy:8080",
        )
        self.assertIsNone(s.upstream_proxy_url)

    def test_egress_proxy_active_without_legacy(self) -> None:
        s = Settings(
            codebuff_token="t",
            local_api_key=None,
            proxy_enabled=False,
            proxy_url=None,
            egress_proxy_url="socks5h://us-proxy:1080",
        )
        self.assertEqual(s.upstream_proxy_url, "socks5h://us-proxy:1080")

    def test_blank_proxy_urls_ignored(self) -> None:
        s = Settings(
            codebuff_token="t",
            local_api_key=None,
            proxy_enabled=True,
            proxy_url="   ",
            egress_proxy_url="  ",
        )
        self.assertIsNone(s.upstream_proxy_url)


class DeployModeTests(unittest.TestCase):
    def test_vercel_detected(self) -> None:
        with patch.dict("os.environ", {"VERCEL": "1"}):
            self.assertEqual(_detect_deploy_mode(), "vercel")

    def test_local_default(self) -> None:
        # Clear all platform env vars
        env = {k: v for k, v in os.environ.items()
               if k not in ("VERCEL", "VERCEL_URL", "K_SERVICE", "GOOGLE_CLOUD_PROJECT",
                            "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV", "DYNO", "CONTAINER")}
        with patch.dict("os.environ", env, clear=True):
            # Depends on /.dockerenv and /proc/1 — on macOS = local
            mode = _detect_deploy_mode()
            self.assertIn(mode, ("local", "vps"))

    def test_explicit_override(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_DEPLOY_MODE": "vps"}):
            s = load_settings()
            self.assertEqual(s.deploy_mode, "vps")


class PremiumRegionTests(unittest.TestCase):
    def test_us_in_premium(self) -> None:
        self.assertIn("US", PREMIUM_REGIONS)

    def test_ca_in_premium(self) -> None:
        self.assertIn("CA", PREMIUM_REGIONS)

    def test_non_premium_not_in_set(self) -> None:
        self.assertNotIn("ID", PREMIUM_REGIONS)
        self.assertNotIn("CN", PREMIUM_REGIONS)


class SettingsNewFieldsTests(unittest.TestCase):
    def test_cli_user_agent_env_overridable(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_CLI_USER_AGENT": "Bun/1.4.0"}):
            s = load_settings()
            self.assertEqual(s.cli_user_agent, "Bun/1.4.0")

    def test_default_cli_user_agent(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "FREEBUFF_CLI_USER_AGENT"}
        with patch.dict("os.environ", env, clear=True):
            s = load_settings()
            self.assertEqual(s.cli_user_agent, "Bun/1.3.11")

    def test_egress_settings_loaded(self) -> None:
        with patch.dict("os.environ", {
            "FREEBUFF_EGRESS_REGION": "US",
            "FREEBUFF_EGRESS_PROXY_URL": "socks5://proxy:1080",
            "FREEBUFF_EGRESS_AUTO": "false",
        }):
            s = load_settings()
            self.assertEqual(s.egress_region_required, "US")
            self.assertEqual(s.egress_proxy_url, "socks5://proxy:1080")
            self.assertFalse(s.egress_auto)


if __name__ == "__main__":
    unittest.main()
