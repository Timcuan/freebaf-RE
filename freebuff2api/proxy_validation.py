"""Proxy egress validation against upstream privacy-signal hard-block.

Upstream commit CodebuffAI/codebuff#709 ("Block Freebuff VPN and proxy traffic")
hard-blocks any egress whose IPinfo privacy signals include:
  - vpn
  - proxy
  - tor
  - res_proxy

`hosting` alone is NOT hard-blocked (limited to DeepSeek Flash path).

This module probes the proxy egress through IPinfo (and fallbacks) and
classifies it as:
  - residential  (no privacy signals, or only `hosting`)  → safe
  - hard_blocked (vpn/proxy/tor/res_proxy)                 → reject
  - unknown      (probe failed)                            → warn

Used at startup to reject proxies that would get every account banned,
and at runtime via the /api/health/egress endpoint.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("freebuff2api.proxy_validation")

# IPinfo privacy signals that trigger upstream hard-block (commit #709).
HARD_BLOCKED_SIGNALS: frozenset[str] = frozenset({"vpn", "proxy", "tor", "res_proxy"})
# `hosting` alone → limited mode (DeepSeek Flash only), NOT hard-blocked.
LIMITED_SIGNALS: frozenset[str] = frozenset({"hosting"})

# Free privacy probes (no API key required).
# IPinfo gives the most reliable privacy data; fallbacks provide country only.
_IPINFO_URL = "https://ipinfo.io/json"
_IPINFO_FALLBACKS = (
    "https://ipapi.co/json/",
    "http://ip-api.com/json/?fields=status,countryCode,country,isp,org,as,proxy,hosting",
)


@dataclass(frozen=True)
class ProxyValidation:
    ok: bool
    country: str | None
    country_name: str | None
    ip: str | None
    isp: str | None
    org: str | None
    privacy_signals: tuple[str, ...]
    hard_blocked: bool
    limited: bool
    reason: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "country": self.country,
            "country_name": self.country_name,
            "ip": self.ip,
            "isp": self.isp,
            "org": self.org,
            "privacy_signals": list(self.privacy_signals),
            "hard_blocked": self.hard_blocked,
            "limited": self.limited,
            "reason": self.reason,
            "source": self.source,
        }


def _classify(signals: tuple[str, ...]) -> tuple[bool, bool, str]:
    """Return (hard_blocked, limited, reason) for a set of privacy signals."""
    hard = any(s in HARD_BLOCKED_SIGNALS for s in signals)
    limited = any(s in LIMITED_SIGNALS for s in signals)
    if hard:
        labels = sorted({s for s in signals if s in HARD_BLOCKED_SIGNALS})
        return True, False, f"hard-blocked by upstream: {','.join(labels)}"
    if limited:
        return False, True, "limited to DeepSeek Flash path (hosting signal)"
    return False, False, "clean residential egress"


def _parse_ipinfo(data: dict[str, Any]) -> ProxyValidation:
    privacy = data.get("privacy") or {}
    signals = tuple(sorted(privacy.get("signals") or []))
    hard, limited, reason = _classify(signals)
    country = (data.get("country") or "").upper() or None
    return ProxyValidation(
        ok=not hard,
        country=country,
        country_name=None,
        ip=data.get("ip"),
        isp=privacy.get("asn") or data.get("org"),
        org=data.get("org"),
        privacy_signals=signals,
        hard_blocked=hard,
        limited=limited,
        reason=reason,
        source="ipinfo.io",
    )


def _parse_ipapi(data: dict[str, Any]) -> ProxyValidation:
    # ipapi.co has no privacy signals; treat as unknown
    country = (data.get("country_code") or data.get("country") or "").upper() or None
    return ProxyValidation(
        ok=True,  # cannot confirm hard-block; assume ok
        country=country,
        country_name=data.get("country_name"),
        ip=data.get("ip"),
        isp=data.get("org"),
        org=data.get("org"),
        privacy_signals=(),
        hard_blocked=False,
        limited=False,
        reason="privacy signals unknown (ipapi.co has no privacy data)",
        source="ipapi.co",
    )


def _parse_ip_api(data: dict[str, Any]) -> ProxyValidation:
    # ip-api.com free endpoint has proxy/hosting flags
    signals: list[str] = []
    if data.get("proxy"):
        signals.append("proxy")
    if data.get("hosting"):
        signals.append("hosting")
    sig_tuple = tuple(sorted(signals))
    hard, limited, reason = _classify(sig_tuple)
    country = (data.get("countryCode") or "").upper() or None
    return ProxyValidation(
        ok=not hard,
        country=country,
        country_name=data.get("country"),
        ip=data.get("query"),
        isp=data.get("isp"),
        org=data.get("org"),
        privacy_signals=sig_tuple,
        hard_blocked=hard,
        limited=limited,
        reason=reason,
        source="ip-api.com",
    )


async def validate_proxy_egress(
    proxy_url: str | None,
    *,
    timeout: float = 12.0,
) -> ProxyValidation:
    """Probe egress through the given proxy and classify privacy risk.

    If `proxy_url` is None, probes the direct egress instead.
    """
    client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**client_kwargs) as client:
        # Try IPinfo first — gives real privacy signals
        try:
            resp = await client.get(_IPINFO_URL, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                return _parse_ipinfo(resp.json())
        except Exception as e:
            logger.debug("ipinfo probe failed: %s", e)

        # Fallback to ip-api.com (has proxy/hosting flags)
        try:
            resp = await client.get(_IPINFO_FALLBACKS[1], headers={"Accept": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "fail":
                    return _parse_ip_api(data)
        except Exception as e:
            logger.debug("ip-api probe failed: %s", e)

        # Last resort: ipapi.co (country only, no privacy data)
        try:
            resp = await client.get(_IPINFO_FALLBACKS[0], headers={"Accept": "application/json"})
            if resp.status_code == 200:
                return _parse_ipapi(resp.json())
        except Exception as e:
            logger.debug("ipapi probe failed: %s", e)

    return ProxyValidation(
        ok=False,
        country=None,
        country_name=None,
        ip=None,
        isp=None,
        org=None,
        privacy_signals=(),
        hard_blocked=False,
        limited=False,
        reason="all privacy probes failed",
        source="unknown",
    )


async def validate_egress_for_upstream(
    direct_proxy_url: str | None,
    *,
    premium_regions: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Full stealth validation: premium region + no hard-block privacy signals.

    Returns a dict with both direct + proxy validation, plus a combined `ok`
    flag that is True only when egress is premium AND not hard-blocked.
    """
    from .config import PREMIUM_REGIONS

    regions = premium_regions if premium_regions is not None else PREMIUM_REGIONS

    direct = await validate_proxy_egress(None)
    proxy_val: ProxyValidation | None = None
    if direct_proxy_url:
        proxy_val = await validate_proxy_egress(direct_proxy_url)

    def _ok(v: ProxyValidation) -> bool:
        if v.hard_blocked:
            return False
        if v.country and v.country not in regions:
            return False
        return True

    direct_ok = _ok(direct)
    proxy_ok = _ok(proxy_val) if proxy_val else None

    return {
        "direct": direct.as_dict(),
        "proxy": proxy_val.as_dict() if proxy_val else None,
        "premium_regions": sorted(regions),
        "direct_ok": direct_ok,
        "proxy_ok": proxy_ok,
        "ok": direct_ok or (proxy_ok is True),
        "recommendation": _recommend(direct, proxy_val, direct_ok, proxy_ok),
    }


