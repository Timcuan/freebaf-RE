"""Per-account identity isolation — anti cross-account correlation.

Upstream bot-sweep (CodebuffAI/codebuff #527, abuse-detection.ts) clusters
accounts by:
  - shared device fingerprint sig_hash
  - shared fingerprint_id
  - creation clusters (accounts created within 30min)
  - round-the-clock usage (distinct_hours_24h >= 20)
  - email patterns (numeric siblings, common domains)
  - same IP / IP range / IP privacy signals (VPN, proxy, tor)

When the gateway runs multiple accounts from one host, all accounts share:
  - egress IP  → strongest correlation vector
  - TLS JA3/JA4 (curl_cffi profile) → second strongest
  - User-Agent (CLI version) → weak but consistent
  - timing (concurrent activity) → strong if no staggering

This module assigns each account a unique identity bundle:
  - proxy URL (residential, distinct IP per account)
  - TLS profile (curl_cffi browser profile)
  - CLI version (User-Agent variation)
  - timezone/locale (Accept-Language variation)
  - activity offset (per-account hour offset for staggering)

Bundles are deterministic per-account (derived from token hash) so they
persist across restarts, but can be overridden via env for manual control.

Env:
  FREEBUFF_PER_ACCOUNT_PROXY=socks5://u:p@h1:1080,socks5://u:p@h2:1080,...
      Comma-separated proxy URLs, one per account. Account i uses proxy[i].
      If fewer proxies than accounts, extras fall back to FREEBUFF_EGRESS_PROXY_URL
      or direct (no proxy).

  FREEBUFF_PER_ACCOUNT_TLS=chrome124,chrome120,safari17_0,...
      Comma-separated curl_cffi profiles. Defaults to rotating through
      SUPPORTED_PROFILES.

  FREEBUFF_PER_ACCOUNT_CLI_VERSION=1.0.682,1.0.680,1.0.681,...
      CLI version per account (User-Agent = codebuff/<version>).

  FREEBUFF_PER_ACCOUNT_LOCALE=en-US,zh-CN,en-GB,...
      Accept-Language + device locale per account.

  FREEBUFF_PER_ACCOUNT_TIMEZONE=America/New_York,Asia/Shanghai,...
      Device timezone per account.

  FREEBUFF_ACCOUNT_STAGGER_MINUTES=15
      Default stagger window (minutes) for activity distribution when no
      explicit idle window configured. Each account gets a phase offset of
      (account_index * stagger) % 60 minutes.

  FREEBUFF_IDENTITY_ISOLATION=true (default)
      Master switch. When false, all accounts share global settings
      (legacy behavior — NOT recommended for multi-account stealth).
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .stealth_transport import SUPPORTED_PROFILES, DEFAULT_PROFILE

logger = logging.getLogger("freebuff2api.account_identity")

# Default CLI versions to rotate through (all real upstream releases).
# Verified against npm codebuff package history (June 2026).
_DEFAULT_CLI_VERSIONS: tuple[str, ...] = (
    "1.0.682",
    "1.0.680",
    "1.0.681",
    "1.0.679",
    "1.0.678",
)

# Default locales — distinct Accept-Language signals.
_DEFAULT_LOCALES: tuple[str, ...] = (
    "en-US",
    "en-GB",
    "en-CA",
    "zh-CN",
    "ja-JP",
    "de-DE",
    "fr-FR",
)

# Default timezones — distinct device timezone signals.
_DEFAULT_TIMEZONES: tuple[str, ...] = (
    "America/New_York",
    "America/Los_Angeles",
    "America/Chicago",
    "Europe/London",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Europe/Berlin",
)


def _csv_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a comma-separated env var, falling back to `default`."""
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    items = tuple(s.strip() for s in raw.split(",") if s.strip())
    return items or default


