"""Tests for admin login-flow endpoints (web UI direct login)."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from freebuff2api.app import app
from freebuff2api.login_flow import LoginStartResult, LoginUser


def _admin_login(client: TestClient, admin_key: str = "admin-secret") -> None:
    client.post("/admin/api/login", json={"key": admin_key})


class LoginFlowAdminTests(unittest.TestCase):
    def test_start_requires_auth(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.post("/admin/api/login-flow/start?mode=freebuff")
        self.assertEqual(response.status_code, 401)

    def test_start_rejects_invalid_mode(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                _admin_login(client)
                response = client.post("/admin/api/login-flow/start?mode=evil")
        self.assertEqual(response.status_code, 400)

    def test_start_returns_login_url_and_session_id(self) -> None:
        start_result = LoginStartResult(
            fingerprint_id="fb-1",
            fingerprint_hash="hash-1",
            expires_at=9999,
            login_url="https://login.example/auth",
        )
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with TestClient(app) as client:
                    _admin_login(client)
                    response = client.post("/admin/api/login-flow/start?mode=freebuff")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["login_url"], "https://login.example/auth")
        self.assertTrue(data["session_id"])
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["mode"], "freebuff")

    def test_poll_unknown_session_returns_404(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                _admin_login(client)
                response = client.get("/admin/api/login-flow/poll/nope")
        self.assertEqual(response.status_code, 404)

    def test_poll_returns_pending_status(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")

        async def long_poll(*a, **kw):
            await asyncio.sleep(10)

        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=long_poll):
                    with TestClient(app) as client:
                        _admin_login(client)
                        start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                        poll = client.get(f"/admin/api/login-flow/poll/{start['session_id']}")
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["data"]["status"], "pending")

    def test_poll_returns_success_after_user_auths(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")
        user = LoginUser(user_id="u1", email="a@x", name="A", auth_token="tok-1", raw={})

        async def fast_poll(*a, **kw):
            return user

        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=fast_poll):
                    with TestClient(app) as client:
                        _admin_login(client)
                        start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                        # Give the background task a moment to complete
                        import time as _t

                        _t.sleep(0.1)
                        poll = client.get(f"/admin/api/login-flow/poll/{start['session_id']}")

        self.assertEqual(poll.status_code, 200)
        data = poll.json()["data"]
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["user"]["email"], "a@x")
        self.assertEqual(data["user"]["auth_token"], "tok-1")

    def test_commit_requires_success_status(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=asyncio.sleep(10)):
                    with TestClient(app) as client:
                        _admin_login(client)
                        start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                        commit = client.post(f"/admin/api/login-flow/commit/{start['session_id']}")

        self.assertEqual(commit.status_code, 400)

    def test_commit_adds_token_to_pool(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")
        user = LoginUser(user_id="u1", email="a@x", name="A", auth_token="tok-new", raw={})

        async def fast_poll(*a, **kw):
            return user

        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=fast_poll):
                    with patch("freebuff2api.admin.verify_token_async", new=AsyncMock(return_value=(True, "HTTP 200"))):
                        with patch("freebuff2api.admin.write_env_values") as write_env:
                            with patch("freebuff2api.admin.CodebuffAccountPool") as pool_cls:
                                pool = pool_cls.return_value
                                pool.account_count = 1
                                pool.default_client = object()
                                pool.default_sessions = object()
                                pool.aclose = AsyncMock()
                                with TestClient(app) as client:
                                    _admin_login(client)
                                    start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                                    import time as _t

                                    _t.sleep(0.1)
                                    commit = client.post(f"/admin/api/login-flow/commit/{start['session_id']}")

        self.assertEqual(commit.status_code, 200)
        self.assertEqual(commit.json()["msg"], "login committed and token added to pool")
        self.assertEqual(commit.json()["data"]["token_count"], 1)
        write_env.assert_called_once()

    def test_commit_rejects_unverified_token(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")
        user = LoginUser(user_id="u1", email="a@x", name="A", auth_token="bad", raw={})

        async def fast_poll(*a, **kw):
            return user

        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=fast_poll):
                    with patch("freebuff2api.admin.verify_token_async", new=AsyncMock(return_value=(False, "HTTP 401 rejected"))):
                        with TestClient(app) as client:
                            _admin_login(client)
                            start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                            import time as _t

                            _t.sleep(0.1)
                            commit = client.post(f"/admin/api/login-flow/commit/{start['session_id']}")

        self.assertEqual(commit.status_code, 402)

    def test_cancel_terminates_session(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")

        async def long_poll(*a, **kw):
            await asyncio.sleep(10)

        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=long_poll):
                    with TestClient(app) as client:
                        _admin_login(client)
                        start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                        cancel = client.post(f"/admin/api/login-flow/cancel/{start['session_id']}")
                        poll = client.get(f"/admin/api/login-flow/poll/{start['session_id']}")

        self.assertEqual(cancel.status_code, 200)
        # After cancel, poll should 404 because the session was popped
        self.assertEqual(poll.status_code, 404)

    def test_commit_idempotent_when_token_already_in_pool(self) -> None:
        start_result = LoginStartResult("fb-1", "h", 9999, "https://x")
        user = LoginUser(user_id="u1", email="a@x", name="A", auth_token="tok-dup", raw={})

        async def fast_poll(*a, **kw):
            return user

        with patch.dict(
            "os.environ",
            {"FREEBUFF_ADMIN_KEY": "admin-secret", "FREEBUFF_TOKEN": "tok-dup"},
            clear=True,
        ):
            with patch("freebuff2api.admin.start_login", new_callable=AsyncMock, return_value=start_result):
                with patch("freebuff2api.admin.poll_login", new_callable=AsyncMock, side_effect=fast_poll):
                    with patch("freebuff2api.admin.verify_token_async", new=AsyncMock(return_value=(True, "HTTP 200"))):
                        with patch("freebuff2api.admin.write_env_values"):
                            with patch("freebuff2api.admin.CodebuffAccountPool") as pool_cls:
                                pool = pool_cls.return_value
                                pool.account_count = 1
                                pool.default_client = object()
                                pool.default_sessions = object()
                                pool.aclose = AsyncMock()
                                with TestClient(app) as client:
                                    _admin_login(client)
                                    start = client.post("/admin/api/login-flow/start?mode=freebuff").json()["data"]
                                    import time as _t

                                    _t.sleep(0.1)
                                    commit = client.post(f"/admin/api/login-flow/commit/{start['session_id']}")

        self.assertEqual(commit.status_code, 200)
        self.assertIn("already in pool", commit.json()["msg"])
        self.assertEqual(commit.json()["data"]["token_count"], 1)


if __name__ == "__main__":
    unittest.main()
