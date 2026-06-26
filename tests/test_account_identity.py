"""Tests for account_identity — per-account stealth isolation."""
from __future__ import annotations

import os
import unittest

from freebuff2api.account_identity import (
    AccountIdentity,
    AccountIdentityRegistry,
    _account_id_from_token,
    _locale_to_accept_language,
    _pick,
)


class HelperTests(unittest.TestCase):
    def test_account_id_stable(self) -> None:
        a = _account_id_from_token("tok-A")
        b = _account_id_from_token("tok-A")
        c = _account_id_from_token("tok-B")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 16)

    def test_locale_to_accept_language(self) -> None:
        self.assertEqual(_locale_to_accept_language("en-US"), "en-US,en;q=0.9")
        self.assertEqual(_locale_to_accept_language("zh-CN"), "zh-CN,zh;q=0.9")

    def test_pick_deterministic(self) -> None:
        items = ("a", "b", "c")
        self.assertEqual(_pick(items, 0, "x"), "a")
        self.assertEqual(_pick(items, 1, "x"), "b")
        self.assertEqual(_pick(items, 2, "x"), "c")
        self.assertEqual(_pick(items, 3, "x"), "a")  # wraps
        self.assertEqual(_pick((), 0, "x"), "")


class IdentityRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save env to restore after each test
        self._saved_env: dict[str, str | None] = {}
        for key in (
            "FREEBUFF_IDENTITY_ISOLATION",
            "FREEBUFF_PER_ACCOUNT_PROXY",
            "FREEBUFF_PER_ACCOUNT_TLS",
            "FREEBUFF_PER_ACCOUNT_CLI_VERSION",
            "FREEBUFF_PER_ACCOUNT_LOCALE",
            "FREEBUFF_PER_ACCOUNT_TIMEZONE",
            "FREEBUFF_ACCOUNT_STAGGER_MINUTES",
            "FREEBUFF_EGRESS_PROXY_URL",
        ):
            self._saved_env[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_isolation_disabled_all_share_global(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "false"
        os.environ["FREEBUFF_EGRESS_PROXY_URL"] = "socks5://global:1080"
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        self.assertFalse(reg.isolation_enabled)
        for i in range(len(tokens)):
            ident = reg[i]
            self.assertEqual(ident.proxy_url, "socks5://global:1080")
            self.assertEqual(ident.tls_profile, "chrome124")
            self.assertEqual(ident.cli_version, "1.0.682")
            self.assertEqual(ident.locale, "en-US")
            self.assertEqual(ident.activity_phase_minutes, 0)

    def test_isolation_enabled_distinct_per_account(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        os.environ["FREEBUFF_PER_ACCOUNT_PROXY"] = (
            "socks5://a:1080,socks5://b:1080,socks5://c:1080"
        )
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        self.assertTrue(reg.isolation_enabled)
        self.assertEqual(reg.isolated_count, 3)
        proxies = {reg[i].proxy_url for i in range(3)}
        self.assertEqual(proxies, {"socks5://a:1080", "socks5://b:1080", "socks5://c:1080"})
        # TLS profiles differ across accounts (3 distinct from SUPPORTED_PROFILES)
        profiles = {reg[i].tls_profile for i in range(3)}
        self.assertEqual(len(profiles), 3)
        # CLI versions differ
        versions = {reg[i].cli_version for i in range(3)}
        self.assertEqual(len(versions), 3)
        # Locales differ
        locales = {reg[i].locale for i in range(3)}
        self.assertEqual(len(locales), 3)
        # Phase stagger
        phases = {reg[i].activity_phase_minutes for i in range(3)}
        self.assertEqual(phases, {0, 15, 30})  # 0, 15, 30 with default stagger 15

    def test_proxy_fallback_to_global_when_list_short(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        os.environ["FREEBUFF_PER_ACCOUNT_PROXY"] = "socks5://a:1080"  # only 1
        os.environ["FREEBUFF_EGRESS_PROXY_URL"] = "socks5://global:1080"
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        # Account 0 gets the explicit proxy, 1 and 2 fall back to global
        self.assertEqual(reg[0].proxy_url, "socks5://a:1080")
        self.assertEqual(reg[1].proxy_url, "socks5://global:1080")
        self.assertEqual(reg[2].proxy_url, "socks5://global:1080")
        self.assertEqual(reg.isolated_count, 3)  # all have proxy (even if shared)

    def test_no_proxy_at_all(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        tokens = ("tok-A", "tok-B")
        reg = AccountIdentityRegistry(tokens)
        self.assertTrue(reg.isolation_enabled)
        self.assertEqual(reg.isolated_count, 0)  # no proxy = no isolation
        for i in range(2):
            self.assertIsNone(reg[i].proxy_url)
            # TLS + UA + locale still vary
            self.assertIsNotNone(reg[i].tls_profile)
            self.assertIsNotNone(reg[i].cli_version)

    def test_status_redacts_proxy(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        os.environ["FREEBUFF_PER_ACCOUNT_PROXY"] = "socks5://secret:1080"
        tokens = ("tok-A",)
        reg = AccountIdentityRegistry(tokens)
        status = reg.status()
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["proxy_url"], "***")
        self.assertNotIn("secret", str(status))

    def test_accept_language_derived_from_locale(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        os.environ["FREEBUFF_PER_ACCOUNT_LOCALE"] = "zh-CN,en-US,ja-JP"
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        self.assertEqual(reg[0].accept_language, "zh-CN,zh;q=0.9")
        self.assertEqual(reg[1].accept_language, "en-US,en;q=0.9")
        self.assertEqual(reg[2].accept_language, "ja-JP,ja;q=0.9")

    def test_cli_user_agent_format(self) -> None:
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        os.environ["FREEBUFF_PER_ACCOUNT_CLI_VERSION"] = "1.0.682,1.0.680"
        tokens = ("tok-A", "tok-B")
        reg = AccountIdentityRegistry(tokens)
        self.assertEqual(reg[0].cli_user_agent, "codebuff/1.0.682")
        self.assertEqual(reg[1].cli_user_agent, "codebuff/1.0.680")

    def test_client_id_unique_per_account(self) -> None:
        """Per-account client_id breaks cross-account correlation."""
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        cids = {reg[i].client_id for i in range(3)}
        self.assertEqual(len(cids), 3)  # all distinct
        for cid in cids:
            self.assertEqual(len(cid), 11)  # matches upstream hex[:11] format

    def test_session_id_unique_and_uuid_format(self) -> None:
        tokens = ("tok-A", "tok-B")
        reg = AccountIdentityRegistry(tokens)
        sids = {reg[i].session_id for i in range(2)}
        self.assertEqual(len(sids), 2)
        for sid in sids:
            # UUID format: 8-4-4-4-12
            self.assertEqual(len(sid), 36)
            self.assertEqual(sid.count("-"), 4)

    def test_identity_stable_across_registries(self) -> None:
        """Same token → same client_id/session_id across restarts."""
        tokens = ("tok-A", "tok-B")
        reg1 = AccountIdentityRegistry(tokens)
        reg2 = AccountIdentityRegistry(tokens)
        for i in range(2):
            self.assertEqual(reg1[i].client_id, reg2[i].client_id)
            self.assertEqual(reg1[i].session_id, reg2[i].session_id)

    def test_browser_ua_varies_per_account(self) -> None:
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        uas = {reg[i].browser_ua for i in range(3)}
        self.assertEqual(len(uas), 3)  # all distinct

    def test_device_os_varies_per_account(self) -> None:
        """device.os in ad-request body varies per account (correlation break)."""
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "true"
        os.environ["FREEBUFF_PER_ACCOUNT_LOCALE"] = "zh-CN,en-US,de-DE"
        tokens = ("tok-A", "tok-B", "tok-C")
        reg = AccountIdentityRegistry(tokens)
        # zh-CN → windows, en-US → macos, de-DE → linux
        self.assertEqual(reg[0].device_os, "windows")
        self.assertEqual(reg[1].device_os, "macos")
        self.assertEqual(reg[2].device_os, "linux")

    def test_legacy_mode_still_has_unique_client_session_ids(self) -> None:
        """Even in legacy mode, client_id/session_id must be per-account."""
        os.environ["FREEBUFF_IDENTITY_ISOLATION"] = "false"
        tokens = ("tok-A", "tok-B")
        reg = AccountIdentityRegistry(tokens)
        self.assertNotEqual(reg[0].client_id, reg[1].client_id)
        self.assertNotEqual(reg[0].session_id, reg[1].session_id)

    def test_empty_tokens(self) -> None:
        reg = AccountIdentityRegistry(())
        self.assertEqual(len(reg), 0)
        self.assertEqual(reg.status(), [])

    def test_isolation_enabled_with_no_env_uses_defaults(self) -> None:
        # Default is isolation ON (no env set)
        tokens = ("tok-A", "tok-B")
        reg = AccountIdentityRegistry(tokens)
        self.assertTrue(reg.isolation_enabled)
        # TLS profiles rotate through SUPPORTED_PROFILES
        self.assertIn(reg[0].tls_profile, ("chrome124", "chrome120", "chrome116"))
        self.assertIn(reg[1].tls_profile, ("chrome124", "chrome120", "chrome116"))


if __name__ == "__main__":
    unittest.main()
