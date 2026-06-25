"""Freebuff/Codebuff OAuth-style login flow primitives.

Shared by:
  - scripts/login.py (CLI harvester)
  - admin POST /admin/api/login-flow/* (web UI direct login)

Upstream flow (verified against CodebuffAI/codebuff cli/src/login/login-flow.ts):
  1. POST {base}/api/auth/cli/code  body={"fingerprintId": "fb-<hex>"}
     -> {loginUrl, fingerprintHash, expiresAt}
  2. User opens loginUrl in a browser and signs in.
  3. GET {base}/api/auth/cli/status?fingerprintId=..&fingerprintHash=..&expiresAt=..
     -> 401 while pending, 200 {user: {id,email,name,authToken,...}} on success

The CLI stores only one `default` profile in credentials.json, which forces a
logout-relogin to switch accounts. This module never touches that file —
callers persist tokens themselves (token store / .env FREEBUFF_TOKEN pool).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from .stealth import generate_legacy_fingerprint, is_valid_fingerprint

BASE_FREEBUFF = "https://freebuff.com"
BASE_CODEBUFF = "https://www.codebuff.com"
VERIFY_URL = "https://www.codebuff.com/api/v1/freebuff/session"
# Upstream CLI uses `codebuff/<version>` (verified: codebuff@1.0.682, June 2026).
# Override via FREEBUFF_CLI_USER_AGENT env.
HTTP_USER_AGENT = "codebuff/1.0.682"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_TIMEOUT = 5 * 60


@dataclass
class LoginStartResult:
    fingerprint_id: str
    fingerprint_hash: str
    expires_at: int
    login_url: str


@dataclass
class LoginUser:
    user_id: str | None
    email: str | None
    name: str | None
    auth_token: str
    raw: dict[str, Any]


def endpoints_for(mode: str) -> tuple[str, str]:
    base = BASE_CODEBUFF if mode == "codebuff" else BASE_FREEBUFF
    return f"{base}/api/auth/cli/code", f"{base}/api/auth/cli/status"


def generate_fingerprint() -> str:
    """Generate an upstream-valid legacy-format fingerprint.

    Returns `codebuff-cli-<8 base64url chars>` — matches the official CLI's
    legacy fallback path exactly. The previous `fb-<16hex>` format was not
    recognized upstream and made accounts trivially detectable as
    non-official clients.
    """
    return generate_legacy_fingerprint()


def _sync_request_code(fingerprint_id: str, code_url: str) -> dict[str, Any]:
    body = json.dumps({"fingerprintId": fingerprint_id}).encode()
    req = urllib.request.Request(
        code_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _sync_poll_once(
    fingerprint_id: str,
    fingerprint_hash: str,
    expires_at: int,
    status_url: str,
) -> dict[str, Any] | None:
    qs = urllib.parse.urlencode(
        {
            "fingerprintId": fingerprint_id,
            "fingerprintHash": fingerprint_hash,
            "expiresAt": str(expires_at),
        }
    )
    url = f"{status_url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            user = data.get("user")
            if user and user.get("authToken"):
                return user
            return None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None
        e.read()
        raise


async def request_code(fingerprint_id: str, code_url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_sync_request_code, fingerprint_id, code_url)


async def start_login(mode: str = "freebuff") -> LoginStartResult:
    code_url, _ = endpoints_for(mode)
    fingerprint_id = generate_fingerprint()
    data = await request_code(fingerprint_id, code_url)
    return LoginStartResult(
        fingerprint_id=fingerprint_id,
        fingerprint_hash=data["fingerprintHash"],
        expires_at=int(data["expiresAt"]),
        login_url=data["loginUrl"],
    )


async def poll_login(
    start: LoginStartResult,
    mode: str = "freebuff",
    *,
    interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_POLL_TIMEOUT,
    should_continue: "Any | None" = None,
) -> LoginUser:
    _, status_url = endpoints_for(mode)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_continue is not None and not should_continue():
            raise asyncio.CancelledError("login polling cancelled")
        user = await asyncio.to_thread(
            _sync_poll_once,
            start.fingerprint_id,
            start.fingerprint_hash,
            start.expires_at,
            status_url,
        )
        if user is not None:
            return LoginUser(
                user_id=user.get("id"),
                email=user.get("email"),
                name=user.get("name"),
                auth_token=user["authToken"],
                raw=user,
            )
        await asyncio.sleep(interval)
    raise TimeoutError("login was not completed within the timeout window")


def verify_token_sync(token: str) -> tuple[bool, str]:
    req = urllib.request.Request(
        VERIFY_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode(errors="replace")
        if e.code in (401, 403):
            return False, f"HTTP {e.code} (token rejected): {body}"
        return True, f"HTTP {e.code} (auth ok, endpoint returned: {body})"
    except urllib.error.URLError as e:
        return False, f"network error: {e}"


async def verify_token(token: str) -> tuple[bool, str]:
    return await asyncio.to_thread(verify_token_sync, token)


def user_to_stored_dict(user: LoginUser, *, fingerprint_id: str, fingerprint_hash: str, source: str, added_at: float | None = None) -> dict[str, Any]:
    return {
        "index": 0,
        "user_id": user.user_id,
        "email": user.email,
        "name": user.name,
        "auth_token": user.auth_token,
        "fingerprint_id": fingerprint_id,
        "fingerprint_hash": fingerprint_hash,
        "added_at": added_at if added_at is not None else time.time(),
        "source": source,
    }
