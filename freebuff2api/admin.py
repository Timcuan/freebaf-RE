from __future__ import annotations

import datetime
import hmac
import json
import os
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .codebuff import CodebuffAccountPool, CodebuffClient, CodebuffError
from .config import DEFAULT_ADMIN_KEY, Settings, project_env_path, write_env_values
from .logging_config import get_buffered_logs
from .models import DEFAULT_MODEL, models_response
from .usage import ApiKeyRecord
from .usage_store import ApiKeyStore, RequestStore


COOKIE_NAME = "freebuff_admin_session"
COOKIE_MAX_AGE = 60 * 60 * 12
NO_STORE_HEADERS = {"Cache-Control": "no-store"}

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _admin_secret(settings: Settings) -> str | None:
    return settings.admin_key or settings.local_api_key


def _expected_login_key(settings: Settings) -> str | None:
    return settings.admin_key or settings.local_api_key


def _sign(secret: str, issued_at: str) -> str:
    return hmac.new(secret.encode("utf-8"), issued_at.encode("utf-8"), sha256).hexdigest()


def _cookie_value(secret: str) -> str:
    issued_at = str(int(time.time()))
    return f"{issued_at}.{_sign(secret, issued_at)}"


def _check_admin_auth(request: Request) -> None:
    if _is_admin_authenticated(request):
        return
    raise HTTPException(status_code=401, detail="Admin login required")


def _is_admin_authenticated(request: Request) -> bool:
    settings = _settings(request)
    secret = _admin_secret(settings)
    if not secret:
        return False
    raw = request.cookies.get(COOKIE_NAME) or ""
    try:
        issued_at, signature = raw.split(".", 1)
        issued_ts = int(issued_at)
    except ValueError:
        return False
    if int(time.time()) - issued_ts > COOKIE_MAX_AGE:
        return False
    return hmac.compare_digest(signature, _sign(secret, issued_at))


def _is_vercel() -> bool:
    return os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))


def _mask(value: str | None, *, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def _token_rows(settings: Settings) -> list[dict[str, Any]]:
    rows = []
    for index, token in enumerate(settings.codebuff_tokens, start=1):
        rows.append(
            {
                "index": index,
                "masked": _mask(token),
                "prefix": token[:8],
                "length": len(token),
            }
        )
    return rows


def _config_payload(settings: Settings) -> dict[str, Any]:
    using_default_admin_key = settings.admin_key == DEFAULT_ADMIN_KEY
    return {
        "environment": "vercel" if _is_vercel() else "local",
        "token_count": len(settings.codebuff_tokens),
        "tokens": _token_rows(settings),
        "api_key_configured": bool(settings.local_api_key),
        "api_key_masked": _mask(settings.local_api_key),
        "admin_key_configured": bool(settings.admin_key),
        "admin_key_masked": _mask(settings.admin_key),
        "using_default_admin_key": using_default_admin_key,
        "setup_complete": bool(settings.local_api_key and settings.codebuff_tokens and not using_default_admin_key),
        "debug": settings.debug,
        "log_level": settings.log_level,
        "proxy_enabled": settings.proxy_enabled,
        "proxy_url": _mask(settings.proxy_url, keep=10),
        "base_url": settings.codebuff_api_url,
        "port": settings.port,
    }


def _api_ok(data: dict[str, Any] | list[Any] | None = None, msg: str = "ok") -> dict[str, Any]:
    return {"code": 0, "msg": msg, "data": data or {}}


def _apply_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


async def _replace_accounts(request: Request, settings: Settings) -> None:
    old_accounts = request.app.state.accounts
    new_accounts = CodebuffAccountPool(settings)
    request.app.state.settings = settings
    request.app.state.accounts = new_accounts
    request.app.state.codebuff = new_accounts.default_client
    request.app.state.sessions = new_accounts.default_sessions
    await old_accounts.aclose()


def _tokens(settings: Settings) -> list[str]:
    return list(settings.codebuff_tokens)


async def _save_token_list(request: Request, tokens: list[str]) -> dict[str, Any]:
    clean_tokens = [item.strip() for item in tokens if item and item.strip()]
    token_value = ",".join(clean_tokens)
    old_settings = _settings(request)
    new_settings = replace(old_settings, codebuff_token=token_value or None)
    if not _is_vercel():
        write_env_values({"FREEBUFF_TOKEN": token_value or None})
    _apply_env({"FREEBUFF_TOKEN": token_value or None})
    await _replace_accounts(request, new_settings)
    return {
        **_config_payload(new_settings),
        "persisted": not _is_vercel(),
        "env": f"FREEBUFF_TOKEN={token_value}",
    }


@router.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=307)


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    html_path = Path(__file__).parent / "admin_static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"), headers=NO_STORE_HEADERS)


