"""Tests for freebuff2api/stealth_transport.py — TLS fingerprint mimicry."""
from __future__ import annotations

import asyncio
import importlib.util
from unittest.mock import MagicMock, patch

import httpx
import pytest

import freebuff2api.stealth_transport as st


def test_supported_profiles_includes_chrome():
    assert any(p.startswith("chrome") for p in st.SUPPORTED_PROFILES)


def test_default_profile_is_chrome():
    assert st.DEFAULT_PROFILE.startswith("chrome")


def test_is_stealth_transport_available_reflects_curl_cffi():
    # Should match whether curl_cffi imported successfully
    assert st.is_stealth_transport_available() == st._CURL_CFFI_AVAILABLE


def test_resolve_profile_default():
    assert st._resolve_profile(None) == st.DEFAULT_PROFILE
    assert st._resolve_profile("invalid") == st.DEFAULT_PROFILE


def test_resolve_profile_valid():
    assert st._resolve_profile("chrome120") == "chrome120"
    assert st._resolve_profile("safari17_0") == "safari17_0"


def test_build_stealth_client_returns_httpx_client():
    client = st.build_stealth_client()
    assert isinstance(client, httpx.AsyncClient)
    asyncio.run(client.aclose())


def test_build_stealth_client_with_proxy():
    if not st._CURL_CFFI_AVAILABLE:
        client = st.build_stealth_client(proxy="socks5://localhost:1080")
        assert isinstance(client, httpx.AsyncClient)
        asyncio.run(client.aclose())
    else:
        # curl_cffi path: transport wraps the proxy
        client = st.build_stealth_client(proxy="socks5://localhost:1080")
        assert isinstance(client, httpx.AsyncClient)
        assert isinstance(client._transport, st.CurlCffiTransport)
        assert client._transport.proxy == "socks5://localhost:1080"
        asyncio.run(client.aclose())


@pytest.mark.skipif(not st._CURL_CFFI_AVAILABLE, reason="curl_cffi not installed")
def test_curl_cffi_transport_handle_request():
    transport = st.CurlCffiTransport(profile="chrome124", proxy=None, timeout=5.0)
    assert transport.profile == "chrome124"

    # Mock curl_cffi.requests.request
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.headers = {"Content-Type": "application/json"}
    fake_resp.content = b'{"ok": true}'

    with patch("freebuff2api.stealth_transport.curl_requests.request", return_value=fake_resp) as mock_req:
        request = httpx.Request("GET", "https://example.com/test")
        response = asyncio.run(transport.handle_async_request(request))

    assert response.status_code == 200
    assert response.content == b'{"ok": true}'
    mock_req.assert_called_once()
    # Verify impersonate profile passed
    _, kwargs = mock_req.call_args
    assert kwargs.get("impersonate") == "chrome124"


@pytest.mark.skipif(not st._CURL_CFFI_AVAILABLE, reason="curl_cffi not installed")
def test_curl_cffi_transport_post_forwards_body():
    transport = st.CurlCffiTransport(profile="chrome124")

    fake_resp = MagicMock()
    fake_resp.status_code = 201
    fake_resp.headers = {"Content-Type": "application/json"}
    fake_resp.content = b'{"created": true}'

    with patch("freebuff2api.stealth_transport.curl_requests.request", return_value=fake_resp) as mock_req:
        request = httpx.Request(
            "POST",
            "https://example.com/api",
            content=b'{"key": "value"}',
            headers={"Content-Type": "application/json"},
        )
        response = asyncio.run(transport.handle_async_request(request))

    assert response.status_code == 201
    _, kwargs = mock_req.call_args
    assert kwargs.get("data") == b'{"key": "value"}'
    assert kwargs.get("method") == "POST"


@pytest.mark.skipif(not st._CURL_CFFI_AVAILABLE, reason="curl_cffi not installed")
def test_curl_cffi_transport_aclose_no_error():
    transport = st.CurlCffiTransport()
    asyncio.run(transport.aclose())  # should not raise


def test_build_stealth_client_fallback_when_unavailable():
    """When curl_cffi is not installed, build_stealth_client returns a plain httpx client."""
    with patch.object(st, "_CURL_CFFI_AVAILABLE", False):
        client = st.build_stealth_client()
        assert isinstance(client, httpx.AsyncClient)
        # Should NOT be a CurlCffiTransport
        assert not isinstance(getattr(client, "_transport", None), st.CurlCffiTransport)
        asyncio.run(client.aclose())