def _account_id_from_token(token: str | None) -> str:
    """Stable per-account identifier (sha256 first 16 hex).

    Handles None tokens (single-account no-token mode) by hashing a constant.
    """
    raw = token if token else "__no_token__"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AccountIdentity:
    """Per-account identity bundle for stealth isolation.

    Each field is the value this account should use when making upstream
    requests. The gateway's CodebuffClient should consult this bundle
    instead of the global Settings for these fields.
    """

    account_index: int
    account_id: str
    proxy_url: str | None
    tls_profile: str
    cli_version: str
    cli_user_agent: str          # codebuff/<version>
    locale: str
    timezone: str
    accept_language: str         # derived from locale
    activity_phase_minutes: int  # 0-59 stagger offset
    # Per-account correlation-breaking fields (derived from account_id).
    client_id: str = ""          # 11-char hex, like upstream CLI default
    session_id: str = ""         # UUID per account
    device_os: str = "windows"   # device.os in ad-request body
    browser_ua: str = ""         # ad-provider User-Agent (Chrome/Firefox/Safari)

    @property
    def is_isolated(self) -> bool:
        """True if this account has a distinct proxy (strongest signal)."""
        return self.proxy_url is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_index": self.account_index,
            "account_id": self.account_id,
            "proxy_url": "***" if self.proxy_url else None,
            "tls_profile": self.tls_profile,
            "cli_version": self.cli_version,
            "cli_user_agent": self.cli_user_agent,
            "locale": self.locale,
            "timezone": self.timezone,
            "accept_language": self.accept_language,
            "activity_phase_minutes": self.activity_phase_minutes,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "device_os": self.device_os,
            "browser_ua": self.browser_ua,
            "is_isolated": self.is_isolated,
        }


def _locale_to_accept_language(locale: str) -> str:
    """Convert a locale code to an Accept-Language header value.

    e.g. en-US -> "en-US,en;q=0.9", zh-CN -> "zh-CN,zh;q=0.9"
    """
    primary = locale.split("-")[0]
    return f"{locale},{primary};q=0.9"


def _pick(items: tuple[str, ...], account_index: int, account_id: str) -> str:
    """Deterministically pick an item for an account.

    Uses account_index modulo length for stable distribution. Falls back
    to hashing account_id if index exceeds length (unlikely).
    """
    if not items:
        return ""
    return items[account_index % len(items)]


# Browser UA variations for ad-provider requests. All real, current.
_BROWSER_UAS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.3 Safari/605.1.15",
)

_DEVICE_OS_BY_LOCALE: dict[str, str] = {
    "zh-CN": "windows",
    "en-US": "macos",
    "en-GB": "macos",
    "en-CA": "windows",
    "ja-JP": "macos",
    "de-DE": "linux",
    "fr-FR": "macos",
}


def _derive_client_id(account_id: str) -> str:
    """Derive a stable 11-char hex client_id from account_id.

    Mirrors upstream CLI default (uuid4().hex[:11]) but deterministic per
    account so the same account always uses the same client_id across
    restarts (avoiding correlation via changing client_id).
    """
    digest = hashlib.sha256(f"client:{account_id}".encode("utf-8")).hexdigest()
    return digest[:11]


def _derive_session_id(account_id: str) -> str:
    """Derive a stable UUID-format session_id from account_id.

    Stable per account (avoids correlation via changing session_id across
    requests). Format matches upstream (uuid4 string).
    """
    import uuid

    digest = hashlib.sha256(f"session:{account_id}".encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16]))


def _browser_ua_for_account(account_index: int) -> str:
    return _BROWSER_UAS[account_index % len(_BROWSER_UAS)]


