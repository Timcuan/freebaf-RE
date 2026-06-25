"""Tests for freebuff2api/proxy_validation.py — VPN/proxy hard-block detection."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from freebuff2api import proxy_validation as pv
from freebuff2api.proxy_validation import (
    ProxyValidation,
    validate_egress_for_upstream,
    validate_proxy_egress,
)


def test_classify_clean_signals():
    hard, limited, reason = pv._classify(())
    assert not hard
    assert not limited
    assert "clean" in reason


def test_classify_hosting_only_is_limited_not_hard_blocked():
    hard, limited, reason = pv._classify(("hosting",))
    assert not hard
    assert limited
    assert "limited" in reason.lower()


def test_classify_vpn_is_hard_blocked():
    hard, limited, reason = pv._classify(("vpn",))
    assert hard
    assert not limited
    assert "vpn" in reason


def test_classify_proxy_is_hard_blocked():
    hard, limited, reason = pv._classify(("proxy",))
    assert hard


def test_classify_tor_is_hard_blocked():
    hard, limited, reason = pv._classify(("tor",))
    assert hard


def test_classify_res_proxy_is_hard_blocked():
    hard, limited, reason = pv._classify(("res_proxy",))
    assert hard


def test_classify_vpn_plus_hosting_is_hard_blocked():
    # vpn wins over hosting
    hard, limited, reason = pv._classify(("vpn", "hosting"))
    assert hard
    assert not limited


def test_parse_ipinfo_clean():
    data = {"ip": "1.2.3.4", "country": "US", "org": "Comcast", "privacy": {"signals": []}}
    result = pv._parse_ipinfo(data)
    assert result.ok is True
    assert result.hard_blocked is False
    assert result.country == "US"
    assert result.source == "ipinfo.io"


def test_parse_ipinfo_vpn_hard_blocked():
    data = {"ip": "1.2.3.4", "country": "US", "org": "NordVPN", "privacy": {"signals": ["vpn", "hosting"]}}
    result = pv._parse_ipinfo(data)
    assert result.ok is False
    assert result.hard_blocked is True
    assert "vpn" in result.reason


def test_parse_ipinfo_hosting_limited():
    data = {"ip": "1.2.3.4", "country": "US", "org": "DigitalOcean", "privacy": {"signals": ["hosting"]}}
    result = pv._parse_ipinfo(data)
    assert result.ok is True  # not hard-blocked
    assert result.limited is True


def test_parse_ip_api_proxy_flag():
    data = {"status": "success", "countryCode": "US", "country": "United States",
            "query": "1.2.3.4", "isp": "SomeISP", "org": "SomeOrg", "proxy": True, "hosting": False}
    result = pv._parse_ip_api(data)
    assert result.hard_blocked is True
    assert result.ok is False
    assert "proxy" in result.privacy_signals


def test_parse_ip_api_hosting_only_limited():
    data = {"status": "success", "countryCode": "US", "country": "United States",
            "query": "1.2.3.4", "isp": "AWS", "org": "AWS", "proxy": False, "hosting": True}
    result = pv._parse_ip_api(data)
    assert result.hard_blocked is False
    assert result.limited is True


def test_parse_ip_api_clean():
    data = {"status": "success", "countryCode": "US", "country": "United States",
            "query": "1.2.3.4", "isp": "Comcast", "org": "Comcast", "proxy": False, "hosting": False}
    result = pv._parse_ip_api(data)
    assert result.hard_blocked is False
    assert result.limited is False
    assert result.ok is True


def test_parse_ipapi_no_privacy_data_assumes_ok():
    data = {"ip": "1.2.3.4", "country_code": "US", "country_name": "United States", "org": "Comcast"}
    result = pv._parse_ipapi(data)
    assert result.ok is True
    assert result.country == "US"
    assert "unknown" in result.reason


def test_proxy_validation_as_dict_shape():
    v = ProxyValidation(
        ok=True, country="US", country_name="United States", ip="1.2.3.4",
        isp="Comcast", org="Comcast", privacy_signals=("hosting",),
        hard_blocked=False, limited=True, reason="limited", source="ipinfo.io",
    )
    d = v.as_dict()
    assert d["ok"] is True
    assert d["country"] == "US"
    assert d["privacy_signals"] == ["hosting"]
    assert d["hard_blocked"] is False
    assert d["limited"] is True


def _make_mock_transport(json_responses: list[dict | Exception], status_codes: list[int] | None = None):
    """Create a mock httpx transport that returns canned responses in order."""
    import httpx

    calls = {"n": 0}

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            idx = min(calls["n"], len(json_responses) - 1)
            calls["n"] += 1
            resp = json_responses[idx]
            if isinstance(resp, Exception):
                raise resp
            status = status_codes[idx] if status_codes else 200
            return httpx.Response(status_code=status, json=resp, request=request)

    return MockTransport(), calls


def test_validate_proxy_egress_uses_ipinfo_first():
    transport, calls = _make_mock_transport([
        {"ip": "1.2.3.4", "country": "US", "org": "Comcast", "privacy": {"signals": []}}
    ])
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = asyncio.run(validate_proxy_egress(None))
    assert result.source == "ipinfo.io"
    assert result.country == "US"
    assert calls["n"] == 1  # only one probe needed (ipinfo succeeded)


def test_validate_proxy_egress_falls_back_to_ip_api_when_ipinfo_fails():
    transport, calls = _make_mock_transport(
        [
            Exception("ipinfo 500"),  # ipinfo fails
            {"status": "success", "countryCode": "US", "country": "United States",
             "query": "1.2.3.4", "isp": "Comcast", "proxy": False, "hosting": False},
        ],
    )
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = asyncio.run(validate_proxy_egress(None))
    assert result.source == "ip-api.com"
    assert result.country == "US"


def test_validate_proxy_egress_all_fail_returns_unknown():
    transport, _ = _make_mock_transport([Exception("network down"), Exception("down"), Exception("down")])
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = asyncio.run(validate_proxy_egress(None))
    assert result.ok is False
    assert result.source == "unknown"
    assert "failed" in result.reason


def test_validate_egress_for_upstream_combines_premium_and_privacy():
    direct = ProxyValidation(
        ok=False, country="ID", country_name="Indonesia", ip="1.1.1.1",
        isp="Telkom", org="Telkom", privacy_signals=(),
        hard_blocked=False, limited=False, reason="clean", source="ipinfo.io",
    )
    proxy_v = ProxyValidation(
        ok=True, country="US", country_name="United States", ip="2.2.2.2",
        isp="Comcast", org="Comcast", privacy_signals=(),
        hard_blocked=False, limited=False, reason="clean", source="ipinfo.io",
    )

    async def fake_validate(url, **kw):
        if url is None:
            return direct
        return proxy_v

    with patch("freebuff2api.proxy_validation.validate_proxy_egress", new=AsyncMock(side_effect=fake_validate)):
        result = asyncio.run(validate_egress_for_upstream("socks5://proxy:1080"))
    assert result["direct_ok"] is False  # non-premium
    assert result["proxy_ok"] is True
    assert result["ok"] is True  # proxy saves it


def test_validate_egress_for_upstream_rejects_hard_blocked_proxy():
    direct = ProxyValidation(
        ok=False, country="ID", country_name=None, ip="1.1.1.1",
        isp=None, org=None, privacy_signals=(),
        hard_blocked=False, limited=False, reason="clean", source="ipinfo.io",
    )
    proxy_v = ProxyValidation(
        ok=False, country="US", country_name="United States", ip="2.2.2.2",
        isp="NordVPN", org="NordVPN", privacy_signals=("vpn",),
        hard_blocked=True, limited=False, reason="hard-blocked: vpn", source="ipinfo.io",
    )

    async def fake_validate(url, **kw):
        return direct if url is None else proxy_v

    with patch("freebuff2api.proxy_validation.validate_proxy_egress", new=AsyncMock(side_effect=fake_validate)):
        result = asyncio.run(validate_egress_for_upstream("socks5://proxy:1080"))
    # Even though proxy is US, it's hard-blocked → not ok
    assert result["proxy_ok"] is False
    assert result["ok"] is False
    assert "HARD-BLOCKED" in result["recommendation"]


def test_validate_egress_for_upstream_direct_ok_no_proxy():
    direct = ProxyValidation(
        ok=True, country="US", country_name="United States", ip="1.1.1.1",
        isp="Comcast", org="Comcast", privacy_signals=(),
        hard_blocked=False, limited=False, reason="clean", source="ipinfo.io",
    )

    async def fake_validate(url, **kw):
        return direct

    with patch("freebuff2api.proxy_validation.validate_proxy_egress", new=AsyncMock(side_effect=fake_validate)):
        result = asyncio.run(validate_egress_for_upstream(None))
    assert result["direct_ok"] is True
    assert result["ok"] is True
    assert result["proxy"] is None
    assert "no proxy needed" in result["recommendation"]