@router.post("/admin/api/login")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    key = str(body.get("key") or "")
    expected = _expected_login_key(_settings(request))
    if not expected:
        raise HTTPException(status_code=503, detail="Set FREEBUFF_ADMIN_KEY first")
    if not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    response = JSONResponse(_api_ok(_config_payload(_settings(request))))
    response.set_cookie(
        COOKIE_NAME,
        _cookie_value(_admin_secret(_settings(request)) or expected),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


def _region_from_payload(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    if source == "ipapi.co":
        return {
            "ip": payload.get("ip"),
            "country": payload.get("country_name") or payload.get("country"),
            "region": payload.get("region"),
            "city": payload.get("city"),
            "timezone": payload.get("timezone"),
            "org": payload.get("org"),
        }
    if source == "ipinfo.io":
        return {
            "ip": payload.get("ip"),
            "country": payload.get("country"),
            "region": payload.get("region"),
            "city": payload.get("city"),
            "timezone": payload.get("timezone"),
            "org": payload.get("org"),
        }
    return {
        "ip": payload.get("query"),
        "country": payload.get("country"),
        "region": payload.get("regionName") or payload.get("region"),
        "city": payload.get("city"),
        "timezone": payload.get("timezone"),
        "org": payload.get("isp") or payload.get("org"),
    }


async def _probe_region(settings: Settings) -> dict[str, Any]:
    probes = [
        ("ipapi.co", "https://ipapi.co/json/"),
        ("ipinfo.io", "https://ipinfo.io/json"),
        ("ip-api.com", "http://ip-api.com/json/?fields=status,message,query,country,regionName,city,timezone,isp,org"),
    ]
    errors: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0),
        follow_redirects=True,
        proxy=settings.upstream_proxy_url,
        trust_env=False,
    ) as client:
        for source, url in probes:
            started = time.perf_counter()
            try:
                response = await client.get(url, headers={"Accept": "application/json"})
                latency_ms = round((time.perf_counter() - started) * 1000)
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") == "fail":
                    raise ValueError(payload.get("message") or "region probe failed")
                return {
                    "ok": True,
                    "source": source,
                    "latency_ms": latency_ms,
                    **_region_from_payload(source, payload),
                }
            except Exception as error:
                errors.append({"source": source, "error": str(error)})
    return {"ok": False, "source": "unknown", "errors": errors}


async def _probe_url(client: httpx.AsyncClient, name: str, url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.get(url, headers={"Accept": "*/*"})
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "ok": response.status_code < 500,
            "status": response.status_code,
            "latency_ms": latency_ms,
        }
    except Exception as error:
        return {"name": name, "ok": False, "error": str(error)}
    return response


