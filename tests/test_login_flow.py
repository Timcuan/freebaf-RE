"""Tests for freebuff2api/login_flow.py primitives."""
from __future__ import annotations

import asyncio
import urllib.error
from unittest.mock import patch

import pytest

from freebuff2api import login_flow
from freebuff2api.login_flow import (
    LoginStartResult,
    LoginUser,
    endpoints_for,
    generate_fingerprint,
    user_to_stored_dict,
)


def test_endpoints_for_freebuff():
    code, status = endpoints_for("freebuff")
    assert "freebuff.com" in code
    assert code.endswith("/api/auth/cli/code")
    assert status.endswith("/api/auth/cli/status")


def test_endpoints_for_codebuff():
    code, status = endpoints_for("codebuff")
    assert "codebuff.com" in code
    assert code.endswith("/api/auth/cli/code")
    assert status.endswith("/api/auth/cli/status")


def test_endpoints_for_unknown_defaults_to_freebuff():
    code, _ = endpoints_for("unknown")
    assert "freebuff.com" in code


def test_generate_fingerprint_format():
    fp = generate_fingerprint()
    # Must be an upstream-recognized format (codebuff-cli-<8 base64url>)
    assert fp.startswith("codebuff-cli-")
    suffix = fp[len("codebuff-cli-"):]
    assert len(suffix) == 8
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in suffix)


def test_generate_fingerprint_unique():
    fps = {generate_fingerprint() for _ in range(20)}
    assert len(fps) == 20


def test_generate_fingerprint_not_old_fb_format():
    # Critical: must NOT be the old `fb-<hex>` format that was invalid upstream
    fp = generate_fingerprint()
    assert not fp.startswith("fb-")


def test_user_to_stored_dict_shape():
    user = LoginUser(
        user_id="u1",
        email="a@x.com",
        name="A",
        auth_token="tok-1",
        raw={"id": "u1"},
    )
    d = user_to_stored_dict(
        user,
        fingerprint_id="fb-1",
        fingerprint_hash="hash-1",
        source="freebuff",
        added_at=1000.0,
    )
    assert d["user_id"] == "u1"
    assert d["email"] == "a@x.com"
    assert d["auth_token"] == "tok-1"
    assert d["fingerprint_id"] == "fb-1"
    assert d["fingerprint_hash"] == "hash-1"
    assert d["source"] == "freebuff"
    assert d["added_at"] == 1000.0
    assert d["index"] == 0


def test_user_to_stored_dict_added_at_defaults_to_now():
    user = LoginUser(user_id=None, email=None, name=None, auth_token="t", raw={})
    d = user_to_stored_dict(user, fingerprint_id="fb", fingerprint_hash="h", source="freebuff")
    assert d["added_at"] > 0


def test_request_code_returns_parsed_json():
    payload = {"loginUrl": "https://x", "fingerprintHash": "h", "expiresAt": 1}
    with patch("freebuff2api.login_flow._sync_request_code", return_value=payload) as mock:
        async def run():
            return await login_flow.request_code("fb-1", "https://code")

        result = asyncio.run(run())
        assert result == payload
        mock.assert_called_once_with("fb-1", "https://code")


def test_start_login_constructs_result():
    payload = {"loginUrl": "https://login", "fingerprintHash": "hash123", "expiresAt": 9999}
    with patch("freebuff2api.login_flow._sync_request_code", return_value=payload):
        result = asyncio.run(login_flow.start_login("freebuff"))
        assert isinstance(result, LoginStartResult)
        assert result.login_url == "https://login"
        assert result.fingerprint_hash == "hash123"
        assert result.expires_at == 9999
        # Must be an upstream-recognized format (not the old fb-<hex>)
        assert result.fingerprint_id.startswith("codebuff-cli-")


def test_poll_login_returns_user_when_auth_token_present():
    user_data = {"id": "u1", "email": "a@x", "name": "A", "authToken": "tok"}
    start = LoginStartResult(
        fingerprint_id="fb-1",
        fingerprint_hash="h",
        expires_at=9999,
        login_url="https://x",
    )
    with patch("freebuff2api.login_flow._sync_poll_once", return_value=user_data):
        result = asyncio.run(login_flow.poll_login(start, mode="freebuff", interval=0.01))
        assert isinstance(result, LoginUser)
        assert result.auth_token == "tok"
        assert result.email == "a@x"


def test_poll_login_polls_until_token():
    start = LoginStartResult(
        fingerprint_id="fb-1",
        fingerprint_hash="h",
        expires_at=9999,
        login_url="https://x",
    )
    user_data = {"id": "u1", "authToken": "tok"}
    responses = [None, None, None, user_data]
    with patch("freebuff2api.login_flow._sync_poll_once", side_effect=responses):
        result = asyncio.run(login_flow.poll_login(start, mode="freebuff", interval=0.01))
        assert result.auth_token == "tok"


def test_poll_login_timeout():
    start = LoginStartResult(
        fingerprint_id="fb-1",
        fingerprint_hash="h",
        expires_at=9999,
        login_url="https://x",
    )
    with patch("freebuff2api.login_flow._sync_poll_once", return_value=None):
        with pytest.raises(TimeoutError):
            asyncio.run(login_flow.poll_login(start, mode="freebuff", interval=0.01, timeout=0.05))


def test_poll_login_should_continue_aborts():
    start = LoginStartResult(
        fingerprint_id="fb-1",
        fingerprint_hash="h",
        expires_at=9999,
        login_url="https://x",
    )
    counter = {"n": 0}

    def should_continue():
        counter["n"] += 1
        return counter["n"] <= 1

    with patch("freebuff2api.login_flow._sync_poll_once", return_value=None):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                login_flow.poll_login(
                    start, mode="freebuff", interval=0.01, timeout=1.0, should_continue=should_continue
                )
            )


def test_sync_poll_once_returns_none_on_401():
    err = urllib.error.HTTPError(
        url="https://x",
        code=401,
        msg="unauthorized",
        hdrs=None,
        fp=None,
    )

    def raise_401(*a, **kw):
        raise err

    with patch("urllib.request.urlopen", side_effect=raise_401):
        result = login_flow._sync_poll_once("fb", "h", 1, "https://status")
        assert result is None


def test_sync_poll_once_returns_user_when_present():
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"user": {"id": "u1", "authToken": "tok"}}'

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        result = login_flow._sync_poll_once("fb", "h", 1, "https://status")
        assert result == {"id": "u1", "authToken": "tok"}


def test_verify_token_sync_rejected_on_401():
    err = urllib.error.HTTPError(
        url="https://x",
        code=401,
        msg="unauthorized",
        hdrs=None,
        fp=None,
    )

    class FakeRead:
        def read(self, n=-1): return b"rejected"

    def raise_401(*a, **kw):
        e = urllib.error.HTTPError("https://x", 401, "unauth", None, None)
        # urllib.error.HTTPError is also the response object; emulate .read()
        e.read = lambda n=-1: b"rejected"
        raise e

    with patch("urllib.request.urlopen", side_effect=raise_401):
        ok, info = login_flow.verify_token_sync("tok")
        assert ok is False
        assert "401" in info


def test_verify_token_sync_ok_on_200():
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        ok, info = login_flow.verify_token_sync("tok")
        assert ok is True
        assert "200" in info


def test_verify_token_async_wraps_sync():
    async def run():
        return await login_flow.verify_token("tok")

    with patch("freebuff2api.login_flow.verify_token_sync", return_value=(True, "ok")):
        ok, info = asyncio.run(run())
        assert ok is True
        assert info == "ok"
