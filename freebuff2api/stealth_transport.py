"""TLS fingerprint mimicry via curl_cffi.

httpx uses Python's ssl module, which produces a TLS ClientHello that does
not match any real browser or the Node/Bun CLI. Upstream can fingerprint
this via JA3/JA4 (and Codebuff has a cloudflare front that sees it).

curl_cffi wraps libcurl-impersonate, which reproduces the exact TLS
fingerprint of real browsers/clients. We use the `chrome` profile by
default because:
  - The Codebuff web app is served behind Cloudflare
  - Cloudflare's JA3 bot detection is browser-aware
  - A Chrome TLS fingerprint is the most common real-client signal

This module provides an httpx transport wrapper that routes requests
through curl_cffi while keeping the httpx.AsyncClient API unchanged.

If curl_cffi is unavailable, falls back to the default httpx transport
with a warning — the gateway still works but loses TLS stealth.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("freebuff2api.stealth_transport")

# Browser profiles supported by curl_cffi.
# chrome124 = latest stable Chrome at time of writing; matches our HAR_BROWSER_USER_AGENT.
SUPPORTED_PROFILES: tuple[str, ...] = (
    "chrome124",
    "chrome120",
    "chrome116",
    "chrome110",
    "edge99",
    "safari17_0",
    "safari15_3",
    "firefox102",
)

DEFAULT_PROFILE = "chrome124"

try:
    from curl_cffi import requests as curl_requests  # type: ignore
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    logger.info(
        "curl_cffi not installed — TLS fingerprint mimicry disabled. "
        "Install with: pip install curl_cffi"
    )


def is_stealth_transport_available() -> bool:
    return _CURL_CFFI_AVAILABLE


def _resolve_profile(profile: str | None) -> str:
    if profile and profile in SUPPORTED_PROFILES:
        return profile
    return DEFAULT_PROFILE


class CurlCffiTransport(httpx.AsyncBaseTransport):
    """httpx transport backed by curl_cffi for TLS fingerprint mimicry.

    Each request is forwarded to curl_cffi with the chosen browser profile,
    so the TLS ClientHello matches a real browser. The HTTP/2 frame ordering
    and ALPN negotiation also match the target browser.
    """

    def __init__(
        self,
        *,
        profile: str | None = None,
        proxy: str | None = None,
        timeout: float = 30.0,
        verify: bool = True,
    ) -> None:
        if not _CURL_CFFI_AVAILABLE:
            raise RuntimeError("curl_cffi is not installed")
        self.profile = _resolve_profile(profile)
        self.proxy = proxy
        self.timeout = timeout
        self.verify = verify

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Forward the httpx request to curl_cffi
        method = request.method
        url = str(request.url)
        headers = dict(request.headers)
        body = request.content if method in ("POST", "PUT", "PATCH") else None

        def _send() -> Any:
            return curl_requests.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                impersonate=self.profile,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                timeout=self.timeout,
                verify=self.verify,
                allow_redirects=False,
            )

        import asyncio

        loop = asyncio.get_event_loop()
        cffi_resp = await loop.run_in_executor(None, _send)

        # Convert curl_cffi response back to httpx.Response
        response_headers = []
        for k, v in (cffi_resp.headers or {}).items():
            response_headers.append((k, v))
        return httpx.Response(
            status_code=cffi_resp.status_code,
            headers=response_headers,
            content=cffi_resp.content,
            request=request,
        )

    async def aclose(self) -> None:
        pass


def build_stealth_client(
    *,
    proxy: str | None = None,
    profile: str | None = None,
    timeout: float = 30.0,
    trust_env: bool = False,
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with TLS fingerprint mimicry.

    Falls back to a standard httpx.AsyncClient if curl_cffi is not installed.
    """
    if _CURL_CFFI_AVAILABLE:
        transport = CurlCffiTransport(profile=profile, proxy=proxy, timeout=timeout)
        return httpx.AsyncClient(
            transport=transport,
            trust_env=trust_env,
            timeout=timeout,
            proxy=None,  # proxy handled inside the transport
        )
    logger.warning(
        "stealth transport unavailable — using standard httpx (TLS fingerprint NOT mimicked)"
    )
    client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": trust_env}
    if proxy:
        client_kwargs["proxy"] = proxy
    return httpx.AsyncClient(**client_kwargs)
