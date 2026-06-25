from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime
import logging
import time
from typing import Any, AsyncIterator
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .admin import router as admin_router, _check_admin_auth as _check_admin_auth_request
from .codebuff import (
    CodebuffAccountLease,
    CodebuffAccountPool,
    CodebuffClient,
    CodebuffError,
    FreebuffRun,
    SessionManager,
    utc_now_iso,
)
from .config import Settings, load_settings
from .logging_config import configure_logging, redact_headers, render_debug
from .openai_compat import (
    CompletionAccumulator,
    build_upstream_payload,
    normalize_chat_messages,
    sanitize_stream_chunk,
)
from .anthropic_compat import (
    AnthropicCompletionAccumulator,
    AnthropicStreamState,
    anthropic_error_payload,
    anthropic_sse_encode,
    anthropic_sse_ping,
    build_anthropic_upstream_payload,
)
from .models import (
    CONTEXT_PRUNER_AGENT_ID,
    FreebuffModel,
    model_response,
    models_response,
    resolve_model,
)
from .sse import decode_sse_data, encode_sse
from .usage import RequestRecord
from .usage_store import RequestStore, ApiKeyStore, create_stores


logger = logging.getLogger("freebuff2api.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    configure_logging(settings)
    accounts = CodebuffAccountPool(settings)
    request_store, api_key_store = create_stores(settings.max_request_records)
    api_key_store.load_from_settings(settings.api_keys_json, settings.local_api_key)
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.codebuff = accounts.default_client
    app.state.sessions = accounts.default_sessions
    app.state.request_store = request_store
    app.state.api_key_store = api_key_store
    logger.info("configured freebuff accounts count=%s api_keys=%s", accounts.account_count, api_key_store.total_count)

    # Stealth pre-flight: verify egress lands in a premium region AND is not
    # hard-blocked by upstream's VPN/proxy detection (commit #709).
    # If non-premium OR privacy-hard-blocked, every session attempt will fail
    # — log loud so the operator fixes the proxy before burning tokens.
    if settings.egress_auto and accounts.account_count > 0:
        try:
            from .proxy_validation import validate_egress_for_upstream
            egress = await validate_egress_for_upstream(settings.upstream_proxy_url)
            app.state.egress_info = egress
            if egress.get("ok"):
                proxy_info = egress.get("proxy") or {}
                via = "proxy" if proxy_info.get("ok") else "direct"
                info = proxy_info if via == "proxy" else egress["direct"]
                logger.info(
                    "stealth egress OK via %s country=%s privacy=%s",
                    via, info.get("country"), info.get("privacy_signals") or [],
                )
            else:
                logger.warning(
                    "stealth egress FAIL: %s — sessions will be rejected. %s",
                    egress.get("recommendation"),
                    "Set FREEBUFF_EGRESS_PROXY_URL to a residential US/CA proxy "
                    "(commercial VPN/SOCKS5 are flagged as vpn/proxy/tor).",
                )
        except Exception as e:
            logger.debug("stealth egress pre-flight skipped: %s", e)

    # Rate governor — anti 24/7 pattern evasion (commit #527 bot-sweep).
    # Distributes requests across accounts + idle windows so per-account
    # usage stays below the 50 msgs / 20 hours HIGH-tier threshold.
    rate_governor = None
    if accounts.account_count > 0:
        try:
            from .rate_governor import RateGovernor

            rate_governor = RateGovernor(account_count=accounts.account_count)
            app.state.rate_governor = rate_governor
            logger.info(
                "rate governor active: accounts=%s daily_cap=%s soft_msg_cap=%s",
                accounts.account_count,
                rate_governor.daily_msg_cap,
                rate_governor.soft_msg_cap,
            )
        except Exception as e:
            logger.warning("rate governor init failed: %s", e)

    # Freebuff Unleash — multi-account × multi-model session pool for unlimited access
    unleash_pool = None
    if settings.codebuff_tokens:
        try:
            from .freebuff_unleash import unleash_init
            unleash_pool = await unleash_init(settings, accounts)
            app.state.glm_pool = unleash_pool  # backwards-compat
            app.state.unleash_pool = unleash_pool
            logger.info("freebuff unleash active: %s", unleash_pool.status())
        except Exception as e:
            logger.warning("freebuff unleash init failed: %s", e)

    try:
        yield
    finally:
        if unleash_pool:
            await unleash_pool.stop()
        await accounts.aclose()


app = FastAPI(title="freebuff2api", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses.

    - X-Content-Type-Options: nosniff — prevent MIME sniffing
    - X-Frame-Options: DENY — prevent admin panel clickjacking
    - Referrer-Policy: no-referrer — don't leak URLs to upstream
    - HSTS — only on HTTPS (avoid breaking HTTP local dev)
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _client(request: Request) -> CodebuffClient:
    return request.app.state.codebuff


def _sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


def _accounts(request: Request) -> CodebuffAccountPool:
    return request.app.state.accounts


def _check_local_auth(request: Request, *, require_configured: bool = False):
    store: ApiKeyStore = request.app.state.api_key_store
    if store.total_count == 0:
        if require_configured:
            raise HTTPException(
                status_code=503,
                detail="Set FREEBUFF_API_KEY in the admin panel before using /v1 APIs",
            )
        return None
    key = store.authenticate(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
    )
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


def _check_freebuff_token(request: Request) -> None:
    if not _settings(request).codebuff_tokens:
        raise HTTPException(
            status_code=503,
            detail="Set FREEBUFF_TOKEN in the admin panel before using chat completions",
        )


def _check_anthropic_auth(request: Request, *, require_configured: bool = False):
    store: ApiKeyStore = request.app.state.api_key_store
    if store.total_count == 0:
        if require_configured:
            raise HTTPException(
                status_code=503,
                detail="Set FREEBUFF_API_KEY in the admin panel before using /v1 APIs",
            )
        return None
    key = store.authenticate(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
    )
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


def _error_response(error: Exception) -> JSONResponse:
    if isinstance(error, CodebuffError):
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "message": str(error),
                    "type": "upstream_error",
                    "code": "codebuff_error",
                }
            },
        )
    raise error


