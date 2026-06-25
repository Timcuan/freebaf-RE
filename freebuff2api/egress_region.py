"""Egress region detection + premium-region enforcement.

Codebuff free-tier requires US/EU egress IP. Non-US IPs hit
`session_model_mismatch` (409) per upstream behavior.

This module:
1. Detects current public IP + country via free geo-IP APIs.
2. Compares against `PREMIUM_REGIONS` whitelist.
3. If non-premium + `FREEBUFF_EGRESS_PROXY_URL` set → routes upstream through it.
4. If non-premium + no proxy → logs warning, returns region info for caller.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import PREMIUM_REGIONS, Settings

logger = logging.getLogger("freebuff2api.egress")


@dataclass(frozen=True)
class EgressInfo:
    ip: str | None
    country: str | None      # "US", "DE", "ID", etc.
    country_name: str | None
    region: str | None
    city: str | None
    timezone: str | None
    is_premium: bool
    proxy_active: bool
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "country": self.country,
            "country_name": self.country_name,
            "region": self.region,
            "city": self.city,
            "timezone": self.timezone,
            "is_premium": self.is_premium,
            "proxy_active": self.proxy_active,
            "source": self.source,
        }


# Free geo-IP APIs, fallback chain.
_GEO_PROVIDERS: tuple[tuple[str, str], ...] = (
    # (name, url)
    ("ipinfo.io", "https://ipinfo.io/json"),
    ("ip-api.com", "http://ip-api.com/json"),
    ("ipapi.co", "https://ipapi.co/json/"),
    ("ifconfig.co", "https://ifconfig.co/json"),
)


async def _fetch_geo(client: httpx.AsyncClient, name: str, url: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return _normalize_geo(name, data)
    except Exception as e:
        logger.debug("geo fetch failed for %s: %s", name, e)
        return None


def _normalize_geo(source: str, data: dict[str, Any]) -> dict[str, Any]:
    """Normalize different geo-IP API responses to common shape."""
    if source == "ipinfo.io":
        return {
            "ip": data.get("ip"),
            "country": data.get("country"),  # "US"
            "country_name": None,
            "region": data.get("region"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
        }
    if source == "ip-api.com":
        return {
            "ip": data.get("query"),
            "country": data.get("countryCode"),
            "country_name": data.get("country"),
            "region": data.get("regionName"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
        }
    if source == "ipapi.co":
        return {
            "ip": data.get("ip"),
            "country": data.get("country_code") or data.get("country"),
            "country_name": data.get("country_name"),
            "region": data.get("region"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
        }
    if source == "ifconfig.co":
        return {
            "ip": data.get("ip"),
            "country": data.get("country"),
            "country_name": data.get("country_name"),
            "region": data.get("region"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
        }
    return data


async def detect_egress(
    settings: Settings,
    *,
    via_proxy: bool = False,
) -> EgressInfo:
    """Detect current egress region.

    If `via_proxy=True`, route through `settings.upstream_proxy_url` to test
    the proxy egress (verifies the proxy actually lands in a premium region).
    """
    proxy = settings.upstream_proxy_url if via_proxy else None
    async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=15.0) as client:
        for name, url in _GEO_PROVIDERS:
            data = await _fetch_geo(client, name, url)
            if data and data.get("country"):
                country = (data["country"] or "").upper()
                is_premium = country in PREMIUM_REGIONS
                return EgressInfo(
                    ip=data.get("ip"),
                    country=country or None,
                    country_name=data.get("country_name"),
                    region=data.get("region"),
                    city=data.get("city"),
                    timezone=data.get("timezone"),
                    is_premium=is_premium,
                    proxy_active=bool(proxy),
                    source=name,
                )
    return EgressInfo(
        ip=None, country=None, country_name=None, region=None,
        city=None, timezone=None,
        is_premium=False, proxy_active=bool(proxy),
        source="unknown",
    )


async def verify_premium_egress(settings: Settings) -> dict[str, Any]:
    """Check both direct + proxy egress. Return diagnostic dict.

    Used by /admin/network and /api/health/egress endpoints.
    """
    direct = await detect_egress(settings, via_proxy=False)
    proxy_info: EgressInfo | None = None
    if settings.upstream_proxy_url:
        proxy_info = await detect_egress(settings, via_proxy=True)

    result: dict[str, Any] = {
        "deploy_mode": settings.deploy_mode,
        "direct": direct.as_dict(),
        "proxy": proxy_info.as_dict() if proxy_info else None,
        "premium_regions": sorted(PREMIUM_REGIONS),
        "egress_required_region": settings.egress_region_required,
        "egress_proxy_configured": bool(settings.upstream_proxy_url),
        "ok": direct.is_premium or (proxy_info is not None and proxy_info.is_premium),
    }
    result["recommended_action"] = _recommend(direct, proxy_info, settings)
    return result


def _recommend(direct: EgressInfo, proxy: EgressInfo | None, settings: Settings) -> str:
    if direct.is_premium:
        return "direct egress is premium — no proxy needed"
    if proxy and proxy.is_premium:
        return "proxy egress is premium — upstream requests will route correctly"
    if proxy and not proxy.is_premium:
        return (f"proxy egress country={proxy.country} is non-premium; "
                f"set FREEBUFF_EGRESS_PROXY_URL to a US/EU SOCKS5/HTTP proxy")
    if not settings.upstream_proxy_url:
        return (f"direct egress country={direct.country} is non-premium and no proxy configured; "
                f"set FREEBUFF_EGRESS_PROXY_URL=socks5://... or deploy in US region")
    return "configuration mismatch — check proxy URL"


def sync_detect_egress(settings: Settings, *, via_proxy: bool = False) -> EgressInfo:
    """Sync wrapper for non-async contexts (admin panel, CLI)."""
    return asyncio.run(detect_egress(settings, via_proxy=via_proxy))


def sync_verify_premium_egress(settings: Settings) -> dict[str, Any]:
    return asyncio.run(verify_premium_egress(settings))
