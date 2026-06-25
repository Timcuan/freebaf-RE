import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from freebuff2api.app import app


class AdminTests(unittest.TestCase):
    def test_admin_page_is_served(self) -> None:
        response = TestClient(app).get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn("freebaf-RE admin panel", response.text)
        self.assertIn("naive-ui", response.text)
        self.assertIn("Auto refresh", response.text)
        self.assertIn("syncLogAutoRefresh", response.text)

    def test_root_redirects_to_admin(self) -> None:
        response = TestClient(app).get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/admin")

    def test_session_status_reports_login_state(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                anonymous = client.get("/admin/api/session")
                client.post("/admin/api/login", json={"key": "admin-secret"})
                authenticated = client.get("/admin/api/session")

        self.assertEqual(anonymous.status_code, 200)
        self.assertFalse(anonymous.json()["data"]["authenticated"])
        self.assertTrue(anonymous.json()["data"]["admin_key_configured"])
        self.assertEqual(authenticated.status_code, 200)
        self.assertTrue(authenticated.json()["data"]["authenticated"])

    def test_admin_api_requires_login(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.get("/admin/api/overview")

        self.assertEqual(response.status_code, 401)

    def test_admin_login_uses_admin_key_and_sets_cookie(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.post("/admin/api/login", json={"key": "admin-secret"})
                overview = client.get("/admin/api/overview")

        self.assertEqual(response.status_code, 200)
        self.assertIn("freebuff_admin_session", response.cookies)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["data"]["status"], "ok")

    def test_admin_login_uses_default_admin_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with TestClient(app) as client:
                response = client.post("/admin/api/login", json={"key": "sk-admin"})
                config = client.get("/admin/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(config.json()["data"]["using_default_admin_key"])
        self.assertFalse(config.json()["data"]["setup_complete"])

    def test_wrong_admin_key_is_rejected(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                response = client.post("/admin/api/login", json={"key": "bad"})

        self.assertEqual(response.status_code, 401)

    def test_save_tokens_updates_runtime_settings_without_real_env_write(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.write_env_values") as write_env:
                with patch("freebuff2api.admin.CodebuffAccountPool") as pool_cls:
                    pool = pool_cls.return_value
                    pool.account_count = 2
                    pool.default_client = object()
                    pool.default_sessions = object()
                    pool.aclose = AsyncMock()
                    with TestClient(app) as client:
                        client.post("/admin/api/login", json={"key": "admin-secret"})
                        response = client.put(
                            "/admin/api/freebuff-tokens",
                            json={"tokens": ["token-a", "token-b"]},
                        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["token_count"], 2)
        write_env.assert_called_once()

    def test_token_row_endpoints_can_reveal_update_add_and_delete_tokens(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FREEBUFF_ADMIN_KEY": "admin-secret",
                "FREEBUFF_TOKEN": "token-a,token-b",
            },
            clear=True,
        ):
            with patch("freebuff2api.admin.write_env_values"):
                with TestClient(app) as client:
                    client.post("/admin/api/login", json={"key": "admin-secret"})

                    revealed = client.get("/admin/api/freebuff-tokens/2")
                    added = client.post(
                        "/admin/api/freebuff-tokens",
                        json={"token": "token-c"},
                    )
                    updated = client.put(
                        "/admin/api/freebuff-tokens/2",
                        json={"token": "token-b-new"},
                    )
                    deleted = client.delete("/admin/api/freebuff-tokens/1")

        self.assertEqual(revealed.status_code, 200)
        self.assertEqual(revealed.json()["data"]["token"], "token-b")
        self.assertEqual(added.json()["data"]["token_count"], 3)
        self.assertEqual(updated.json()["data"]["tokens"][1]["masked"], "*" * len("token-b-new"))
        self.assertEqual(deleted.json()["data"]["token_count"], 2)

    def test_api_key_is_saved_by_separate_endpoint(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.write_env_values") as write_env:
                with TestClient(app) as client:
                    client.post("/admin/api/login", json={"key": "admin-secret"})
                    response = client.put(
                        "/admin/api/api-key",
                        json={"api_key": "new-api-key"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["api_key_configured"])
        write_env.assert_called_once_with({"FREEBUFF_API_KEY": "new-api-key"})

    def test_network_endpoint_returns_region_and_connectivity(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch(
                "freebuff2api.admin._probe_region",
                new_callable=AsyncMock,
                return_value={"ok": True, "ip": "203.0.113.1", "country": "Testland", "source": "ipapi.co"},
            ):
                with patch(
                    "freebuff2api.admin._probe_url",
                    new_callable=AsyncMock,
                    side_effect=[
                        {"name": "codebuff", "ok": True, "status": 200},
                        {"name": "freebuff", "ok": True, "status": 200},
                    ],
                ):
                    with TestClient(app) as client:
                        client.post("/admin/api/login", json={"key": "admin-secret"})
                        response = client.get("/admin/api/network")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["region"]["country"], "Testland")
        self.assertEqual(len(data["connectivity"]), 2)

    def test_admin_models_returns_v1_models_shape(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with TestClient(app) as client:
                client.post("/admin/api/login", json={"key": "admin-secret"})
                response = client.get("/admin/api/models")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["object"], "list")
        self.assertGreater(len(response.json()["data"]["data"]), 0)

    def test_env_endpoint_returns_local_env_content(self) -> None:
        with patch.dict("os.environ", {"FREEBUFF_ADMIN_KEY": "admin-secret"}, clear=True):
            with patch("freebuff2api.admin.project_env_path") as env_path:
                path = env_path.return_value
                path.exists.return_value = True
                path.read_text.return_value = "FREEBUFF_TOKEN=token\n"
                with TestClient(app) as client:
                    client.post("/admin/api/login", json={"key": "admin-secret"})
                    response = client.get("/admin/api/env")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["environment"], "local")
        self.assertTrue(data["editable"])
        self.assertEqual(data["content"], "FREEBUFF_TOKEN=token\n")

    def test_env_endpoint_warns_on_vercel_without_reading_env_file(self) -> None:
        with patch.dict(
            "os.environ",
            {"FREEBUFF_ADMIN_KEY": "admin-secret", "VERCEL": "1"},
            clear=True,
        ):
            with patch("freebuff2api.admin.project_env_path") as env_path:
                path = env_path.return_value
                path.exists.return_value = True
                path.read_text.side_effect = AssertionError("should not read .env on Vercel")
                with TestClient(app) as client:
                    client.post("/admin/api/login", json={"key": "admin-secret"})
                    response = client.get("/admin/api/env")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["environment"], "vercel")
        self.assertFalse(data["editable"])
        self.assertEqual(data["content"], "")
        self.assertIn("Environment Variables", data["message"])


if __name__ == "__main__":
    unittest.main()