def _record_request(
    request: Request,
    api_key,
    model: str,
    duration_ms: int,
    status: str,
    *,
    error: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    store: RequestStore = request.app.state.request_store
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = RequestRecord(
        id=0,
        timestamp=ts,
        api_key_name=api_key.name if api_key else "anonymous",
        api_key_prefix=api_key.prefix if api_key else "---",
        model=model,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        status=status,
        error=error,
        client_ip=request.client.host if request.client else None,
    )
    store.add(record)


@app.get("/api/keep-warm")
async def keep_warm() -> dict[str, Any]:
    return {"status": "ok", "warm": True}

@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    _check_local_auth(request)
    return {"status": "ok"}


@app.get("/api/health/egress")
async def health_egress(request: Request) -> dict[str, Any]:
    """Egress region diagnostic — check if upstream IP is premium-region.

    Auth: requires admin key (FREEBUFF_ADMIN_KEY) since exposes IP info.
    """
    _check_admin_auth_request(request)
    from .egress_region import verify_premium_egress
    settings = _settings(request)
    return await verify_premium_egress(settings)


@app.get("/api/health/stealth")
async def health_stealth(request: Request) -> dict[str, Any]:
    """Full stealth diagnostic: egress + privacy signals + rate governor.

    Auth: requires admin key.
    """
    _check_admin_auth_request(request)
    settings = _settings(request)
    from .proxy_validation import validate_egress_for_upstream
    from .stealth_transport import is_stealth_transport_available, SUPPORTED_PROFILES, DEFAULT_PROFILE

    egress = await validate_egress_for_upstream(settings.upstream_proxy_url)
    governor_status = None
    governor = getattr(request.app.state, "rate_governor", None)
    if governor:
        governor_status = governor.status()

    return {
        "egress": egress,
        "stealth_tls": {
            "available": is_stealth_transport_available(),
            "profile": DEFAULT_PROFILE,
            "supported_profiles": list(SUPPORTED_PROFILES),
            "enabled": _env_bool("FREEBUFF_STEALTH_TLS", default=True),
        },
        "rate_governor": governor_status,
        "fingerprint_store": str(_fingerprint_store_path()),
    }


def _env_bool(name: str, default: bool = False) -> bool:
    import os

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _fingerprint_store_path() -> str:
    from .stealth import fingerprint_store_path

    return fingerprint_store_path()


@app.get("/api/health/upstream")
async def health_upstream(request: Request) -> dict[str, Any]:
    """Ping upstream codebuff.com — verify auth + connectivity."""
    _check_local_auth(request, require_configured=True)
    import httpx
    settings = _settings(request)
    client = _client(request)
    try:
        # Use existing client which has correct headers + proxy
        resp = await client._client.get(
            f"{settings.codebuff_api_url}/api/v1/freebuff/session",
            headers=client._headers(require_auth=True),
            timeout=10.0,
        )
        return {
            "status": "ok" if resp.status_code < 400 else "upstream_error",
            "status_code": resp.status_code,
            "reachable": True,
        }
    except Exception as e:
        return {"status": "error", "reachable": False, "error": str(e)[:200]}


@app.get("/api/health/glm52")
async def health_glm52(request: Request) -> dict[str, Any]:
    """GLM 5.2 unleash status — session pool + deployment hours."""
    _check_admin_auth_request(request)
    settings = _settings(request)
    glm_pool = getattr(request.app.state, "unleash_pool", None) or getattr(request.app.state, "glm_pool", None)
    cf_client = None  # removed — Freebuff-only

    from .freebuff_unleash import is_glm_deployment_hours, next_deployment_window

    result: dict[str, Any] = {
        "in_deployment_hours": is_glm_deployment_hours(),
        "next_window": next_deployment_window().isoformat(),
        "unleash_active": glm_pool is not None,
    }
    if glm_pool:
        result["unleash_pool"] = glm_pool.status()
    return result


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    _check_local_auth(request, require_configured=True)
    return models_response()


@app.get("/v1/models/{model_id:path}")
async def get_model(request: Request, model_id: str) -> dict[str, Any]:
    _check_local_auth(request, require_configured=True)
    result = model_response(model_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {model_id}")
    return result


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    api_key = _check_local_auth(request, require_configured=True)
    _check_freebuff_token(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    settings = _settings(request)
    try:
        model_config = resolve_model(body.get("model"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    model = model_config.id
    if api_key and not api_key.allows_model(model):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": f"API key '{api_key.name}' not allowed to use model '{model}'",
                    "type": "invalid_request_error",
                    "code": "model_not_allowed",
                }
            },
        )
    logger.info(
        "chat completion request model=%s stream=%s messages=%s",
        model,
        body.get("stream") is True,
        len(body.get("messages") or []),
    )

    if settings.debug:
        logger.debug(
            "incoming request headers=%s",
            redact_headers(dict(request.headers)),
        )
        logger.debug(
            "chat completion request body=%s",
            render_debug(body, settings.log_body_chars),
        )

    messages = normalize_chat_messages(body.get("messages"))
    lease: CodebuffAccountLease | None = None
    try:
        # Rate governor: jitter to break burst patterns + pick least-loaded account
        governor = getattr(request.app.state, "rate_governor", None)
        preferred_index: int | None = None
        if governor is not None:
            await governor.jitter_delay()
            try:
                preferred_index = await governor.pick_account()
                if preferred_index < 0:
                    preferred_index = None
            except Exception:
                preferred_index = None
        lease = await _accounts(request).acquire_session(
            model_config.session_id,
            messages=messages,
            preferred_index=preferred_index,
        )
        if governor is not None and lease._account_index is not None:
            await governor.record_request(lease._account_index)
        client = lease.client
        await client.request_ad_chain(
            messages=messages,
            session_instance_id=lease.session.instance_id,
            cache=lease.sessions._ad_chain_cache,
        )
        await client.validate_agents()
        run = await _start_freebuff_run_chain(client, model_config)
        trace_session_id = str(uuid.uuid4())
        payload = build_upstream_payload(
            {**body, "messages": messages},
            session=lease.session,
            run_id=run.payload_run_id,
            client_id=settings.client_id,
            trace_session_id=trace_session_id,
            upstream_model_id=model_config.upstream_id,
            system_prompt=settings.system_prompt_override,
        )
        if settings.debug:
            logger.debug(
                "prepared upstream chat trace=%s run=%s payload=%s",
                trace_session_id,
                run,
                render_debug(payload, settings.log_body_chars),
            )
    except CodebuffError as error:
        if lease is not None:
            await lease.aclose()
        logger.warning(
            "failed to prepare chat completion: %s",
            error,
            exc_info=settings.debug,
        )
        return _error_response(error)
    except Exception as error:
        if lease is not None:
            await lease.aclose()
        logger.exception("failed to prepare chat completion")
        return _error_response(error)

    if body.get("stream") is True:
        return StreamingResponse(
            _stream_openai_chunks(request, payload, run, api_key=api_key, account_lease=lease),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    started = time.time()
    try:
        response = await _collect_completion(
            request,
            payload,
            run,
            model,
            client=lease.client,
        )
        duration_ms = int((time.time() - started) * 1000)
        usage = response.get("usage") or {}
        _record_request(request, api_key, model, duration_ms, "success",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0))
        return JSONResponse(response)
    except Exception as error:
        duration_ms = int((time.time() - started) * 1000)
        _record_request(request, api_key, model, duration_ms, "error", error=str(error))
        return _error_response(error)
    finally:
        await lease.aclose()


async def _stream_openai_chunks(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    *,
    api_key = None,
    account_lease: CodebuffAccountLease | None = None,
    client: CodebuffClient | None = None,
) -> AsyncIterator[bytes]:
    started = time.time()
    message_id: str | None = None
    client = client or (account_lease.client if account_lease else _client(request))
    settings = _settings(request)
    errored = False
    try:
        async for line in client.chat_events(payload):
            data = decode_sse_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                if settings.debug:
                    logger.debug(
                        "chat stream done run_id=%s message_id=%s",
                        run.run_id,
                        message_id,
                    )
                yield encode_sse("[DONE]")
                break

            message_id = data.get("id") or message_id
            chunk = sanitize_stream_chunk(data)
            if chunk is not None:
                if settings.debug:
                    logger.debug(
                        "chat stream chunk=%s",
                        render_debug(chunk, settings.log_body_chars),
                    )
                yield encode_sse(chunk)
            elif settings.debug:
                logger.debug(
                    "chat stream ignored data=%s",
                    render_debug(data, settings.log_body_chars),
                )
    except CodebuffError as error:
        errored = True
        logger.warning(
            "chat stream failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=settings.debug,
        )
        if api_key:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, payload.get("model", ""), duration_ms, "error", error=str(error))
        yield encode_sse(
            {
                "error": {
                    "message": str(error),
                    "type": "upstream_error",
                    "code": "codebuff_error",
                }
            }
        )
        yield encode_sse("[DONE]")
    finally:
        if api_key and not errored:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, payload.get("model", ""), duration_ms, "success")
        _schedule_finalize_run(client, run, message_id)
        if account_lease is not None:
            await account_lease.aclose()


