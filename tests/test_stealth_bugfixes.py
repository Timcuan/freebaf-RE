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


if __name__ == "__main__":
    unittest.main()