def _recommend(
    direct: ProxyValidation,
    proxy: ProxyValidation | None,
    direct_ok: bool,
    proxy_ok: bool | None,
) -> str:
    if direct_ok:
        return "direct egress is premium + clean — no proxy needed"
    if proxy_ok is True:
        return "proxy egress is premium + clean — upstream requests will route correctly"
    if proxy and proxy.hard_blocked:
        return (
            f"proxy egress is HARD-BLOCKED by upstream ({proxy.reason}). "
            f"Switch to a residential US/CA proxy — commercial VPN/SOCKS5 services "
            f"are flagged as vpn/proxy/tor and will get every account banned."
        )
    if direct.hard_blocked:
        return (
            f"direct egress is HARD-BLOCKED ({direct.reason}). "
            f"Use a residential US/CA proxy via FREEBUFF_EGRESS_PROXY_URL."
        )
    if proxy and not proxy.country:
        return "proxy egress country unknown — privacy probes failed; verify manually"
    if proxy and proxy.country and not proxy_ok:
        return f"proxy egress country={proxy.country} is non-premium; use US/CA"
    return "configuration mismatch — check FREEBUFF_EGRESS_PROXY_URL"


def sync_validate_proxy_egress(proxy_url: str | None) -> ProxyValidation:
    return asyncio.run(validate_proxy_egress(proxy_url))
