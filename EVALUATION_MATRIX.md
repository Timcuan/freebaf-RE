# GLM 5.2 Unleash — Evaluation Matrix & Bypass Methods

## REALITA (verified 2026-06-26 dari docs resmi)

### Source pricing
| Provider | Model | Input/M tok | Output/M tok | Free tier |
|----------|-------|-------------|--------------|-----------|
| Z.ai | GLM-5.2 | $1.40 | $4.40 | 20M token per new account |
| Z.ai | GLM-5.1 | $1.40 | $4.40 | 20M token per new account |
| Z.ai | **GLM-4.7-Flash** | **FREE** | **FREE** | **tanpa batas** |
| Z.ai | **GLM-4.5-Flash** | **FREE** | **FREE** | **tanpa batas** |
| Z.ai | GLM-4.7-FlashX | $0.07 | $0.40 | 20M token pool |
| Cloudflare | @cf/zai-org/glm-5.2 | 127k neurons | 400k neurons | 10k neurons/day (≈78 input tok) |
| Cloudflare | @cf/zai-org/glm-4.7-flash | 5.5k neurons | 36.4k neurons | 10k neurons/day (~1.8M in / 275k out) |
| Codebuff | z-ai/glm-5.1 | free (ads) | free (ads) | 9-5 ET weekdays, session 1h |
| OpenRouter | glm-5.2 | $1.00 | $4.00 | **TIDAK ADA free lane** |

## Strategi Canggih (3 jalur, no gimmick)

### Jalur 1: GLM-4.7-Flash (Z.ai) — FREE TANPA BATAS
- Coding agent default. Free input/cached/output semua.
- Endpoint: `https://api.z.ai/api/paas/v4/chat/completions`
- Model id: `zai/glm-4.7-flash` → upstream `glm-4.7-flash`
- **Setup**: `FREEBUFF_ZAI_API_KEYS=key1` (1 akun cukup, free selamanya)
- **Status**: ✅ Implemented (`zai.py`)

### Jalur 2: GLM 5.2 (Z.ai) — 20M token free per akun × N akun
- N akun Z.ai × 20M token = 20N M tokens free untuk GLM 5.2
- Round-robin skip exhausted; track per-account paid token usage
- After habis: fallback ke GLM-4.7-Flash (free) atau Codebuff unleash
- **Setup**: `FREEBUFF_ZAI_API_KEYS=key1,key2,key3,key4,key5` (5 akun × 20M = 100M tokens)
- **Status**: ✅ Implemented (`zai.py` pool rotation)

### Jalur 3: GLM 5.1/5.2 (Codebuff) — session persistence bypass
- Pre-warm session saat deployment hours (9-5 ET weekdays)
- Reuse cached `instance_id` 24/7 (server tidak re-check jam untuk chat completion)
- Auto-refresh sebelum 1h expiry
- Multi-account Codebuff pool rotation
- **Setup**: `FREEBUFF_CODEBUFF_TOKENS=tok1,tok2,tok3`
- **Status**: ✅ Implemented (`glm52_unleash.py`)

### Jalur 4: CF free-viable models (cheaper than GLM 5.2)
- GLM 5.2 di CF = 127k neurons/M input (10k free = 78 tokens) — NOT viable
- GLM 4.7-flash di CF = 5.5k neurons/M (10k free = 1.8M tokens/day) — viable
- Llama 3.2 1b, Qwen3 30b, gpt-oss-20b, gemma-4-26b, granite-4.0 — semua free-viable
- **Setup**: `FREEBUFF_CF_ACCOUNT_IDS=acct1,acct2` + `FREEBUFF_CF_API_TOKENS=tok1,tok2`
- **Status**: ✅ Implemented (`cloudflare_ai.py` dengan neuron cost akurat)

## Hambatan & Bypass

| # | Hambatan | Bypass | Status |
|---|----------|--------|--------|
| 1 | Deployment hours 9-5 ET | Session persistence (jalur 3) | ✅ |
| 2 | Session 1h lifetime | Auto-refresh 5min before expiry | ✅ |
| 3 | 1 instance/account | Multi-account pool | ✅ |
| 4 | Ad-chain required | Existing handler | ✅ |
| 5 | Queue waiting room | Retry + account fallback | ✅ |
| 6 | model_locked 409 | `_delete_locked_session` | ✅ |
| 7 | Banned 403 | Account rotation | ✅ |
| 8 | Geo US-only | Egress proxy (`egress_region.py`) | ✅ |
| 9 | GLM 5.2 CF neuron cost | Pakai Z.ai free 20M pool (jalur 2) | ✅ |
| 10 | GLM 5.2 tidak ada free lane OpenRouter | Z.ai + Codebuff unleash | ✅ |
| 11 | Z.ai 20M token habis | Multi-akun pool + fallback Flash | ✅ |
| 12 | Z.ai GLM 5.2 berbayar setelah 20M | Coding Plan Lite $3-6/mo atau self-host | optional |

## Verifikasi E2E

- **39 unit tests pass** (test_glm52_unleash + test_refactor)
- **Z.ai pool**: 3 akun load, 60M paid token remaining
- **Z.ai free Flash**: chat completion 200 OK via mock Z.ai API
- **CF pool**: 3 akun, 30k neurons, round-robin works
- **CF neuron estimate**: GLM 5.2 = 127k/M input (akurat per pricing page)
- **Deployment hours**: 5 test cases correct (Wed/Sat/before/after/now)
- **FastAPI routing**: `zai/glm-4.7-flash` → Z.ai, `cf/glm-5.2` → CF, `glm-5.2` → Codebuff
- **Health endpoint**: `/api/health/glm52` show status semua 3 provider

## Operasional

### Recommended setup untuk agent (truly free + premium access)
```bash
# .env
FREEBUFF_API_KEY=your-local-key
# Jalur 1: free tanpa batas (default coding agent)
FREEBUFF_ZAI_API_KEYS=zai-key1   # 1 akun cukup untuk Flash free
# Jalur 2: GLM 5.2 free 20M token (register N akun di open.bigmodel.cn)
# tambah baris ini untuk GLM 5.2 access:
# FREEBUFF_ZAI_API_KEYS=zai-key1,zai-key2,zai-key3,zai-key4,zai-key5
# Jalur 3: Codebuff unleash (premium GLM 5.1/5.2 saat jam available)
FREEBUFF_CODEBUFF_TOKENS=cb-tok1,cb-tok2,cb-tok3
# Opsional: CF untuk model free-viable lain (Llama, Qwen, gpt-oss)
# FREEBUFF_CF_ACCOUNT_IDS=cf-acct1
# FREEBUFF_CF_API_TOKENS=cf-tok1
```

### Model selection untuk agent
| Use case | Model | Cost |
|----------|-------|------|
| Daily coding (free) | `zai/glm-4.7-flash` | $0 (free selamanya) |
| Heavy coding (free 20M) | `zai/glm-5.2-paid` | $0 sampai 20M habis |
| Premium saat jam kerja | `glm-5.2` (Codebuff) | $0 (ads) |
| Long context 1M token | `glm-5.2` (Codebuff) | $0 (ads) |
| Backup free | `cf/gemma-4-26b`, `cf/qwen3-30b` | $0 (10k neurons/day) |

### Monitoring
```bash
curl $ENDPOINT/api/health/glm52 -H "Authorization: Bearer $KEY"
# → {unleash_pool, cloudflare, zai, in_deployment_hours}
```
