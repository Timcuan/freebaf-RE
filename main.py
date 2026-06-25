"""Unified entry point — auto-detects deployment mode.

- Local: `python main.py` → uvicorn direct
- VPS:    systemd service or Docker container → same uvicorn direct
- Vercel: `api/index.py` exports app (no uvicorn)

Deployment mode is auto-detected from env (VERCEL, K_SERVICE, /.dockerenv, /proc/1/cmdline)
and can be overridden via `FREEBUFF_DEPLOY_MODE=local|vps|vercel|cloudrun|lambda`.

Egress region check runs on startup if `FREEBUFF_EGRESS_AUTO=true` (default).
Set `FREEBUFF_EGRESS_PROXY_URL=socks5://host:port` to force US/EU egress.
"""
from __future__ import annotations

import logging
import sys

import uvicorn

from freebuff2api.config import load_settings
from freebuff2api.egress_region import sync_verify_premium_egress


logger = logging.getLogger("freebuff2api.entry")


def _startup_egress_check() -> None:
    """Warn (not fail) if egress is non-premium and no proxy configured."""
    settings = load_settings()
    if not settings.egress_auto:
        return
    try:
        info = sync_verify_premium_egress(settings)
        direct = info["direct"]
        proxy = info.get("proxy")
        mode = info["deploy_mode"]
        if info["ok"]:
            print(f"[egress] mode={mode} direct={direct.get('country')} "
                  f"proxy={proxy.get('country') if proxy else None} OK", file=sys.stderr)
        else:
            print(f"[egress] mode={mode} direct={direct.get('country')} "
                  f"NON-PREMIUM — {info['recommended_action']}", file=sys.stderr)
            if settings.egress_region_required and not info["ok"]:
                print(f"[egress] FREEBUFF_EGRESS_REGION={settings.egress_region_required} "
                      f"required but not satisfied. Upstream will likely 409.", file=sys.stderr)
    except Exception as e:
        print(f"[egress] check failed: {e}", file=sys.stderr)


def main() -> None:
    settings = load_settings()
    print(f"[freebuff2api] deploy_mode={settings.deploy_mode} "
          f"host={settings.host} port={settings.port}", file=sys.stderr)
    print(f"[freebuff2api] models={len(__import__('freebuff2api.models', fromlist=['ALL_MODELS']).ALL_MODELS)} "
          f"tokens={len(settings.codebuff_tokens)} "
          f"proxy={bool(settings.upstream_proxy_url)}", file=sys.stderr)

    # Egress check only makes sense for local/VPS (Vercel = iad1 already US)
    if settings.deploy_mode in ("local", "vps", "cloudrun", "lambda"):
        _startup_egress_check()

    uvicorn.run(
        "freebuff2api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        timeout_keep_alive=75,  # > Vercel 60s idle; helps streaming
    )


if __name__ == "__main__":
    main()