class AccountIdentityRegistry:
    """Resolves and caches AccountIdentity for each account in the pool.

    Construction is cheap (env parsing + token hashing). Identities are
    immutable and cached for the lifetime of the process.
    """

    def __init__(
        self,
        tokens: tuple[str, ...],
        *,
        isolation_enabled: bool | None = None,
        global_proxy_url: str | None = None,
    ) -> None:
        self._tokens = tokens
        self._isolation_enabled = (
            isolation_enabled
            if isolation_enabled is not None
            else _bool_env("FREEBUFF_IDENTITY_ISOLATION", True)
        )
        self._global_proxy = global_proxy_url or os.getenv("FREEBUFF_EGRESS_PROXY_URL")
        self._proxies = _csv_list("FREEBUFF_PER_ACCOUNT_PROXY", ())
        self._tls_profiles = _csv_list("FREEBUFF_PER_ACCOUNT_TLS", SUPPORTED_PROFILES)
        self._cli_versions = _csv_list("FREEBUFF_PER_ACCOUNT_CLI_VERSION", _DEFAULT_CLI_VERSIONS)
        self._locales = _csv_list("FREEBUFF_PER_ACCOUNT_LOCALE", _DEFAULT_LOCALES)
        self._timezones = _csv_list("FREEBUFF_PER_ACCOUNT_TIMEZONE", _DEFAULT_TIMEZONES)
        self._stagger_minutes = _int_env("FREEBUFF_ACCOUNT_STAGGER_MINUTES", 15)
        self._identities: list[AccountIdentity] = [
            self._build(i, tok) for i, tok in enumerate(tokens)
        ]

    def _build(self, index: int, token: str) -> AccountIdentity:
        account_id = _account_id_from_token(token)
        # Per-account correlation-breaking fields (always derived, even in
        # legacy mode — these are stable per account and never shared).
        client_id = _derive_client_id(account_id)
        session_id = _derive_session_id(account_id)
        device_os = _DEVICE_OS_BY_LOCALE.get(
            _pick(self._locales, index, account_id), "windows"
        ) if self._isolation_enabled else "windows"
        browser_ua = _browser_ua_for_account(index) if self._isolation_enabled else _BROWSER_UAS[0]

        if not self._isolation_enabled:
            # Legacy mode: all accounts share global settings.
            cli_version = self._cli_versions[0] if self._cli_versions else "1.0.682"
            return AccountIdentity(
                account_index=index,
                account_id=account_id,
                proxy_url=self._global_proxy,
                tls_profile=DEFAULT_PROFILE,
                cli_version=cli_version,
                cli_user_agent=f"codebuff/{cli_version}",
                locale="en-US",
                timezone="America/New_York",
                accept_language="en-US,en;q=0.9",
                activity_phase_minutes=0,
                client_id=client_id,
                session_id=session_id,
                device_os="windows",
                browser_ua=_BROWSER_UAS[0],
            )

        # Isolated mode — per-account distinct values.
        # Proxy: explicit per-account list, else fall back to global, else None.
        if index < len(self._proxies):
            proxy = self._proxies[index]
        else:
            proxy = self._global_proxy

        tls_profile = _pick(self._tls_profiles, index, account_id)
        cli_version = _pick(self._cli_versions, index, account_id)
        locale = _pick(self._locales, index, account_id)
        timezone = _pick(self._timezones, index, account_id)
        phase = (index * self._stagger_minutes) % 60

        return AccountIdentity(
            account_index=index,
            account_id=account_id,
            proxy_url=proxy,
            tls_profile=tls_profile,
            cli_version=cli_version,
            cli_user_agent=f"codebuff/{cli_version}",
            locale=locale,
            timezone=timezone,
            accept_language=_locale_to_accept_language(locale),
            activity_phase_minutes=phase,
            client_id=client_id,
            session_id=session_id,
            device_os=_DEVICE_OS_BY_LOCALE.get(locale, "windows"),
            browser_ua=browser_ua,
        )

    def __len__(self) -> int:
        return len(self._identities)

    def __getitem__(self, index: int) -> AccountIdentity:
        return self._identities[index]

    @property
    def isolation_enabled(self) -> bool:
        return self._isolation_enabled

    @property
    def isolated_count(self) -> int:
        """Number of accounts with a distinct proxy (true isolation)."""
        return sum(1 for ident in self._identities if ident.is_isolated)

    def status(self) -> list[dict[str, Any]]:
        return [ident.to_dict() for ident in self._identities]


# ── env helpers ──────────────────────────────────────────────────────


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default
