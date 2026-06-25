"""Stealth fingerprint generation matching upstream Codebuff CLI format.

Upstream recognizes three fingerprint formats (verified against
CodebuffAI/codebuff cli/src/utils/fingerprint.ts):

  - `enhanced-<sha256-base64url>`  hardware-based, deterministic per machine
  - `codebuff-cli-<8 base64url>`   legacy fallback, random per process
  - `codebuff-sdk-<13 base36>`     SDK default

Sending an unrecognized format (e.g. `fb-<hex>`) makes the account trivially
detectable as a non-official client. This module generates fingerprints in
the legacy `codebuff-cli-` format — random, no hardware dependency, valid
upstream. Each (account, login session) gets a stable fingerprint persisted
to disk so the same account reuses the same fingerprint across runs (mirrors
the CLI's `cachedFingerprintPromise` behavior).

User-Agent: upstream CLI uses `codebuff/<version>`. We default to the latest
npm release (1.0.682, June 2026) but allow override via env.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Latest verified CLI version (npm codebuff@1.0.682, June 2026).
# Override via FREEBUFF_CLI_VERSION env to track upstream bumps.
DEFAULT_CLI_VERSION = "1.0.682"
DEFAULT_CLI_USER_AGENT = f"codebuff/{DEFAULT_CLI_VERSION}"

# Legacy fingerprint format: codebuff-cli-<8 chars base64url>
# Matches cli/src/utils/fingerprint.ts calculateLegacyFingerprint()
LEGACY_PREFIX = "codebuff-cli-"
LEGACY_SUFFIX_LEN = 8


def generate_legacy_fingerprint() -> str:
    """Generate a legacy-format fingerprint (codebuff-cli-XXXXXXXX).

    Random 6 bytes → base64url → first 8 chars. Mirrors upstream fallback
    path exactly.
    """
    suffix = base64.urlsafe_b64encode(secrets.token_bytes(6)).decode("ascii")[:LEGACY_SUFFIX_LEN]
    return f"{LEGACY_PREFIX}{suffix}"


def generate_sdk_fingerprint() -> str:
    """Generate an SDK-format fingerprint (codebuff-sdk-<13 base36>).

    Matches @codebuff/sdk CodebuffClient default fingerprintId.
    Used when emulating SDK-based API access rather than CLI.
    """
    suffix = secrets.token_hex(8)  # 16 hex chars; SDK uses base36 substring(2,15)
    return f"codebuff-sdk-{suffix[:13]}"


def is_valid_fingerprint(fp: str | None) -> bool:
    """Check if a fingerprint matches an upstream-recognized format."""
    if not fp or not isinstance(fp, str):
        return False
    if fp.startswith("enhanced-"):
        return len(fp) > len("enhanced-")
    if fp.startswith(LEGACY_PREFIX):
        return len(fp) == len(LEGACY_PREFIX) + LEGACY_SUFFIX_LEN
    if fp.startswith("codebuff-sdk-"):
        return len(fp) > len("codebuff-sdk-")
    return False


@dataclass
class AccountFingerprint:
    account_id: str  # token-derived identifier (sha256 first 16)
    fingerprint_id: str
    user_agent: str
    created_at: float
    last_used_at: float


def _account_id_from_token(token: str) -> str:
    """Derive a stable identifier from a token (sha256 first 16 hex).

    Used as the key for per-account fingerprint persistence — same token
    always maps to the same fingerprint file, mirroring CLI behavior where
    the fingerprint is cached per machine.
    """
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def fingerprint_store_path() -> Path:
    override = os.getenv("FREEBUFF_FINGERPRINT_STORE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "freebaf-re" / "fingerprints.json"


def load_fingerprint_store() -> dict[str, dict[str, Any]]:
    path = fingerprint_store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_fingerprint_store(store: dict[str, dict[str, Any]]) -> Path:
    path = fingerprint_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def get_or_create_fingerprint(
    token: str,
    *,
    user_agent: str | None = None,
    persist: bool = True,
) -> AccountFingerprint:
    """Return a stable fingerprint for the given token.

    First call for a token generates a fresh legacy-format fingerprint and
    persists it. Subsequent calls reuse the stored fingerprint — mirrors
    the CLI's process-wide fingerprint cache but extended across runs.

    This is critical for stealth: a real CLI sends the same fingerprint for
    every request within a session and across sessions on the same machine.
    Rotating fingerprints per-request is a strong abuse signal upstream.
    """
    ua = user_agent or os.getenv("FREEBUFF_CLI_USER_AGENT", DEFAULT_CLI_USER_AGENT)
    account_id = _account_id_from_token(token)
    store = load_fingerprint_store() if persist else {}
    entry = store.get(account_id)
    if entry and is_valid_fingerprint(entry.get("fingerprint_id")):
        entry["last_used_at"] = time.time()
        if persist:
            store[account_id] = entry
            save_fingerprint_store(store)
        return AccountFingerprint(
            account_id=account_id,
            fingerprint_id=entry["fingerprint_id"],
            user_agent=entry.get("user_agent") or ua,
            created_at=float(entry.get("created_at", 0)),
            last_used_at=entry["last_used_at"],
        )
    fp = generate_legacy_fingerprint()
    now = time.time()
    entry = {
        "account_id": account_id,
        "fingerprint_id": fp,
        "user_agent": ua,
        "created_at": now,
        "last_used_at": now,
    }
    if persist:
        store[account_id] = entry
        save_fingerprint_store(store)
    return AccountFingerprint(
        account_id=account_id,
        fingerprint_id=fp,
        user_agent=ua,
        created_at=now,
        last_used_at=now,
    )


def rotate_fingerprint(token: str, *, persist: bool = True) -> AccountFingerprint:
    """Force-generate a new fingerprint for a token (e.g. after a ban)."""
    account_id = _account_id_from_token(token)
    fp = generate_legacy_fingerprint()
    ua = os.getenv("FREEBUFF_CLI_USER_AGENT", DEFAULT_CLI_USER_AGENT)
    now = time.time()
    entry = {
        "account_id": account_id,
        "fingerprint_id": fp,
        "user_agent": ua,
        "created_at": now,
        "last_used_at": now,
    }
    if persist:
        store = load_fingerprint_store()
        store[account_id] = entry
        save_fingerprint_store(store)
    return AccountFingerprint(
        account_id=account_id,
        fingerprint_id=fp,
        user_agent=ua,
        created_at=now,
        last_used_at=now,
    )


def clear_fingerprint(token: str) -> bool:
    """Remove a token's fingerprint entry. Returns True if removed."""
    store = load_fingerprint_store()
    account_id = _account_id_from_token(token)
    if account_id not in store:
        return False
    store.pop(account_id)
    save_fingerprint_store(store)
    return True


def cli_user_agent() -> str:
    """Resolve the CLI User-Agent string from env or default."""
    return os.getenv("FREEBUFF_CLI_USER_AGENT", DEFAULT_CLI_USER_AGENT)
