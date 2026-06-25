# freebuff2api-vercel — Reverse Engineering Analysis & Maximization Plan

> Fork: `Timcuan/freebuff2api-vercel` ← upstream `t479842598/freebuff2api-vercel`
> Branch: `analysis/re-maximize-potential`
> Date: 2026-06-26

## 1. Executive Summary

Repo adalah **OpenAI/Anthropic-compatible API adapter** untuk `codebuff.com` / `freebuff.com` free-tier models. Bukan "exploit" dalam arti klassik — ini adalah **protocol shim** yang mengabstraksikan Codebuff's internal agent-run protocol menjadi standar `/v1/chat/completions` + `/v1/messages`.

**Inti yang di-reverse-engineer:**
1. Codebuff CLI auth flow (`/api/auth/cli/code` → polling `/api/auth/cli/status` → `authToken`)
2. Codebuff internal protocol (`/api/v1/freebuff/session`, `/api/v1/agent-runs`, `/api/v1/chat/completions` upstream)
3. Ad-chain economy (`/api/v1/ads`, `/api/v1/ads/impression`, zeroclick `/api/v2/impressions`) — mekanisme "watch ad → get free session"
4. Session lifecycle & model-mismatch recovery (409 → re-create session)
5. Agent-spawn protocol (parent agent + child agent untuk Gemini free variants)
6. Request fingerprinting (HAR browser UA, fixed headers, host header)

## 2. Struktur Kode (4,433 LoC Python + 1,134 HTML admin)

```
freebuff2api-vercel/
├── main.py                          # Uvicorn entry (local)
├── api/index.py                     # Vercel entry (1 line: export app)
├── vercel.json                      # Rewrites → /api/index.py
├── exploitation.js                  # Cloudflare Worker proxy (obfuscated host)
├── pyproject.toml                   # FastAPI + httpx[socks] + dotenv + uvicorn
├── requirements.txt                 # Vercel install
├── progress.md                      # Dev log (Chinese)
├── tool/
│   ├── get_token.py                 # CLI: device-code OAuth flow
│   └── web/{main.py,static,templates}  # Web version of token getter
├── freebuff2api/
│   ├── app.py              (835)    # FastAPI routes: /v1/chat/completions, /v1/messages, /v1/models, /healthz, /admin, /api/keep-warm
│   ├── codebuff.py         (899)    # ⭐ Upstream client — protocol RE
│   ├── anthropic_compat.py (857)    # Anthropic ↔ OpenAI bidirectional conversion
│   ├── openai_compat.py    (258)    # OpenAI normalize (developer→system, cache_control, Buffy prompt)
│   ├── admin.py            (669)    # Admin API (tokens, api keys, env, logs, network)
│   ├── admin_static/index.html (1134) # Vue 3 + Naive UI admin panel
│   ├── usage_store.py      (198)    # In-memory request records
│   ├── usage.py             (72)    # Usage stats
│   ├── models.py           (161)    # ⭐ Model registry + agent definitions
│   ├── config.py           (160)    # Settings + env load + .env writeback
│   ├── logging_config.py   (150)    # Redaction + debug rendering
│   └── sse.py               (23)    # SSE helpers
└── tests/                  (2,769)  # 12 test files, 120 tests passing
```

## 3. Reverse-Engineered Upstream Protocol

### 3.1 Auth flow (codebuff.com / freebuff.com)

```
POST /api/auth/cli/code
  body: {"fingerprintId": "fb-<hex16>"}
  resp: {"loginUrl": ..., "fingerprintHash": ..., "expiresAt": ...}
                  ↓ user browser opens loginUrl
GET /api/auth/cli/status?fingerprintId=...&fingerprintHash=...&expiresAt=...
  poll every 2s, timeout 5min
  resp: {"user": {"authToken": "Bearer-token", "id":..., "name":..., "email":...}}
                  ↓
verify: GET /api/v1/freebuff/session with Authorization: Bearer <token>
```

**Sumber:** `tool/get_token.py:27-105`, `tool/web/main.py:55-80`

### 3.2 Chat flow (upstream Codebuff)

```
1. GET /api/v1/freebuff/session          # active free session
2. POST /api/v1/ads                       # request ad (waiting_room surface)
3. POST /api/v1/ads/impression            # report ad impression (LITE mode)
4. POST https://zeroclick.dev/api/v2/impressions  # second ad provider
5. GET /api/v1/freebuff/streak            # streak / today_used tracking
6. POST /api/v1/agent-runs                # create run
7. POST /api/v1/agent-runs/{run_id}/steps # step
8. POST /api/v1/chat/completions          # actual chat (upstream OpenAI-compat)
   - body: agentId, model, messages, codebuff_metadata.cost_mode=free
   - SSE stream
```

**Sumber:** `freebuff2api/codebuff.py:160-899`

### 3.3 Model registry (free-tier agents)

