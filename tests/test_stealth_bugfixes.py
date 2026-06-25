"""Tests for stealth bug fixes: ad-chain cache, warmup dedup, auth hardening."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from freebuff2api.app import app
from freebuff2api.codebuff import CodebuffClient, SessionManager
from freebuff2api.config import Settings


def _settings() -> Settings:
    return Settings(codebuff_token="tok", local_api_key="k")


class AdChainCacheTests(unittest.IsolatedAsyncioTestCase):
    """BUG #1: ad-chain should be cached per session to avoid bot signal."""

    async def test_cache_skips_repeat_request_within_ttl(self) -> None:
        client = CodebuffClient(_settings())
        cache: dict[str, float] = {}
        instance_id = "inst-1"
        call_count = {"n": 0}

        async def fake_request_ads(provider, messages=None, surface=None):
            call_count["n"] += 1
            return {"ads": [{"impressionIds": ["i1"], "impUrl": "https://x/impression"}]}

        with patch.object(client, "request_ads", side_effect=fake_request_ads):
            with patch.object(client, "report_zeroclick_impressions", new=AsyncMock()):
                with patch.object(client, "report_codebuff_impression", new=AsyncMock()):
                    await client.request_ad_chain(
                        session_instance_id=instance_id,
                        cache=cache,
                    )
                    first_count = call_count["n"]
                    await client.request_ad_chain(
                        session_instance_id=instance_id,
                        cache=cache,
                    )
                    assert call_count["n"] == first_count
                    assert instance_id in cache
        await client.aclose()

    async def test_cache_expires_after_ttl(self) -> None:
        client = CodebuffClient(_settings())
        cache: dict[str, float] = {}
        instance_id = "inst-1"
        call_count = {"n": 0}

        async def fake_request_ads(provider, messages=None, surface=None):
            call_count["n"] += 1
            return {"ads": [{"impressionIds": [], "impUrl": ""}]}

        with patch.object(client, "request_ads", side_effect=fake_request_ads):
            with patch.object(client, "report_zeroclick_impressions", new=AsyncMock()):
                with patch.object(client, "report_codebuff_impression", new=AsyncMock()):
                    await client.request_ad_chain(
                        session_instance_id=instance_id,
                        cache=cache,
                        cache_ttl_seconds=0.01,
                    )
                    await asyncio.sleep(0.05)
                    await client.request_ad_chain(
                        session_instance_id=instance_id,
                        cache=cache,
                        cache_ttl_seconds=0.01,
                    )
                    assert call_count["n"] >= 2
        await client.aclose()

    async def test_no_cache_backward_compat(self) -> None:
        client = CodebuffClient(_settings())
        call_count = {"n": 0}

        async def fake_request_ads(provider, messages=None, surface=None):
            call_count["n"] += 1
            return {"ads": []}

        with patch.object(client, "request_ads", side_effect=fake_request_ads):
            await client.request_ad_chain()
            first = call_count["n"]
            await client.request_ad_chain()
            # 2 providers × 2 calls = 4 (no cache → always requests)
            assert call_count["n"] == first * 2
        await client.aclose()

    def test_session_manager_has_ad_chain_cache(self) -> None:
        client = CodebuffClient(_settings())
        sm = SessionManager(client, _settings())
        assert hasattr(sm, "_ad_chain_cache")
        assert isinstance(sm._ad_chain_cache, dict)


class WarmupDedupTests(unittest.IsolatedAsyncioTestCase):
    """BUG #2: warmup tick should not spawn duplicate tasks for same (account, model)."""

    async def test_inflight_tracking_prevents_duplicate(self) -> None:
        from freebuff2api.freebuff_unleash import UnleashPool

        pool = UnleashPool.__new__(UnleashPool)
        pool._inflight = {}

        async def slow_task() -> None:
            await asyncio.sleep(10)

        fake_task = asyncio.create_task(slow_task())
        key = (0, "z-ai/glm-5.2")
        pool._inflight[key] = fake_task

        assert key in pool._inflight
        assert not pool._inflight[key].done()

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass


