"""Z.ai provider — free GLM-4.7-Flash + GLM 5.2 via account-pool token rotation.

RE findings (verified 2026-06-26 from docs.z.ai/guides/overview/pricing):

Z.ai pricing (per 1M tokens):
- GLM-5.2: $1.40 input / $4.40 output (berbayar)
- GLM-5.1: $1.40 input / $4.40 output (berbayar)
- GLM-4.7-Flash: FREE FREE FREE FREE (input, cached, output semua gratis)
- GLM-4.5-Flash: FREE FREE FREE FREE
- GLM-4.7-FlashX: $0.07 / $0.40 (cheap)
- GLM-4.5-Air: $0.20 / $1.10

Free entry points:
1. New account = 20M tokens free resource package (sekaligus)
2. GLM-4.7-Flash = free tanpa batas (selama Z.ai promo berlaku)
3. GLM Coding Plan trial = free daily quota (Lite $3-6/mo after)

Endpoint (OpenAI-compatible):
  POST https://api.z.ai/api/paas/v4/chat/completions
  Headers: Authorization: Bearer {api_key}, Accept-Language: en-US,en
  Body: standard OpenAI chat completions
  Response: standard OpenAI format (streaming + non-streaming)

Strategy for truly unlimited GLM 5.2 free:
1. GLM-4.7-Flash sebagai default coding agent — free selamanya
   (untuk coding tasks, Flash sudah cukup mumpuni per Z.ai docs)
2. GLM 5.2 via pool rotasi:
   - N akun Z.ai × 20M token free = 20N M tokens free total
   - Setelah habis, fallback ke GLM-4.7-Flash (free) atau Codebuff unleash
3. Account pool: track per-account token usage, rotate saat habis

Config (env):
- FREEBUFF_ZAI_API_KEYS=key1,key2,key3   (comma-separated, N accounts)
- FREEBUFF_ZAI_DEFAULT_MODEL=glm-4.7-flash  (free default)
- FREEBUFF_ZAI_GLM52_BUDGET_TOKENS=20000000 (20M free per account)
- FREEBUFF_ZAI_FALLBACK_TO_CODEBUFF=true
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import Settings

logger = logging.getLogger("freebuff2api.zai")

ZAI_API_BASE = "https://api.z.ai/api/paas/v4"
ZAI_GLM_5_2 = "glm-5.2"
ZAI_GLM_5_1 = "glm-5.1"
ZAI_GLM_4_7_FLASH = "glm-4.7-flash"  # FREE tanpa batas
ZAI_GLM_4_5_FLASH = "glm-4.5-flash"  # FREE tanpa batas
ZAI_GLM_4_7_FLASHX = "glm-4.7-flashx"  # $0.07/$0.40 (cheap)

# Free models (gratis tanpa batas di Z.ai)
ZAI_FREE_MODELS: frozenset[str] = frozenset({
    ZAI_GLM_4_7_FLASH,
    ZAI_GLM_4_5_FLASH,
})

# Paid models dengan free token pool per account
ZAI_PAID_MODELS: frozenset[str] = frozenset({
    ZAI_GLM_5_2,
    ZAI_GLM_5_1,
    "glm-5",
    "glm-5-turbo",
    "glm-4.7",
    "glm-4.7-flashx",
})

DEFAULT_GLM52_BUDGET_TOKENS = 20_000_000  # 20M token free per new account


@dataclass
class ZaiAccountState:
    api_key: str
    # For paid models (GLM 5.2 etc) — track token usage against 20M free budget
    paid_tokens_used: int = 0
    paid_budget_tokens: int = DEFAULT_GLM52_BUDGET_TOKENS
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def paid_remaining(self) -> int:
        return max(0, self.paid_budget_tokens - self.paid_tokens_used)

    @property
    def paid_available(self) -> bool:
        return self.paid_remaining > 10_000  # leave buffer


class ZaiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class ZaiClient:
    """Z.ai OpenAI-compatible client with multi-account rotation.

    GLM-4.7-Flash dan GLM-4.5-Flash = free tanpa batas (no token cost).
    GLM-5.2 = berbayar, tapi 20M token free per new account → pool rotation.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._accounts: list[ZaiAccountState] = self._load_accounts()
        self._next_index = 0
        self._pool_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, read=None),
            follow_redirects=True,
            trust_env=False,
        )

    def _load_accounts(self) -> list[ZaiAccountState]:
        keys = [s.strip() for s in (self.settings.zai_api_keys or "").split(",") if s.strip()]
        budget = self.settings.zai_glm52_budget_tokens
        return [
            ZaiAccountState(api_key=k, paid_budget_tokens=budget)
            for k in keys
        ]

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    @property
    def total_paid_remaining(self) -> int:
        return sum(a.paid_remaining for a in self._accounts)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, account: ZaiAccountState) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {account.api_key}",
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
        }

    def _is_free_model(self, model: str) -> bool:
        return model.lower() in ZAI_FREE_MODELS

    async def _pick_account(self, needs_paid: bool) -> ZaiAccountState | None:
        """Pick account. Free models = any account. Paid = needs paid_available."""
        async with self._pool_lock:
            if not self._accounts:
                return None
            for _ in range(len(self._accounts)):
                idx = self._next_index % len(self._accounts)
                self._next_index += 1
                acc = self._accounts[idx]
                if not needs_paid or acc.paid_available:
                    return acc
            return None

    async def chat_completions(
        self,
        body: dict[str, Any],
        *,
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """OpenAI-compatible chat completions via Z.ai."""
        # Normalize model
        zai_body = {**body}
        requested = (zai_body.get("model") or "").lower()
        # Map aliases
        model_map = {
            "zai/glm-5.2": ZAI_GLM_5_2,
            "zai/glm-5.1": ZAI_GLM_5_1,
            "zai/glm-4.7-flash": ZAI_GLM_4_7_FLASH,
            "zai/glm-4.5-flash": ZAI_GLM_4_5_FLASH,
            "glm-5.2": ZAI_GLM_5_2,
            "glm-5.1": ZAI_GLM_5_1,
            "glm-4.7-flash": ZAI_GLM_4_7_FLASH,
            "glm-4.5-flash": ZAI_GLM_4_5_FLASH,
            "glm-4.7-flashx": ZAI_GLM_4_7_FLASHX,
        }
        zai_body["model"] = model_map.get(requested, requested)
        if not zai_body["model"]:
            zai_body["model"] = self.settings.zai_default_model or ZAI_GLM_4_7_FLASH

        chosen_model = zai_body["model"]
        is_free = self._is_free_model(chosen_model)
        zai_body["stream"] = stream

        account = await self._pick_account(needs_paid=not is_free)
        if account is None:
            raise ZaiError(
                "No Z.ai account available for paid model (all 20M free quotas exhausted). "
                "Register new account at https://open.bigmodel.cn or use glm-4.7-flash (free).",
                status_code=429,
            )

        url = f"{ZAI_API_BASE}/chat/completions"
        async with account._lock:
            account.request_count += 1

        try:
            if stream:
                return self._stream_response(account, url, zai_body, track_paid=not is_free)
            response = await self._client.post(
                url,
                json=zai_body,
                headers=self._headers(account),
                timeout=self.settings.request_timeout,
            )
            if response.status_code >= 400:
                async with account._lock:
                    account.error_count += 1
                    account.last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                raise ZaiError(
                    f"Z.ai API error: {response.text[:300]}",
                    status_code=response.status_code,
                )
            data = response.json()
            # Track paid token usage
            if not is_free:
                usage = data.get("usage", {})
                total = usage.get("total_tokens", 0)
                async with account._lock:
                    account.paid_tokens_used += total
            async with account._lock:
                account.success_count += 1
            return data
        except httpx.RequestError as e:
            async with account._lock:
                account.error_count += 1
                account.last_error = str(e)[:200]
            raise ZaiError(f"Z.ai network error: {e}", status_code=502) from e

    async def _stream_response(
        self,
        account: ZaiAccountState,
        url: str,
        body: dict[str, Any],
        *,
        track_paid: bool,
    ) -> AsyncIterator[bytes]:
        total_tokens = 0
        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers=self._headers(account),
                timeout=self.settings.request_timeout,
            ) as response:
                if response.status_code >= 400:
                    text = await response.aread()
                    async with account._lock:
                        account.error_count += 1
                        account.last_error = f"HTTP {response.status_code}"
                    raise ZaiError(
                        f"Z.ai stream error: {text[:300]!r}",
                        status_code=response.status_code,
                    )
                async for chunk in response.aiter_bytes():
                    if track_paid and b"usage" in chunk:
                        try:
                            import json
                            for line in chunk.decode(errors="replace").split("\n"):
                                if line.startswith("data: ") and "usage" in line:
                                    payload = json.loads(line[6:])
                                    u = payload.get("usage", {})
                                    total_tokens = u.get("total_tokens", total_tokens)
                        except Exception:
                            pass
                    yield chunk
            if track_paid and total_tokens > 0:
                async with account._lock:
                    account.paid_tokens_used += total_tokens
            async with account._lock:
                account.success_count += 1
        except httpx.RequestError as e:
            async with account._lock:
                account.error_count += 1
                account.last_error = str(e)[:200]
            raise ZaiError(f"Z.ai stream network error: {e}", status_code=502) from e

    def status(self) -> dict[str, Any]:
        return {
            "provider": "zai",
            "endpoint": ZAI_API_BASE,
            "free_models": sorted(ZAI_FREE_MODELS),
            "paid_models": sorted(ZAI_PAID_MODELS),
            "default_model": self.settings.zai_default_model or ZAI_GLM_4_7_FLASH,
            "accounts": [
                {
                    "index": i,
                    "api_key_masked": acc.api_key[:8] + "..." if acc.api_key else None,
                    "paid_tokens_used": acc.paid_tokens_used,
                    "paid_remaining": acc.paid_remaining,
                    "paid_budget": acc.paid_budget_tokens,
                    "paid_available": acc.paid_available,
                    "request_count": acc.request_count,
                    "success_count": acc.success_count,
                    "error_count": acc.error_count,
                    "last_error": acc.last_error,
                }
                for i, acc in enumerate(self._accounts)
            ],
            "total_paid_remaining": self.total_paid_remaining,
            "free_tier_per_account": DEFAULT_GLM52_BUDGET_TOKENS,
        }
