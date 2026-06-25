"""Freebuff/Codebuff account login + token harvester (CLI).

Codebuff CLI stores credentials at ~/.config/codebuff/credentials.json with
a single `default` profile — logging in a second account overwrites the first,
which forces a logout-relogin cycle every time you want to use a different
account. This script bypasses that limitation:

  - generates a fresh fingerprint per login
  - polls the upstream auth status endpoint for the authToken
  - appends the resulting token to a local token store (NOT credentials.json)
  - supports N accounts without ever touching the CLI's credential file
  - optional --write-env appends to .env FREEBUFF_TOKEN (comma-joined)

Usage:
    python scripts/login.py                    # interactive, freebuff.com
    python scripts/login.py --codebuff         # use codebuff.com instead
    python scripts/login.py --write-env        # append to .env FREEBUFF_TOKEN
    python scripts/login.py --list             # list stored tokens
    python scripts/login.py --remove INDEX     # remove a stored token by index
    python scripts/login.py --verify INDEX     # verify a stored token still works
    python scripts/login.py --export           # print FREEBUFF_TOKEN=tok1,tok2,...

Token store: ~/.config/freebaf-re/tokens.json (0600) by default
(override with FREEBUFF_TOKEN_STORE env var).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Allow running as a script without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freebuff2api import login_flow  # noqa: E402


@dataclass
class StoredToken:
    index: int
    user_id: str | None
    email: str | None
    name: str | None
    auth_token: str
    fingerprint_id: str
    fingerprint_hash: str
    added_at: float
    source: str  # "freebuff" | "codebuff"


def token_store_path() -> Path:
    override = os.getenv("FREEBUFF_TOKEN_STORE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "freebaf-re" / "tokens.json"


def load_store() -> list[dict[str, Any]]:
    path = token_store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_store(entries: list[dict[str, Any]]) -> Path:
    path = token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def append_env(token: str) -> Path:
    env_path = Path.cwd() / ".env"
    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("FREEBUFF_TOKEN="):
                existing = line[len("FREEBUFF_TOKEN="):].strip()
                joined = ",".join([t for t in (existing.split(",") if existing else []) if t and t != token])
                joined = ",".join([*([joined] if joined else []), token])
                lines.append(f"FREEBUFF_TOKEN={joined}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"FREEBUFF_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def cmd_login(args: argparse.Namespace) -> int:
    mode = "codebuff" if args.codebuff else "freebuff"
    code_url, _ = login_flow.endpoints_for(mode)
    print(f"[mode] {mode}  ({code_url.rsplit('/api', 1)[0]})")

    print("[1/3] requesting auth code...")
    import asyncio

    start = asyncio.run(login_flow.start_login(mode))
    print(f"      fingerprintId={start.fingerprint_id}")
    print(f"      fingerprintHash={start.fingerprint_hash[:16]}...  expiresAt={start.expires_at}")

    print("\n[2/3] open this URL in your browser and sign in:")
    print(f"      {start.login_url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(start.login_url)
        except Exception:
            pass

    print("[3/3] polling for authToken (timeout 5min)...")
    try:
        user = asyncio.run(login_flow.poll_login(start, mode=mode))
    except TimeoutError as e:
        print(f"[err] {e}")
        return 2

    token = user.auth_token
    entries = load_store()
    if any(e.get("auth_token") == token for e in entries):
        idx = next(i for i, e in enumerate(entries) if e.get("auth_token") == token)
        print(f"\n[skip] token already in store (index {idx})")
        return 0

    stored = StoredToken(
        index=len(entries),
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        auth_token=token,
        fingerprint_id=start.fingerprint_id,
        fingerprint_hash=start.fingerprint_hash,
        added_at=time.time(),
        source=mode,
    )
    entries.append(asdict(stored))
    path = save_store(entries)

    print("\n=== success ===")
    print(f"  index  : {stored.index}")
    print(f"  id     : {stored.user_id}")
    print(f"  name   : {stored.name}")
    print(f"  email  : {stored.email}")
    print(f"  source : {stored.source}")
    print(f"  token  : {token[:24]}...{token[-8:]}")
    print(f"  store  : {path}")

    print("\n[verify] testing token against codebuff.com/api/v1/freebuff/session ...")
    ok, info = login_flow.verify_token_sync(token)
    print(f"         {'OK' if ok else 'FAIL'} — {info}")
    if not ok:
        print("         token did NOT authenticate — remove it with --remove {index}")

    if args.write_env:
        env_path = append_env(token)
        print(f"\n[env] appended token to FREEBUFF_TOKEN in {env_path}")
        print(f"      restart the gateway to load the expanded account pool")
    else:
        print("\n(tip: rerun with --write-env to append to .env FREEBUFF_TOKEN)")
        print("(tip: run again to add another account — previous tokens are preserved)")

    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    entries = load_store()
    if not entries:
        print("(no tokens stored)")
        print(f"  store path: {token_store_path()}")
        return 0
    print(f"store: {token_store_path()}")
    print(f"count: {len(entries)}\n")
    for i, e in enumerate(entries):
        token = e.get("auth_token", "")
        masked = f"{token[:12]}...{token[-6:]}" if len(token) > 20 else token
        print(f"  [{i}] {e.get('email') or e.get('user_id') or 'unknown'}")
        print(f"      name   : {e.get('name')}")
        print(f"      source : {e.get('source')}")
        print(f"      token  : {masked}")
        print(f"      added  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.get('added_at', 0)))}")
    print(f"\nTo use all {len(entries)} accounts as a pool, set in .env:")
    joined = ",".join(e["auth_token"] for e in entries if e.get("auth_token"))
    print(f"  FREEBUFF_TOKEN={joined[:60]}...{joined[-20:] if len(joined) > 80 else ''}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    entries = load_store()
    if args.index < 0 or args.index >= len(entries):
        print(f"[err] index {args.index} out of range (0..{len(entries) - 1})")
        return 1
    removed = entries.pop(args.index)
    for i, e in enumerate(entries):
        e["index"] = i
    save_store(entries)
    print(f"[ok] removed index {args.index}: {removed.get('email') or removed.get('user_id')}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    entries = load_store()
    if args.index < 0 or args.index >= len(entries):
        print(f"[err] index {args.index} out of range (0..{len(entries) - 1})")
        return 1
    e = entries[args.index]
    token = e.get("auth_token", "")
    print(f"[verify] index {args.index} ({e.get('email') or e.get('user_id')})")
    ok, info = login_flow.verify_token_sync(token)
    print(f"         {'OK' if ok else 'FAIL'} — {info}")
    return 0 if ok else 2


def cmd_export(_args: argparse.Namespace) -> int:
    entries = load_store()
    tokens = [e["auth_token"] for e in entries if e.get("auth_token")]
    if not tokens:
        print("(no tokens stored)")
        return 1
    print("FREEBUFF_TOKEN=" + ",".join(tokens))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freebuff/Codebuff multi-account login + token harvester",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_login = sub.add_parser("login", help="Login a new account (default action)")
    p_login.add_argument("--codebuff", action="store_true", help="Use codebuff.com instead of freebuff.com")
    p_login.add_argument("--write-env", action="store_true", help="Append token to .env FREEBUFF_TOKEN")
    p_login.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    p_login.set_defaults(func=cmd_login)

    p_list = sub.add_parser("list", help="List stored tokens")
    p_list.set_defaults(func=cmd_list)

    p_remove = sub.add_parser("remove", help="Remove a stored token by index")
    p_remove.add_argument("index", type=int)
    p_remove.set_defaults(func=cmd_remove)

    p_verify = sub.add_parser("verify", help="Verify a stored token still works")
    p_verify.add_argument("index", type=int)
    p_verify.set_defaults(func=cmd_verify)

    p_export = sub.add_parser("export", help="Print FREEBUFF_TOKEN=token1,token2,... for .env")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    if args.cmd is None:
        args.codebuff = False
        args.write_env = "--write-env" in sys.argv
        args.no_browser = "--no-browser" in sys.argv
        return cmd_login(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