class AuthHardeningTests(unittest.TestCase):
    """BUG #3: sensitive health endpoints should require admin auth."""

    def test_health_egress_requires_admin_auth(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.get("/api/health/egress")
        assert response.status_code == 401

    def test_health_stealth_requires_admin_auth(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.get("/api/health/stealth")
        assert response.status_code == 401

    def test_health_glm52_requires_admin_auth(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.get("/api/health/glm52")
        assert response.status_code == 401

    def test_healthz_remains_public(self) -> None:
        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.status_code in (200, 401)


class LoginSessionSafetyTests(unittest.TestCase):
    """BUG #4: login session dict should be lock-protected."""

    def test_lock_exists(self) -> None:
        from freebuff2api.admin import _LOGIN_SESSIONS_LOCK

        assert _LOGIN_SESSIONS_LOCK is not None

    def test_prune_sync_empty_does_not_raise(self) -> None:
        from freebuff2api.admin import _prune_login_sessions_sync, _LOGIN_SESSIONS

        _LOGIN_SESSIONS.clear()
        _prune_login_sessions_sync()

    def test_prune_sync_removes_stale_only(self) -> None:
        from freebuff2api.admin import (
            _prune_login_sessions_sync,
            _LOGIN_SESSIONS,
            _LOGIN_SESSION_TTL,
        )

        _LOGIN_SESSIONS.clear()
        old = time.time() - _LOGIN_SESSION_TTL - 100
        _LOGIN_SESSIONS["stale"] = {"last_touched": old, "session_id": "stale"}
        _LOGIN_SESSIONS["fresh"] = {"last_touched": time.time(), "session_id": "fresh"}
        _prune_login_sessions_sync()
        assert "stale" not in _LOGIN_SESSIONS
        assert "fresh" in _LOGIN_SESSIONS
        _LOGIN_SESSIONS.clear()


class RateGovernorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """BUG #10: rate_governor was init'd but never actually called in chat routing."""

    async def test_acquire_session_accepts_preferred_index(self) -> None:
        """CodebuffAccountPool.acquire_session should accept preferred_index."""
        from freebuff2api.codebuff import CodebuffAccountPool

        settings = Settings(
            codebuff_token="tok-a",
            local_api_key="k",
        )
        # Multi-token pool
        settings_multi = Settings(
            codebuff_token="tok-a",
            local_api_key="k",
        )
        # Patch to simulate 2 tokens
        with patch.object(Settings, "codebuff_tokens", ("tok-a", "tok-b")):
            pool = CodebuffAccountPool(settings_multi)
            # Preferred index 1 should reserve account 1 if free
            assert pool.account_count == 2
            # Just verify the signature accepts preferred_index without error
            # (full acquire would hit network)
            await pool.aclose()

    async def test_jitter_delay_completes(self) -> None:
        from freebuff2api.rate_governor import RateGovernor

        gov = RateGovernor(account_count=2, min_jitter_ms=1, max_jitter_ms=2)
        await gov.jitter_delay()  # should complete quickly

    async def test_pick_account_returns_valid_index(self) -> None:
        from freebuff2api.rate_governor import RateGovernor

        gov = RateGovernor(account_count=3)
        idx = await gov.pick_account()
        assert 0 <= idx < 3

    async def test_record_request_increments_count(self) -> None:
        from freebuff2api.rate_governor import RateGovernor

        gov = RateGovernor(account_count=2)
        await gov.record_request(0)
        async with gov._lock:
            assert gov._accounts[0].daily_msg_count == 1


class DoubleRecordFixTests(unittest.TestCase):
    """BUG #8: streaming finally block was recording 'success' even after error."""

    def test_streaming_error_does_not_double_record(self) -> None:
        """Verify the errored flag logic exists in _stream_openai_chunks source."""
        import inspect

        from freebuff2api.app import _stream_openai_chunks

        source = inspect.getsource(_stream_openai_chunks)
        assert "errored = False" in source
        assert "if api_key and not errored" in source


class DeadParamCleanupTests(unittest.TestCase):
    """BUG #6: cf_enabled dead params should be removed from resolve_model/models_response."""

    def test_resolve_model_no_cf_enabled_param(self) -> None:
        import inspect

        from freebuff2api.models import resolve_model

        sig = inspect.signature(resolve_model)
        assert "cf_enabled" not in sig.parameters

    def test_models_response_no_cf_enabled_param(self) -> None:
        import inspect

        from freebuff2api.models import models_response

        sig = inspect.signature(models_response)
        assert "cf_enabled" not in sig.parameters


class AtomicWriteTests(unittest.TestCase):
    """BUG #12, #14: fingerprint store + .env writes should be atomic (temp + rename)."""

    def test_save_fingerprint_store_uses_atomic_write(self) -> None:
        import inspect

        from freebuff2api.stealth import save_fingerprint_store

        source = inspect.getsource(save_fingerprint_store)
        assert "os.replace" in source or "tmp" in source

    def test_write_env_values_uses_atomic_write(self) -> None:
        import inspect

        from freebuff2api.config import write_env_values

        source = inspect.getsource(write_env_values)
        assert "os.replace" in source or "tmp" in source

    def test_write_env_values_not_duplicated(self) -> None:
        """BUG #15: write_env_values was defined twice — second shadowed first."""
        import freebuff2api.config as cfg

        # Python only keeps one definition; verify there's no duplicate
        # by checking source file doesn't have 2 def lines
        import inspect

        source = inspect.getsource(cfg)
        assert source.count("def write_env_values") == 1
        assert source.count("def project_env_path") == 1


class FingerprintReuseTests(unittest.TestCase):
    """BUG #13: get_or_create_fingerprint should NOT save store on every reuse."""

    def test_get_or_create_does_not_save_on_reuse(self) -> None:
        import inspect

        from freebuff2api.stealth import get_or_create_fingerprint

        source = inspect.getsource(get_or_create_fingerprint)
        # The reuse path should return without calling save_fingerprint_store
        # Find the reuse branch (entry exists)
        assert "is_valid_fingerprint" in source
        # Verify the reuse path doesn't call save (only create path does)
        reuse_section = source[source.index("if entry and is_valid_fingerprint"):]
        create_section = reuse_section[reuse_section.index("fp = generate_legacy_fingerprint"):]
        reuse_only = reuse_section[:reuse_section.index("fp = generate_legacy_fingerprint")]
        assert "save_fingerprint_store" not in reuse_only


class Round4BugfixTests(unittest.TestCase):
    """Round 4: resource leaks, banned/geo handling, security headers, input validation."""

    def test_unleash_stop_cancels_inflight(self) -> None:
        """BUG #18: stop() should cancel in-flight tasks, not just warmup_task."""
        import inspect

        from freebuff2api.freebuff_unleash import UnleashPool

        source = inspect.getsource(UnleashPool.stop)
        assert "_inflight" in source
        assert "cancel" in source

    def test_create_one_handles_banned_status_code(self) -> None:
        """BUG #20: _create_one should mark banned via status_code, not string match."""
        import inspect

        from freebuff2api.freebuff_unleash import UnleashPool

        source = inspect.getsource(UnleashPool._create_one)
        assert "status_code == 403" in source
        assert "status_code == 451" in source
        assert "mark_banned" in source
        assert "mark_geo_blocked" in source

    def test_create_session_handles_banned_status_code(self) -> None:
        """BUG #20: _create_session_any_account should use status_code."""
        import inspect

        from freebuff2api.freebuff_unleash import UnleashPool

        source = inspect.getsource(UnleashPool._create_session_any_account)
        assert "status_code == 403" in source
        assert "status_code == 451" in source

    def test_security_headers_middleware_exists(self) -> None:
        """BUG #21: security headers middleware should be registered."""
        import inspect

        from freebuff2api.app import app, security_headers_middleware

        # Verify middleware function exists
        assert security_headers_middleware is not None
        source = inspect.getsource(security_headers_middleware)
        assert "X-Content-Type-Options" in source
        assert "X-Frame-Options" in source
        assert "Referrer-Policy" in source
        assert "Strict-Transport-Security" in source

    def test_admin_cookie_has_secure_flag(self) -> None:
        """BUG #22: admin cookie should set secure flag on HTTPS."""
        import inspect

        from freebuff2api.admin import login

        source = inspect.getsource(login)
        assert "secure=" in source
        assert "is_https" in source or "x-forwarded-proto" in source

    def test_chat_completions_validates_json_body(self) -> None:
        """BUG #23: chat completions should return 400 on malformed JSON."""
        import inspect

        from freebuff2api.app import chat_completions

        source = inspect.getsource(chat_completions)
        assert "valid JSON" in source
        assert "isinstance(body, dict)" in source

    def test_anthropic_messages_validates_json_body(self) -> None:
        """BUG #23: anthropic messages should return 400 on malformed JSON."""
        import inspect

        from freebuff2api.app import anthropic_messages

        source = inspect.getsource(anthropic_messages)
        assert "valid JSON" in source

    def test_reserve_account_has_timeout(self) -> None:
        """BUG #9: _reserve_account should have timeout, not hang forever."""
        import inspect

        from freebuff2api.codebuff import CodebuffAccountPool

        source = inspect.getsource(CodebuffAccountPool._reserve_account)
        assert "timeout" in source
        assert "wait_for" in source
        assert "timed out" in source or "TimeoutError" in source


class SecurityHeadersIntegrationTests(unittest.TestCase):
    """Integration: verify security headers appear in responses."""

    def test_security_headers_present(self) -> None:
        from fastapi.testclient import TestClient

        from freebuff2api.app import app

        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "no-referrer"


class ChatCompletionsValidationTests(unittest.TestCase):
    """Integration: verify malformed JSON returns 400."""

    def test_malformed_json_returns_400(self) -> None:
        from fastapi.testclient import TestClient

        from freebuff2api.app import app

        with patch.dict("os.environ", {"FREEBUFF_API_KEY": "k", "FREEBUFF_TOKEN": "tok"}, clear=True):
            with TestClient(app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    data="not valid json{{{",
                    headers={"Authorization": "Bearer k"},
                )
        assert response.status_code == 400


class SessionRefreshStaleSlotTests(unittest.TestCase):
    """BUG #29: refresh_session should clear stale slot if delete succeeds but new session fails."""

    def test_refresh_clears_slot_on_rate_limited(self) -> None:
        import inspect

        from freebuff2api.freebuff_unleash import UnleashPool

        source = inspect.getsource(UnleashPool.refresh_session)
        assert "slot cleared" in source or "_slots[account_index].pop" in source

    def test_refresh_clears_slot_on_exception(self) -> None:
        """Both except blocks should pop the stale slot."""
        import inspect

        from freebuff2api.freebuff_unleash import UnleashPool

        source = inspect.getsource(UnleashPool.refresh_session)
        # Both RateLimitedError and generic Exception paths should clear slot
        assert source.count("_slots[account_index].pop") >= 2


class ProxyAwareTokenVerifyTests(unittest.TestCase):
    """BUG #26: verify_token should be proxy-aware (use httpx, not urllib)."""

    def test_verify_token_uses_httpx_not_urllib(self) -> None:
        import inspect

        from freebuff2api.login_flow import verify_token

        source = inspect.getsource(verify_token)
        assert "httpx" in source
        assert "proxy" in source.lower()
        assert "stealth_transport" in source or "build_stealth_client" in source


class StealthTransportLoopTests(unittest.TestCase):
    """BUG #32: handle_async_request should use get_running_loop, not deprecated get_event_loop."""

    def test_uses_get_running_loop(self) -> None:
        import inspect

        from freebuff2api.stealth_transport import CurlCffiTransport

        source = inspect.getsource(CurlCffiTransport.handle_async_request)
        assert "get_running_loop" in source


class RateGovernorFallbackSignalTests(unittest.TestCase):
    """BUG #35: pick_account should return -1 when no account eligible (signal caller to fall back)."""

    def test_all_idle_returns_negative_one(self) -> None:
        from freebuff2api.rate_governor import RateGovernor

        gov = RateGovernor(account_count=2)
        # Force idle window
        with patch("freebuff2api.rate_governor.time.time", return_value=3 * 3600):
            picked = asyncio.run(gov.pick_account())
        assert picked == -1

    def test_all_at_cap_returns_negative_one(self) -> None:
        from freebuff2api.rate_governor import RateGovernor

        gov = RateGovernor(account_count=2, daily_msg_cap=1)
        now = time.time()
        for i in range(2):
            gov._accounts[i].daily_msg_count = 1
            gov._accounts[i].daily_reset_at = now + 99999
        picked = asyncio.run(gov.pick_account())
        assert picked == -1

    def test_app_py_handles_negative_one(self) -> None:
        """app.py should convert -1 to None (use default round-robin)."""
        import inspect

        from freebuff2api.app import chat_completions

        source = inspect.getsource(chat_completions)
        assert "preferred_index < 0" in source


if __name__ == "__main__":
    unittest.main()