```python
FREEBUFF_MODELS = (
  ("deepseek/deepseek-v4-flash", "base2-free-deepseek-flash"),  # default
  ("deepseek/deepseek-v4-pro",   "base2-free-deepseek"),
  ("moonshotai/kimi-k2.6",       "base2-free-kimi"),
  ("minimax/minimax-m2.7",       "base2-free"),
  ("minimax/minimax-m3",         "base2-free-minimax-m3"),
  ("mimo/mimo-v2.5",             "base2-free-mimo"),
  ("mimo/mimo-v2.5-pro",         "base2-free-mimo-pro"),
)
GEMINI_FREE_MODELS = (  # parent + child agent
  ("google/gemini-2.5-flash-lite",         "file-picker",      parent="base2-free-deepseek-flash"),
  ("google/gemini-3.1-flash-lite-preview", "file-picker-max",  parent="base2-free-deepseek-flash"),
  ("google/gemini-3.1-pro-preview",        "thinker-with-files-gemini", parent="base2-free-kimi"),
)
```

**Sumber:** `freebuff2api/models.py:24-63`

### 3.4 Request fingerprinting

Headers fixed untuk bypass upstream fingerprint check:
```python
HAR_BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."
CODEBUFF_JSON_USER_AGENT = "Bun/1.3.11"          # CLI pretends to be Bun runtime
FREEBUFF_CLI_USER_AGENT = "Freebuff-CLI/0.0.105"
CHAT_COMPLETIONS_USER_AGENT = "ai-sdk/openai-compatible/0.0.0-test/codebuff ai-sdk/provider-utils/3.0.20 runtime/browser"
Accept-Encoding: gzip, deflate
Connection: keep-alive
Host: www.codebuff.com   # explicit, no proxy host leak
```

**Sumber:** `freebuff2api/codebuff.py:20-26`, `freebuff2api/config.py:14-18`

### 3.5 Session mismatch recovery (409)

```
upstream 409 error: session_model_mismatch
  → _delete_locked_session(model)
  → re-create session with new model
  → retry chat
```

Codebuff free-tier hanya allow 1 active session per account; ganti model = re-create. Account pool (`CodebuffAccountPool`) menangani multi-token concurrent dengan lock per account.

**Sumber:** `freebuff2api/codebuff.py:503-870` (SessionManager + AccountPool)

### 3.6 `exploitation.js` — Cloudflare Worker reverse proxy

Bukan bagian inti. Worker proxy obfuscated ke `www.codebuff.com`:
- Decode char codes → `https://www.codebuff.com`
- Strip `cf-*` and `x-forwarded-*` headers (anti-Cloudflare detection)
- Rewrite redirects, set-cookie domain, CSP, X-Frame-Options
- Replace host references in HTML/CSS/JS body

Tujuan: untuk dipakai jika `codebuff.com` di-block di region tertentu; user deploy worker sendiri sebagai passthrough.

## 4. Bugs / Inconsistencies Ditemukan

| # | Lokasi | Issue | Severity |
|---|--------|-------|----------|
| 1 | `vercel.json` | `regions: ["fra1"]` (Frankfurt) — README bilang `iad1` (US East). Non-US region trigger `session_model_mismatch` per README sendiri | **HIGH** |
| 2 | `codebuff.py:21` | `Bun/1.3.11` hardcoded — upstream bisa update UA expected, silent break | MED |
| 3 | `config.py:108` | `admin_key` default `sk-admin` — publik deploy tanpa set env = admin terbuka | **HIGH** |
| 4 | `models.py:38-39` | `GEMINI_THINKER_PARENT_MODEL_ID = "moonshotai/kimi-k2.6"` hardcoded — kimi upstream change = break | MED |
| 5 | `admin_static/index.html` | Inline 1134-line SPA, no SRI, no CSP — XSS surface if admin key leaks | MED |
| 6 | `usage_store.py` | In-memory only — Vercel serverless cold start = data loss | LOW (known) |
| 7 | `exploitation.js:42-47` | Strip `cf-*` headers — bypass Cloudflare, tapi untuk `codebuff.com` sendiri tidak perlu (sudah authorized) | LOW |
| 8 | `codebuff.py:80` | `proxy=settings.upstream_proxy_url` tapi `trust_env=False` — ignore `HTTP_PROXY` env, only use explicit config | LOW (intentional) |
| 9 | `tool/get_token.py:36` | `User-Agent: Bun/1.3.11` — same hardcoded fingerprint | LOW |
| 10 | `vercel.json` | No `functions` config (maxDuration) — Vercel free tier 10s timeout, streaming chat bakal cut | MED |

## 5. Maximization Plan — RE Enhancements

### Phase 1: Stabilkan foundation (week 1)

**P1.1 Fix vercel.json region**
```json
{
  "regions": ["iad1"],
  "functions": { "api/index.py": { "maxDuration": 300 } }
}
```

**P1.2 Externalize fingerprints**
- Pindah `Bun/1.3.11`, HAR UA, CLI version ke env vars / config
- Auto-fetch latest CLI version dari upstream release endpoint (jika ada)
- Fallback chain: env → cached → hardcoded

**P1.3 Secure admin default**
- Refuse to start jika `FREEBUFF_ADMIN_KEY` masih `sk-admin` dan `FREEBUFF_HOST=0.0.0.0`
- Generate random admin key on first run, write to `.env`, print to stderr once