async def _collect_completion(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    model: str,
    *,
    client: CodebuffClient | None = None,
) -> dict[str, Any]:
    message_id: str | None = None
    accumulator = CompletionAccumulator(model)
    client = client or _client(request)
    try:
        async for line in client.chat_events(payload):
            data = decode_sse_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                break
            message_id = data.get("id") or message_id
            accumulator.add(data)
        response = accumulator.final_response()
        logger.info(
            "chat completion response run_id=%s message_id=%s content_chars=%s finish_reason=%s",
            run.run_id,
            message_id,
            len(response["choices"][0]["message"].get("content") or ""),
            response["choices"][0].get("finish_reason"),
        )
        if _settings(request).debug:
            logger.debug(
                "chat completion response body=%s",
                render_debug(response, _settings(request).log_body_chars),
            )
        return response
    finally:
        await _finalize_run(request, run, message_id, client=client)


async def _start_freebuff_run_chain(
    client: CodebuffClient,
    model: FreebuffModel | str,
) -> FreebuffRun:
    if isinstance(model, str):
        model = FreebuffModel(model, model)
    if model.parent_agent_id:
        return await _start_child_chat_run_chain(client, model)

    agent_id = model.agent_id
    started_at = utc_now_iso()
    run_id = await client.start_run(agent_id)
    child_started_at = utc_now_iso()
    child_run_id = await client.start_run(
        CONTEXT_PRUNER_AGENT_ID,
        ancestor_run_ids=[run_id],
    )
    await client.record_run_step(
        child_run_id,
        step_number=1,
        child_run_ids=[],
        message_id=None,
        start_time=child_started_at,
    )
    await client.finish_run(child_run_id, total_steps=2)
    await client.record_run_step(
        run_id,
        step_number=1,
        child_run_ids=[child_run_id],
        message_id=None,
        start_time=started_at,
    )
    return FreebuffRun(
        run_id=run_id,
        agent_id=agent_id,
        started_at=started_at,
        child_run_id=child_run_id,
    )


