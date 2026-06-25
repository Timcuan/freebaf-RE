"""Cloudflare Workers AI — free GLM 5.2 provider (10k neurons/day per account).

Source: https://developers.cloudflare.com/workers-ai/
Free tier: 10,000 neurons/day per Cloudflare account (no card required)
GLM 5.2 model: @cf/zai-org/glm-5.2 (also glm-5.2-fp8)

Cost in neurons:
- GLM 5.2: ~25 neurons per 1M input tokens, ~50 neurons per 1M output tokens
- 10,000 neurons/day ≈ 200-400M tokens/day free per account
- With account pool rotation: practically unlimited for agent use

This module implements:
1. OpenAI-compatible adapter for Cloudflare Workers AI
2. Multi-account pool rotation (CF_ACCOUNT_IDS + CF_API_TOKENS csv)
3. Daily neuron quota tracking per account
4. Failover when account exhausted
5. SSE streaming via CF's /v1/chat/completions endpoint

Config (env):
- FREEBUFF_CF_ACCOUNT_IDS=acct1,acct2,acct3       (comma-separated)
- FREEBUFF_CF_API_TOKENS=token1,token2,token3     (matched 1:1 with accounts)
- FREEBUFF_CF_NEURON_BUDGET_DAILY=9000             (safety margin, default 9000 of 10000)
- FREEBUFF_CF_FALLBACK_TO_CODEBUFF=true            (when CF exhausted, fall back)

API endpoint:
  POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions
  Headers: Authorization: Bearer {api_token}
  Body: standard OpenAI chat completions
  Response: standard OpenAI format (streaming + non-streaming)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import Settings

logger = logging.getLogger("freebuff2api.cloudflare_ai")

CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai"
CF_GLM_5_2_MODEL = "@cf/zai-org/glm-5.2"
CF_GLM_5_2_FP8_MODEL = "@cf/zai-org/glm-5.2-fp8"
CF_NEURONS_PER_DAY_FREE = 10_000
DEFAULT_NEURON_BUDGET = 9_000  # safety margin


@dataclass
class CfAccountState:
    account_id: str
    api_token: str
    neuron_used_today: int = 0
    last_reset_day: int = 0  # day-of-year
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset_if_new_day(self) -> None:
        today = time.gmtime().tm_yday
        if self.last_reset_day != today:
            self.neuron_used_today = 0
            self.last_reset_day = today

    @property
    def remaining_neurons(self) -> int:
        self.reset_if_new_day()
        return max(0, CF_NEURONS_PER_DAY_FREE - self.neuron_used_today)

    @property
    def is_available(self) -> bool:
        self.reset_if_new_day()
        return self.neuron_used_today < DEFAULT_NEURON_BUDGET


class CloudflareAIError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class CloudflareAIClient:
    """OpenAI-compatible client for Cloudflare Workers AI GLM 5.2."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._accounts: list[CfAccountState] = self._load_accounts()
        self._next_index = 0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, read=None),
            follow_redirects=True,
            trust_env=False,
        )
        self._pool_lock = asyncio.Lock()

    def _load_accounts(self) -> list[CfAccountState]:
        ids = [s.strip() for s in (self.settings.cf_account_ids or "").split(",") if s.strip()]
        tokens = [s.strip() for s in (self.settings.cf_api_tokens or "").split(",") if s.strip()]
        if len(ids) != len(tokens):
            logger.warning(
                "cf account/tokens count mismatch: ids=%s tokens=%s",
                len(ids), len(tokens),
            )
            # Zip with min length
            pairs = list(zip(ids, tokens))
        else:
            pairs = list(zip(ids, tokens))
        return [
            CfAccountState(account_id=aid, api_token=tok)
            for aid, tok in pairs
        ]

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    @property
    def total_remaining_neurons(self) -> int:
        return sum(acc.remaining_neurons for acc in self._accounts)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pick_account(self) -> CfAccountState | None:
        async with self._pool_lock:
            if not self._accounts:
                return None
            # Round-robin, skip exhausted
            for _ in range(len(self._accounts)):
                idx = self._next_index % len(self._accounts)
                self._next_index += 1
                acc = self._accounts[idx]
                acc.reset_if_new_day()
                if acc.is_available:
                    return acc
            return None

    def _estimate_neurons(self, input_tokens: int, output_tokens: int) -> int:
        # Rough estimate: 25 neurons/1M input + 50 neurons/1M output
        return (input_tokens * 25 + output_tokens * 50) // 1_000_000 + 1

    def _build_url(self, account_id: str, path: str) -> str:
        return f"{CF_API_BASE.format(account_id=account_id)}{path}"

    def _headers(self, account: CfAccountState) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {account.api_token}",
            "Content-Type": "application/json",
        }

    async def chat_completions(
        self,
        body: dict[str, Any],
        *,
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """OpenAI-compatible chat completions via CF Workers AI."""
        account = await self._pick_account()
        if account is None:
            raise CloudflareAIError(
                "All Cloudflare accounts exhausted daily neuron budget",
                status_code=429,
            )

        # Normalize model → GLM 5.2
        cf_body = {**body}
        requested = (cf_body.get("model") or "").lower()
        if "glm-5.2" in requested or "glm_5_2" in requested or "glm52" in requested:
            cf_body["model"] = CF_GLM_5_2_MODEL
        elif "fp8" in requested:
            cf_body["model"] = CF_GLM_5_2_FP8_MODEL
        else:
            cf_body["model"] = CF_GLM_5_2_MODEL  # default to GLM 5.2

        cf_body["stream"] = stream

        url = self._build_url(account.account_id, "/v1/chat/completions")

        async with account._lock:
            account.request_count += 1

        try:
            if stream:
                return self._stream_response(account, url, cf_body)
            response = await self._client.post(
                url,
                json=cf_body,
                headers=self._headers(account),
                timeout=self.settings.request_timeout,
            )
            if response.status_code >= 400:
                async with account._lock:
                    account.error_count += 1
                    account.last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                raise CloudflareAIError(
                    f"CF API error: {response.text[:300]}",
                    status_code=response.status_code,
                )
            data = response.json()
            # Track neuron usage from response
            usage = data.get("usage", {})
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            async with account._lock:
                account.neuron_used_today += self._estimate_neurons(in_tok, out_tok)
                account.success_count += 1
            return data
        except httpx.RequestError as e:
            async with account._lock:
                account.error_count += 1
                account.last_error = str(e)[:200]
            raise CloudflareAIError(f"CF network error: {e}", status_code=502) from e

    async def _stream_response(
        self,
        account: CfAccountState,
        url: str,
        body: dict[str, Any],
    ) -> AsyncIterator[bytes]:
        """Yield SSE bytes from CF, track usage on completion."""
        total_in = 0
        total_out = 0
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
                    raise CloudflareAIError(
                        f"CF stream error: {text[:300]!r}",
                        status_code=response.status_code,
                    )
                async for chunk in response.aiter_bytes():
                    # Parse usage from final SSE chunk if present
                    if b"usage" in chunk:
                        try:
                            line = chunk.decode(errors="replace")
                            if '"usage"' in line:
                                import json
                                # Extract usage from SSE data line
                                for l in line.split("\n"):
                                    if l.startswith("data: ") and "usage" in l:
                                        payload = json.loads(l[6:])
                                        u = payload.get("usage", {})
                                        total_in = u.get("prompt_tokens", total_in)
                                        total_out = u.get("completion_tokens", total_out)
                        except Exception:
                            pass
                    yield chunk
            async with account._lock:
                account.neuron_used_today += self._estimate_neurons(total_in, total_out)
                account.success_count += 1
        except httpx.RequestError as e:
            async with account._lock:
                account.error_count += 1
                account.last_error = str(e)[:200]
            raise CloudflareAIError(f"CF stream network error: {e}", status_code=502) from e

    def status(self) -> dict[str, Any]:
        return {
            "provider": "cloudflare_workers_ai",
            "model": CF_GLM_5_2_MODEL,
            "accounts": [
                {
                    "index": i,
                    "account_id": acc.account_id[:8] + "..." if acc.account_id else None,
                    "neurons_used_today": acc.neuron_used_today,
                    "remaining_neurons": acc.remaining_neurons,
                    "is_available": acc.is_available,
                    "request_count": acc.request_count,
                    "success_count": acc.success_count,
                    "error_count": acc.error_count,
                    "last_error": acc.last_error,
                }
                for i, acc in enumerate(self._accounts)
            ],
            "total_remaining_neurons": self.total_remaining_neurons,
            "free_tier_per_account": CF_NEURONS_PER_DAY_FREE,
            "neuron_budget_per_account": DEFAULT_NEURON_BUDGET,
        }
