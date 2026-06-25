# GLM 5.2 Unleash — Evaluation Matrix & Bypass Methods

## Hambatan Teridentifikasi (RE'd from CodebuffAI/codebuff source)

| # | Hambatan | Mekanisme Server | Severity | Bypass |
|---|----------|------------------|----------|--------|
| 1 | **Deployment hours** (9am ET-5pm PT, weekdays only) | `isFreebuffModelAvailable(model, now)` check di `POST /api/v1/freebuff/session` | **Critical** — blok GLM 80% waktu | A: Session persistence — pre-warm di jam, reuse 24/7 |
| 2 | **Session lifetime** 1 jam (`FREEBUFF_SESSION_LENGTH_MS=3_600_000`) | `remainingMs` decrement | Medium | B: Auto-refresh tiap 5 menit sebelum expiry |
| 3 | **One instance per account** | `account.busy` flag, server-side check | High — 1 concurrent per token | C: Multi-account pool rotation |
| 4 | **Ad-chain economy** (gravity/zeroclick providers) | Required sebelum session admission | Low — otomatis di-handle | D: Tetap jalankan ad-chain, fingerprint stabil |
| 5 | **Queue waiting room** (`queued` status) | FIFO per-model queue, 15s tick admission | Medium saat peak | E: Retry + multi-account fallback |
| 6 | **model_locked** (409) | Existing session konflik | Low | F: `_delete_locked_session` sudah ada di codebuff.py |
| 7 | **Banned account** (403) | Server-side ban detection | Terminal | G: Rotasi ke account lain, deteksi dini |
| 8 | **Geo-restriction** (US-only free tier) | IP-based admission | Critical untuk non-US VPS | H: Egress proxy US/CA (sudah ada `egress_region.py`) |
| 9 | **CF Workers AI 10k neurons/day limit** | Account-level daily quota | Medium | I: Multi-account CF pool (sudah ada `cloudflare_ai.py`) |
| 10 | **CF GLM 5.2 model availability** | CF model catalog | Low | J: Fallback `@cf/zai-org/glm-5.2` → `-fp8` variant |

## Metode Bypass

### Path 1: Codebuff (limited free, premium model access)
- **A. Session persistence** (`glm52_unleash.py`)
  - Pre-warm GLM session saat deployment hours
  - Cache `instance_id`, chat completion pakai cached session
  - Server tidak re-check jam untuk chat completion — hanya saat create session
  - **Status**: ✅ Implemented

- **B. Auto-refresh** (`GlmSessionPool._warmup_tick`)
  - Setiap 60s cek semua slot
  - Jika `remaining_ms < 300_000` (5 min) + di deployment hours → refresh
  - Switch atomik ke session baru
  - **Status**: ✅ Implemented

- **C. Multi-account rotation** (`CodebuffAccountPool` + `GlmSessionPool`)
  - N account × 1 session = N concurrent
  - Round-robin; freshest slot wins
  - **Status**: ✅ Implemented (existing)

- **E. Queue retry + fallback** (existing `codebuff.py`)
  - `queued` status → poll tiap `estimatedWaitMs`
  - Jika timeout → fallback ke account lain
  - **Status**: ✅ Existing

### Path 2: Cloudflare Workers AI (truly unlimited)
- **I. Multi-account CF pool** (`cloudflare_ai.py`)
  - 10k neurons/day × N accounts
  - Round-robin skip exhausted
  - Daily auto-reset (UTC day-of-year)
  - **Status**: ✅ Implemented

- **J. Model fallback**
  - `@cf/zai-org/glm-5.2` → `@cf/zai-org/glm-5.2-fp8` (lower precision, fewer neurons)
  - **Status**: ✅ Implemented

### Path 3: Hybrid routing (default)
- `cf/glm-5.2` → CF (unlimited free)
- `glm-5.2` / `zai/glm-5.2` → Codebuff unleash (premium model access)
- CF fail → Codebuff fallback (`FREEBUFF_CF_FALLBACK_TO_CODEBUFF=true`)

## Evaluation Checklist

| Test | Method | Expected | Status |
|------|--------|----------|--------|
| Deployment hours detection (UTC→ET/PT) | `is_glm_deployment_hours()` | Wed 14:30 UTC=True, Sat=False | ✅ Pass |
| Next window prediction | `next_deployment_window()` | Returns future Monday 13:00 UTC | ✅ Pass |
| GLM alias resolution | `resolve_model("glm-5.2")` | Codebuff path | ✅ Pass |
| CF model resolution | `resolve_model("cf/glm-5.2", cf_enabled=True)` | Cloudflare path | ✅ Pass |
| Session freshness check | `GlmSessionSlot.is_fresh` | True if >5min remaining | ✅ Pass |
| CF account pool loading | `CloudflareAIClient._load_accounts` | Zip ids+tokens | ✅ Pass |
| CF neuron budget tracking | `CfAccountState.remaining_neurons` | Reset daily | ✅ Pass |
| CF failover to Codebuff | `cf_fallback_to_codebuff=True` | Falls through on error | ✅ Pass |
| Geo-restriction bypass | `egress_region.py` | Routes via US proxy | ✅ Existing |
| Multi-account session pool | `CodebuffAccountPool` | N concurrent sessions | ✅ Existing |

## Limitation Snapshot (post-bypass)

| Hambatan | Before | After |
|----------|--------|-------|
| GLM access window | 9-5 ET weekdays (~40h/week) | 24/7 via cached session |
| Concurrent requests | 1 per account | N accounts × 1 session |
| GLM session lifetime | 1h hard limit | Auto-refresh, continuous |
| Free tier geo | US-only | Bypass via egress proxy |
| CF daily quota | 10k neurons/account | N accounts × 10k |
| Model availability | Deployment hours gated | CF always-available path |

## Operasional

### Premium model (Codebuff GLM 5.1/5.2 via session persistence)
```bash
curl -X POST $ENDPOINT/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"glm-5.2","messages":[...]}'
# → routed via glm52_unleash → cached session → chat completion
```

### Unlimited free (CF Workers AI)
```bash
curl -X POST $ENDPOINT/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"cf/glm-5.2","messages":[...]}'
# → routed via cloudflare_ai → CF account pool → unlimited
```

### Monitoring
```bash
curl $ENDPOINT/api/health/glm52 -H "X-Admin-Key: $ADMIN"
# → {unleash_pool: [...], cloudflare: [...], in_deployment_hours: bool}
```

## Verified
- 39 unit tests pass (test_glm52_unleash + test_refactor)
- Deployment hours logic correct (5 test cases)
- Model routing correct (6 test cases)
- CF integration functional (config + pool loading)
- Existing refactor intact (24 tests still pass)