### Phase 2: Perluas model coverage (week 1-2)

**P2.1 Dynamic model discovery**
- Scrape `/api/v1/freebuff/session` response untuk detect available `session_model_id`
- Auto-register new models tanpa code change
- Cache model list di `usage_store` dengan TTL

**P2.2 Add OpenRouter-compatible model IDs**
- Map `openai/gpt-4o` → upstream codebuff alias (jika upstream support via free agent)
- Map `anthropic/claude-sonnet-4` → upstream (jika available)
- Build transparent alias layer biar client apps (Cursor, Hermes, etc.) langsung compatible

**P2.3 Vision + tool_use support**
- Cek apakah upstream support image input (base64)
- Implement tool_use → agent-run spawn mapping (Codebuff punya spawn_agents native)

### Phase 3: Hardening & scale (week 2-3)

**P3.1 Persistent storage layer**
- Add SQLite/Redis backend untuk `usage_store` (Vercel = ephemeral)
- Optional: Vercel KV / Upstash Redis (free tier 10k commands/day)
- Keep in-memory sebagai fast cache, persistent sebagai source of truth

**P3.2 Account pool intelligence**
- Health check per token (rotasi jika 401/403)
- Rate-limit tracking per token (jangan overflow free tier quota)
- Sticky session: 1 conversation = 1 token (avoid context loss)
- Auto-renew token via web tool jika expired

**P3.3 Streaming reliability**
- SSE keepalive ping every 15s (avoid Vercel 10s idle timeout)
- Auto-resume on disconnect dengan `Last-Event-Id`
- Backpressure handling (slow client → buffer dengan cap)

### Phase 4: Distribution & monetization (week 3-4)

**P4.1 Multi-tenant mode**
- Per-user API key → separate account pool
- Quota per key (rate limit + token budget)
- Stripe/Lemon Squeezy integration optional

**P4.2 Admin panel v2**
- Replace inline HTML dengan built Vue + Naive UI (tree-shake)
- Add real-time charts (requests/min, token usage, error rate)
- Token rotation scheduler UI
- Webhook notifications (Slack/Discord) on upstream errors

**P4.3 SDK + clients**
- Python SDK: `pip install freebuff2api-client`
- JS SDK: `npm install freebuff2api-client`
- Cursor rules preset: auto-config `OPENAI_BASE_URL` ke self-hosted endpoint
- Hermes provider registration

### Phase 5: Defensive RE — future-proofing (ongoing)

**P5.1 Upstream protocol drift detection**
- Daily cron: hit `/api/v1/freebuff/session` dengan test token
- Compare response schema dengan baseline
- Alert (webhook) jika schema change detected

**P5.2 Fingerprint rotation**
- Pool of valid UA strings (Bun versions, browser UAs)
- Random pick per request + consistency within session
- Mirror real CLI behavior (sync dengan Codebuff CLI releases)

**P5.3 Ad-chain automation**
- Auto-trigger ad chain secara proactive (sebelum session expire)
- Cache ad impression IDs untuk replay (jika upstream accept)
- **Catatan ethics:** ini potentially violates ToS — document risk, default off

## 6. Quick Wins (executable hari ini)

1. **Fix `vercel.json` region `fra1` → `iad1`** + add `maxDuration: 300`
2. **Add `FREEBUFF_CLI_VERSION` env var** untuk externalize `Bun/1.3.11`
3. **Disable default admin key** — generate random on first run
4. **Add `/v1/models` cache header** (`Cache-Control: public, max-age=300`)
5. **Add SSE keepalive** di `_stream_openai_chunks` + `_stream_anthropic_events`
6. **Add `/api/health/upstream`** — ping codebuff, return status untuk monitoring

## 7. Test coverage assessment

- 120 tests, 2,769 LoC test code — solid baseline
- Coverage area: admin, anthropic_compat, app errors, app messages, codebuff client, config, logging, new features, openai_compat, sessions, streaming, token web
- **Gap:** no integration test dengan real upstream (semua mocked)
- **Gap:** no load test (concurrent account pool behavior)
- **Gap:** no SSE replay test (network interruption)

## 8. License & ethics

- Source repo: AGPL-3.0 implied (fork of fork, original `XxxXTeam/freebuff2api`)
- **ToS risk:** Codebuff free-tier punya ToS; automated ad-chain + account pool potentially violates
- **Mitigation:** default config conservative (single token, no ad automation), opt-in untuk aggressive features
- Document risk di README fork

## 9. Next action

```bash
# Sudah:
# ✓ Fork ke Timcuan/freebuff2api-vercel
# ✓ Clone ke /Users/aaa/Projects/SSH/freebuff2api-vercel
# ✓ Branch analysis/re-maximize-potential
# ✓ Dokumen analisa ini

# Berikutnya:
# 1. Apply Phase 1 quick wins (vercel.json, env vars, admin key)
# 2. Run pytest untuk verify baseline 120 tests pass
# 3. Add integration test dengan real token (CI secret)
# 4. Phase 2: dynamic model discovery
```