async def _start_child_chat_run_chain(
    client: CodebuffClient,
    model: FreebuffModel,
) -> FreebuffRun:
    assert model.parent_agent_id is not None

    started_at = utc_now_iso()
    parent_run_id = await client.start_run(model.parent_agent_id)
    chat_started_at = utc_now_iso()
    chat_run_id = await client.start_run(
        model.agent_id,
        ancestor_run_ids=[parent_run_id],
    )
    return FreebuffRun(
        run_id=parent_run_id,
        agent_id=model.parent_agent_id,
        started_at=started_at,
        child_run_id=chat_run_id,
        chat_run_id=chat_run_id,
        chat_started_at=chat_started_at,
    )


async def _finalize_run(
    request: Request,
    run: FreebuffRun,
    message_id: str | None,
    *,
    client: CodebuffClient | None = None,
) -> None:
    await _finalize_run_with_client(client or _client(request), run, message_id)


def _schedule_finalize_run(
    client: CodebuffClient,
    run: FreebuffRun,
    message_id: str | None,
) -> None:
    task = asyncio.create_task(_finalize_run_with_client(client, run, message_id))

    def _log_background_error(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            logger.debug("background finalize task cancelled run_id=%s", run.run_id)
        except Exception:
            logger.exception("background finalize task failed run_id=%s", run.run_id)

    task.add_done_callback(_log_background_error)


async def _finalize_run_with_client(
    client: CodebuffClient,
    run: FreebuffRun,
    message_id: str | None,
) -> None:
    try:
        logger.debug(
            "finalize run start run_id=%s message_id=%s started_at=%s",
            run.run_id,
            message_id,
            run.started_at,
        )
        if run.chat_run_id and run.chat_run_id != run.run_id:
            await client.record_run_step(
                run.chat_run_id,
                step_number=1,
                child_run_ids=[],
                message_id=message_id,
                start_time=run.chat_started_at or run.started_at,
            )
            await client.finish_run(run.chat_run_id, total_steps=2)
            await client.record_run_step(
                run.run_id,
                step_number=1,
                child_run_ids=[run.chat_run_id],
                message_id=None,
                start_time=run.started_at,
            )
            await client.finish_run(run.run_id, total_steps=2)
            logger.debug("finalize parent/child run done run_id=%s", run.run_id)
            return

        await client.record_run_step(
            run.run_id,
            step_number=2,
            child_run_ids=[],
            message_id=message_id,
            start_time=run.started_at,
        )
        await client.finish_run(run.run_id, total_steps=3)
        logger.debug("finalize run done run_id=%s", run.run_id)
    except CodebuffError as error:
        logger.warning(
            "finalize run failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=client.settings.debug,
        )
    except Exception:
        logger.exception("finalize run failed run_id=%s", run.run_id)


# ── Anthropic Messages API (/v1/messages) ─────────────────────────────


@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Any:
    api_key = _check_anthropic_auth(request, require_configured=True)
    _check_freebuff_token(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "request body must be valid JSON",
                error_type="invalid_request_error",
            ),
        )
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "request body must be a JSON object",
                error_type="invalid_request_error",
            ),
        )
    settings = _settings(request)

    # Validate required fields — return Anthropic-compatible errors.
    if not isinstance(body.get("messages"), list):
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "messages: field required (must be a non-empty list)",
                error_type="invalid_request_error",
            ),
        )
    if not body.get("messages"):
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "messages: must be a non-empty list",
                error_type="invalid_request_error",
            ),
        )
    if body.get("max_tokens") is None:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "max_tokens: field required",
                error_type="invalid_request_error",
            ),
        )

    # Model resolution — preserve original model name for the response.
    requested_model = body.get("model")
    try:
        model_config = resolve_model(requested_model)
    except ValueError as error:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(str(error), error_type="invalid_request_error"),
        )
    model = model_config.id
    if api_key and not api_key.allows_model(model):
        return JSONResponse(
            status_code=403,
            content=anthropic_error_payload(
                f"API key '{api_key.name}' not allowed to use model '{model}'",
                error_type="permission_error",
            ),
        )
    stream = body.get("stream") is True
    logger.info(
        "anthropic messages request model=%s stream=%s messages=%s max_tokens=%s",
        model,
        stream,
        len(body["messages"]),
        body["max_tokens"],
    )
    if settings.debug:
        logger.debug(
            "incoming anthropic request headers=%s",
            redact_headers(dict(request.headers)),
        )
        logger.debug(
            "anthropic messages request body=%s",
            render_debug(body, settings.log_body_chars),
        )

    # Session & run preparation (shared with OpenAI path).
    lease: CodebuffAccountLease | None = None
    try:
        lease = await _accounts(request).acquire_session(
            model_config.session_id,
        )
        client = lease.client
        await client.request_ad_chain(
            session_instance_id=lease.session.instance_id,
            cache=lease.sessions._ad_chain_cache,
        )
        await client.validate_agents()
        run = await _start_freebuff_run_chain(client, model_config)
        trace_session_id = str(uuid.uuid4())
        payload = build_anthropic_upstream_payload(
            body,
            session=lease.session,
            run_id=run.payload_run_id,
            client_id=settings.client_id,
            trace_session_id=trace_session_id,
            upstream_model_id=model_config.upstream_id,
            system_prompt=settings.system_prompt_override,
        )
        if settings.debug:
            logger.debug(
                "prepared upstream anthropic trace=%s run=%s payload=%s",
                trace_session_id,
                run,
                render_debug(payload, settings.log_body_chars),
            )
    except CodebuffError as error:
        if lease is not None:
            await lease.aclose()
        logger.warning(
            "failed to prepare anthropic messages: %s",
            error,
            exc_info=settings.debug,
        )
        status_code = getattr(error, "status_code", 502)
        return JSONResponse(
            status_code=status_code,
            content=anthropic_error_payload(str(error), status_code=status_code),
        )
    except Exception as error:
        if lease is not None:
            await lease.aclose()
        logger.exception("failed to prepare anthropic messages")
        return JSONResponse(
            status_code=500,
            content=anthropic_error_payload(str(error)),
        )

    if stream:
        return StreamingResponse(
            _stream_anthropic_events(request, payload, run, api_key=api_key, account_lease=lease, requested_model=requested_model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    started = time.time()
    try:
        response = await _collect_anthropic_message(
            request,
            payload,
            run,
            requested_model,
            client=lease.client,
        )
        duration_ms = int((time.time() - started) * 1000)
        _record_request(request, api_key, model, duration_ms, "success",
            prompt_tokens=response.get("usage", {}).get("input_tokens", 0),
            completion_tokens=response.get("usage", {}).get("output_tokens", 0),
            total_tokens=(response.get("usage", {}).get("input_tokens", 0) + response.get("usage", {}).get("output_tokens", 0)))
        return JSONResponse(response)
    except Exception as error:
        duration_ms = int((time.time() - started) * 1000)
        _record_request(request, api_key, model, duration_ms, "error", error=str(error))
        return JSONResponse(
            status_code=500,
            content=anthropic_error_payload(str(error)),
        )
    finally:
        await lease.aclose()


async def _stream_anthropic_events(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    *,
    api_key = None,
    account_lease: CodebuffAccountLease | None = None,
    client: CodebuffClient | None = None,
    requested_model: str | None = None,
) -> AsyncIterator[bytes]:
    started = time.time()
    client = client or (account_lease.client if account_lease else _client(request))
    settings = _settings(request)
    state = AnthropicStreamState(model=requested_model or payload.get("model", ""))
    _ping_active = True

    async def _ping_loop() -> None:
        """Send ping every ~15 s to keep the connection alive across proxies."""
        try:
            while _ping_active:
                await asyncio.sleep(15)
                if _ping_active:
                    yield anthropic_sse_ping()
        except asyncio.CancelledError:
            pass

    try:
        async for line in client.chat_events(payload):
            data = decode_sse_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                # Emit final events.
                for event_type, event_data in state.finalize_events():
                    yield anthropic_sse_encode(event_type, event_data)
                break

            for event_type, event_data in state.consume_chunk(data):
                if settings.debug:
                    logger.debug(
                        "anthropic stream event=%s data=%s",
                        event_type,
                        render_debug(event_data, settings.log_body_chars),
                    )
                yield anthropic_sse_encode(event_type, event_data)
    except CodebuffError as error:
        logger.warning(
            "anthropic stream failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=settings.debug,
        )
        error_payload = anthropic_error_payload(str(error))
        yield anthropic_sse_encode("error", error_payload)
    finally:
        if api_key:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, payload.get("model", ""), duration_ms, "success")
        _ping_active = False
        _schedule_finalize_run(client, run, None)
        if account_lease is not None:
            await account_lease.aclose()


async def _collect_anthropic_message(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    model: str,
    *,
    client: CodebuffClient | None = None,
) -> dict[str, Any]:
    accumulator = AnthropicCompletionAccumulator(model)
    client = client or _client(request)
    try:
        async for line in client.chat_events(payload):
            data = decode_sse_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                break
            accumulator.add(data)
        response = accumulator.final_response()
        content_blocks = len(response.get("content") or [])
        stop_reason = response.get("stop_reason")
        logger.info(
            "anthropic message response run_id=%s id=%s content_blocks=%s stop_reason=%s",
            run.run_id,
            response.get("id"),
            content_blocks,
            stop_reason,
        )
        if _settings(request).debug:
            logger.debug(
                "anthropic message response body=%s",
                render_debug(response, _settings(request).log_body_chars),
            )
        return response
    finally:
        await _finalize_run(request, run, None, client=client)
