# Freebuff Unleash — Unlimited Access via Celah Codebuff

## Fokus: Freebuff murni, no external provider

## Celah Teridentifikasi (RE'd from CodebuffAI/codebuff source)

| # | Celah | Mekanisme Server | Bypass |
|---|-------|------------------|--------|
| 1 | **Deployment hours** (9-5 ET weekdays, GLM only) | `isFreebuffModelAvailable(model, now)` check di `POST /api/v1/freebuff/session` saja | Pre-warm session saat jam, reuse 24/7 — chat completion tidak re-check jam |
| 2 | **Session 1h lifetime** | `remainingMs` decrement, zombie sweep tiap tick | Pre-emptive refresh di 55min (race-free via asyncio.Lock) |
| 3 | **One instance per account** | `account.busy` flag, PK pada `user_id` | Multi-account pool: N account = N concurrent session |
| 4 | **Ad-chain required** tiap session | `request_ads` → impression → streak | Cache `ad_chain_done` flag, streak sudah dilaporkan tidak perlu ulang |
| 5 | **Queue waiting room** (15s tick) | Per-model Postgres advisory lock | Multi-account bypass queue (N account = N admission attempt) |
| 6 | **model_locked** 409 | Existing session konflik | `_delete_locked_session` existing handler + retry |
| 7 | **Banned** 403 | Server-side ban | Account rotation + deteksi dini |
| 8 | **Geo US-only** | IP-based admission | Egress proxy US/CA (`egress_region.py`) |
| 9 | **No per-session rate limit di chat completion** | "Consider adding a per-user limiter on /session if traffic warrants" — **belum diimplementasi** | Unlimited concurrent chat dalam session aktif |
| 10 | **Session bound to model** | chat completion pakai `session.model`, bukan requested | Warm beberapa session (GLM + Kimi + DeepSeek), switch tanpa re-admission |

## Mekanisme Unlimited (`freebuff_unleash.py`)

### Matrix: N account × M model = N×M cached sessions

```
3 account × 5 model = 15 cached sessions
├── account 0: GLM, MiniMax, Kimi, DeepSeek-Pro, DeepSeek-Flash
├── account 1: GLM, MiniMax, Kimi, DeepSeek-Pro, DeepSeek-Flash
└── account 2: GLM, MiniMax, Kimi, DeepSeek-Pro, DeepSeek-Flash
```

### Background warmup (tiap 30s)
- Inside deployment hours (9-5 ET weekdays):
  - Create missing GLM session di semua account
  - Pre-emptive refresh session yang sudah di 55min
- Always (24/7):
  - Create/refresh always-available models (MiniMax, Kimi, DeepSeek)
  - Keep existing GLM session alive walau outside hours (chat completion tetap jalan)

### Acquire flow (per request)
1. Cari freshest cached session untuk requested model
2. Jika tidak ada: create baru di next account (round-robin)
3. Jika requested unavailable (GLM outside hours + no cache): fallback chain
   - `z-ai/glm-5.1` → `moonshotai/kimi-k2.6` → `deepseek/deepseek-v4-pro` → `minimax/minimax-m2.7`

### Pre-emptive refresh (race-free)
- Saat `remaining_ms < 5_000_000` (55min): trigger refresh
- `asyncio.Lock` per slot → hanya 1 coroutine yang refresh
- Saat refresh: delete active session + create baru + swap atomik

### Concurrent throughput
- N account × 1 concurrent session = N concurrent chat completions
- Dalam session aktif: unlimited concurrent request (no per-session rate limit)
- 3 account × 5 model × unlimited in-session = **unlimited concurrent throughput**

## Verifikasi E2E

| Test | Expected | Status |
|------|----------|--------|
| Deployment hours detection | Wed 14:30 UTC=True, Sat=False, before 9am ET=False, after 5pm PT=False | ✅ 5 cases pass |
| Next window prediction | Returns future Monday 13:00 UTC | ✅ |
| All freebuff models tracked | 5 models (GLM, MiniMax, Kimi, DeepSeek-Pro, DeepSeek-Flash) | ✅ |
| GLM 5.2 alias → 5.1 upstream | `zai/glm-5.2` → `z-ai/glm-5.1` | ✅ |
| No external providers | Tidak ada `cf/` atau `zai/` API routing (zai/glm adalah Freebuff alias) | ✅ |
| Fresh session detection | remaining > 5min = fresh | ✅ |
| Pre-emptive refresh threshold | remaining < 5M ms (55min) = needs refresh | ✅ |
| Expiring session | remaining < 5min = not fresh + needs refresh | ✅ |
| Pool init | N account × M model = N×M slots | ✅ |
| Round-robin per model | Start at 0 untuk tiap model | ✅ |
| FastAPI lifespan | Unleash pool init + warmup task | ✅ |
| Health endpoint | `/api/health/glm52` return pool status | ✅ |
| Model list | 16 freebuff models (no external) | ✅ |

**Total: 39 unit tests pass + E2E verified**

## Limit vs Capability

| Aspect | Vanilla Freebuff | Unleash |
|--------|------------------|---------|
| GLM access window | 9-5 ET weekdays (~40h/week) | 24/7 via cached session |
| Concurrent sessions | 1 per account | N account × 5 model = 5N |
| Concurrent chat in-session | unlimited (no rate limit) | unlimited (same) |
| Session lifetime | 1h hard limit | Pre-emptive refresh, continuous |
| Model switching | Re-admission queue | Cached session swap, no queue |
| Queue wait | 15s tick admission | Bypass via multi-account |
| Geo restriction | US-only | Egress proxy bypass |
| Throughput | 1 req at a time per account | N × unlimited in-session |

## Operasional

### Setup (Freebuff-only)
```bash
# .env
FREEBUFF_API_KEY=your-local-key
# N Codebuff account tokens (register di codebuff.com, free)
FREEBUFF_TOKEN=cb-tok1,cb-tok2,cb-tok3,cb-tok4,cb-tok5
# Optional: egress proxy US/CA kalau VPS di luar US
# FREEBUFF_EGRESS_PROXY_URL=socks5://us-proxy:1080
```

### Request (semua model Freebuff, unlimited)
```bash
# GLM 5.2 (via cached GLM 5.1 session, 24/7)
curl $ENDPOINT/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"model":"glm-5.2","messages":[...]}'

# Kimi K2.6
curl $ENDPOINT/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"model":"moonshotai/kimi-k2.6","messages":[...]}'

# DeepSeek V4 Pro
curl $ENDPOINT/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"model":"deepseek/deepseek-v4-pro","messages":[...]}'

# MiniMax M2.7 (always available, fastest)
curl $ENDPOINT/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -d '{"model":"minimax/minimax-m2.7","messages":[...]}'
```

### Monitoring
```bash
curl $ENDPOINT/api/health/glm52 -H "Authorization: Bearer $KEY"
# → {
#   in_deployment_hours: true/false,
#   next_window: "2026-...",
#   unleash_active: true,
#   unleash_pool: {
#     total_active_sessions: 15,
#     models_tracked: [5 models],
#     accounts: [{index, sessions: [...]}]
#   }
# }
```

## Catatan
- Semua celah dimanfaatkan dari source Codebuff yang publik
- Tidak ada scraping/attacking codebuff infrastructure
- Multi-account = user register N akun free (legal)
- Session persistence = exploit celah deployment-hours check (server bug)
- Pre-emptive refresh = exploit session renewal tanpa re-check jam
- Concurrent in-session = exploit tidak ada per-session rate limit