@router.post("/admin/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse(_api_ok())
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/admin/api/session")
async def session_status(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return _api_ok(
        {
            "authenticated": _is_admin_authenticated(request),
            "admin_key_configured": bool(settings.admin_key),
            "api_key_configured": bool(settings.local_api_key),
            "using_default_admin_key": settings.admin_key == DEFAULT_ADMIN_KEY,
        }
    )


@router.get("/admin/api/overview")
async def overview(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    settings = _settings(request)
    return _api_ok(
        {
            "status": "ok",
            "environment": "vercel" if _is_vercel() else "local",
            "account_count": request.app.state.accounts.account_count,
            "model_count": len(models_response()["data"]),
            "base_url": settings.codebuff_api_url,
            "debug": settings.debug,
            "log_level": settings.log_level,
        }
    )


@router.get("/admin/api/config")
async def config(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    return _api_ok(_config_payload(_settings(request)))


@router.get("/admin/api/env")
async def env_content(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    env_path = project_env_path()
    vercel = _is_vercel()
    content = ""
    exists = env_path.exists()
    if exists and not vercel:
        content = env_path.read_text(encoding="utf-8")
    return _api_ok(
        {
            "environment": "vercel" if vercel else "local",
            "path": str(env_path),
            "exists": exists,
            "content": content,
            "editable": not vercel,
            "message": (
                "Vercel deployments cannot persist .env changes from the running service; edit variables in Vercel project Settings -> Environment Variables, then redeploy."
                if vercel
                else "Local service reads .env from the project root. Token/API Key changes saved via the admin panel are written back to this file."
            ),
        }
    )


@router.get("/admin/api/models")
async def admin_models(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    return _api_ok(models_response())


@router.get("/admin/api/logs")
async def logs(
    request: Request,
    since_id: int = 0,
    limit: int = 200,
    level: str | None = None,
) -> dict[str, Any]:
    _check_admin_auth(request)
    return _api_ok(
        {
            "items": get_buffered_logs(since_id=since_id, limit=limit, level=level),
            "limit": limit,
        }
    )


@router.get("/admin/api/network")
async def network(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    settings = _settings(request)
    region = await _probe_region(settings)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0),
        follow_redirects=True,
        proxy=settings.upstream_proxy_url,
        trust_env=False,
    ) as client:
        connectivity = [
            await _probe_url(client, "codebuff", f"{settings.codebuff_api_url}/api/healthz"),
            await _probe_url(client, "freebuff", "https://freebuff.com"),
        ]
    return _api_ok(
        {
            "region": region,
            "connectivity": connectivity,
            "proxy_enabled": settings.proxy_enabled,
            "proxy_url": _mask(settings.proxy_url, keep=10),
        }
    )


@router.put("/admin/api/freebuff-tokens")
async def save_tokens(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    tokens = [str(item).strip() for item in body.get("tokens") or []]
    return _api_ok(await _save_token_list(request, tokens), "tokens saved")


@router.post("/admin/api/freebuff-tokens/verify")
async def verify_token(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    settings = replace(_settings(request), codebuff_token=token)
    client = CodebuffClient(settings)
    try:
        data = await client.get_session()
        return _api_ok({"ok": True, "info": "Token verified", "upstream": data})
    except CodebuffError as error:
        return _api_ok({"ok": False, "info": str(error)})
    finally:
        await client.aclose()


@router.get("/admin/api/freebuff-tokens/{index}")
async def get_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    tokens = _tokens(_settings(request))
    if index < 1 or index > len(tokens):
        raise HTTPException(status_code=404, detail="Token not found")
    token = tokens[index - 1]
    return _api_ok({"index": index, "token": token, "masked": _mask(token)})


@router.post("/admin/api/freebuff-tokens")
async def add_token(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    tokens = _tokens(_settings(request))
    tokens.append(token)
    return _api_ok(await _save_token_list(request, tokens), "token added")


@router.put("/admin/api/freebuff-tokens/{index}")
async def update_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    tokens = _tokens(_settings(request))
    if index < 1 or index > len(tokens):
        raise HTTPException(status_code=404, detail="Token not found")
    tokens[index - 1] = token
    return _api_ok(await _save_token_list(request, tokens), "token updated")


@router.delete("/admin/api/freebuff-tokens/{index}")
async def delete_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    tokens = _tokens(_settings(request))
    if index < 1 or index > len(tokens):
        raise HTTPException(status_code=404, detail="Token not found")
    tokens.pop(index - 1)
    return _api_ok(await _save_token_list(request, tokens), "token deleted")


@router.put("/admin/api/api-key")
async def save_api_key(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    api_key = str(body.get("api_key") or "").strip()
    if len(api_key) < 8:
        raise HTTPException(status_code=400, detail="API key must be at least 8 characters")
    new_settings = replace(_settings(request), local_api_key=api_key)
    if not _is_vercel():
        write_env_values({"FREEBUFF_API_KEY": api_key})
    _apply_env({"FREEBUFF_API_KEY": api_key})
    request.app.state.settings = new_settings
    return _api_ok(
        {**_config_payload(new_settings), "persisted": not _is_vercel()},
        "api key saved",
    )


@router.put("/admin/api/security")
async def save_security(request: Request) -> JSONResponse:
    _check_admin_auth(request)
    body = await request.json()
    admin_key = str(body.get("admin_key") or "").strip()
    if len(admin_key) < 8:
        raise HTTPException(status_code=400, detail="Admin key must be at least 8 characters")
    old_settings = _settings(request)
    new_settings = replace(
        old_settings,
        admin_key=admin_key,
    )
    values = {"FREEBUFF_ADMIN_KEY": admin_key}
    if not _is_vercel():
        write_env_values(values)
    _apply_env(values)
    request.app.state.settings = new_settings
    response = JSONResponse(
        _api_ok(
            {
                **_config_payload(new_settings),
                "persisted": not _is_vercel(),
            },
            "security saved",
        )
    )
    response.delete_cookie(COOKIE_NAME)
    return response


@router.post("/admin/api/chat-test")
async def chat_test(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    model = str(body.get("model") or DEFAULT_MODEL.id).strip() or DEFAULT_MODEL.id
    prompt = str(body.get("prompt") or "ping").strip() or "ping"
    from .app import _collect_completion, _start_freebuff_run_chain
    from .openai_compat import build_upstream_payload, normalize_chat_messages
    from .models import resolve_model

    model_config = resolve_model(model)
    messages = normalize_chat_messages([{"role": "user", "content": prompt}])
    lease = await request.app.state.accounts.acquire_session(model_config.session_id, messages)
    try:
        await lease.client.validate_agents()
        await lease.client.request_ad_chain(messages=messages)
        run = await _start_freebuff_run_chain(lease.client, model_config)
        payload = build_upstream_payload(
            {"model": model_config.id, "messages": messages, "stream": False},
            session=lease.session,
            run_id=run.payload_run_id,
            client_id=_settings(request).client_id,
            trace_session_id="admin-test",
            upstream_model_id=model_config.upstream_id,
        )
        response = await _collect_completion(request, payload, run, model_config.id, client=lease.client)
        return _api_ok({"ok": True, "response": response})
    except Exception as error:
        return _api_ok({"ok": False, "info": str(error)})
    finally:
        await lease.aclose()


# ── Request Records ────────────────────────────────────────────────────


@router.get("/admin/api/requests")
async def list_requests(
    request: Request,
    since_id: int = 0,
    limit: int = 200,
    model: str | None = None,
    status: str | None = None,
    api_key_name: str | None = None,
) -> dict[str, Any]:
    _check_admin_auth(request)
    store: RequestStore = request.app.state.request_store
    items = store.list(
        since_id=since_id, limit=limit, model=model,
        status=status, api_key_name=api_key_name,
    )
    return _api_ok({"items": items, "limit": limit})


@router.get("/admin/api/requests/stats")
async def request_stats(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    store: RequestStore = request.app.state.request_store
    return _api_ok(store.stats())


@router.delete("/admin/api/requests")
async def clear_requests(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    store: RequestStore = request.app.state.request_store
    store.clear()
    return _api_ok({}, "request records cleared")


# ── API Key Management ────────────────────────────────────────────────


def _api_key_store(request: Request) -> ApiKeyStore:
    return request.app.state.api_key_store


def _persist_api_keys(request: Request) -> tuple[bool, str]:
    store = _api_key_store(request)
    env_json = store.to_env_json()
    if not _is_vercel():
        write_env_values({"FREEBUFF_API_KEYS": env_json})
    _apply_env({"FREEBUFF_API_KEYS": env_json})
    return not _is_vercel(), env_json


@router.get("/admin/api/api-keys")
async def list_api_keys(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    store = _api_key_store(request)
    return _api_ok({"items": store.list_all(), "count": store.total_count, "active_count": store.count})


@router.post("/admin/api/api-keys")
async def create_api_key(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    key = str(body.get("key") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(key) < 8:
        raise HTTPException(status_code=400, detail="key must be at least 8 characters")
    store = _api_key_store(request)
    if store.get(name):
        raise HTTPException(status_code=409, detail=f"API key '{name}' already exists")
    allowed = body.get("allowed_models", ["*"])
    rec = ApiKeyRecord(
        name=name, key=key,
        allowed_models=allowed if isinstance(allowed, list) else ["*"],
        enabled=True,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    store.add(rec)
    persisted, _ = _persist_api_keys(request)
    return _api_ok(rec.to_dict(mask=True), f"created{' and persisted' if persisted else ''}")


@router.put("/admin/api/api-keys/{name}")
async def update_api_key(request: Request, name: str) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    store = _api_key_store(request)
    fields: dict[str, Any] = {}
    if "key" in body:
        k = str(body["key"]).strip()
        if k and len(k) < 8:
            raise HTTPException(status_code=400, detail="key must be at least 8 characters")
        fields["key"] = k
    if "allowed_models" in body:
        am = body["allowed_models"]
        fields["allowed_models"] = am if isinstance(am, list) else ["*"]
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    if not store.update(name, **fields):
        raise HTTPException(status_code=404, detail=f"API key '{name}' not found")
    persisted, _ = _persist_api_keys(request)
    updated = store.get(name)
    return _api_ok(updated.to_dict(mask=True) if updated else {}, f"updated{' and persisted' if persisted else ''}")


@router.delete("/admin/api/api-keys/{name}")
async def delete_api_key(request: Request, name: str) -> dict[str, Any]:
    _check_admin_auth(request)
    store = _api_key_store(request)
    if not store.delete(name):
        raise HTTPException(status_code=404, detail=f"API key '{name}' not found")
    persisted, _ = _persist_api_keys(request)
    return _api_ok({}, f"deleted{' and persisted' if persisted else ''}")


@router.put("/admin/api/api-keys/{name}/toggle")
async def toggle_api_key(request: Request, name: str) -> dict[str, Any]:
    _check_admin_auth(request)
    store = _api_key_store(request)
    rec = store.get(name)
    if not rec:
        raise HTTPException(status_code=404, detail=f"API key '{name}' not found")
    store.update(name, enabled=not rec.enabled)
    persisted, _ = _persist_api_keys(request)
    updated = store.get(name)
    return _api_ok(updated.to_dict(mask=True) if updated else {}, "toggled")
