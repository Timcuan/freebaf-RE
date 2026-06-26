# Stealth & long-run operations

Guide for running freebaf-RE 24/7 on a VPS or local machine without tripping
Codebuff's bot-sweep (commit `#527`) or leaking memory over weeks of uptime.

## Architecture overview

```
Client (Cursor / Claude Code / SDK)
        │
        ▼
  /v1/chat/completions  or  /v1/messages
        │
        ├─ RateGovernor ── jitter + account pick + usage caps
        │
        ├─ AccountIdentityRegistry ── per-account proxy/TLS/UA/locale/phase
        │
        └─ UnleashPool ── cached sessions per (account × model)
                │
                ├─ background warmup (30s tick, 3s stagger per account)
                ├─ preemptive refresh at 55 min
                └─ ad-chain cache (TTL 30 min, max 32 entries)
```

## Stealth layers

| Layer | Module | What it does |
|-------|--------|--------------|
| Fingerprint | `stealth.py` | Stable `codebuff-cli-<8>` per account, persisted to disk |
| TLS JA3/JA4 | `stealth_transport.py` | `curl_cffi` Chrome impersonation per account |
| Identity bundle | `account_identity.py` | Distinct proxy, TLS, CLI version, locale, timezone, `client_id`, `session_id`, `device_os`, `browser_ua`, activity phase |
| Egress validation | `proxy_validation.py` | IPinfo privacy signals — reject VPN/proxy/Tor before spending tokens |
| Rate governor | `rate_governor.py` | Per-account 24h rolling caps, idle windows, phase stagger, jitter |
| Ad-chain dedup | `codebuff.py` | Skip repeat ad impressions; bounded cache for long runs |
| Unleash pool | `freebuff_unleash.py` | Multi-account session cache, warmup, refresh, health tracking |
| Account health | `account_health.py` | Per-pool quota (premium daily, GLM weekly), ban/geo-block flags |

## Upstream bot-sweep signals (commit #527)

Codebuff ranks suspects hourly (dry-run → manual review). Triggers we evade:

| Signal | Threshold | Our counter |
|--------|-----------|-------------|
| 24/7 usage | ≥50 msgs AND ≥20 distinct hours / 24h | Soft cap 40 msgs + 16 hours; multi-account spread |
| Heavy usage | ≥500 msgs / 24h | Daily hard cap 180 |
| New-account burst | <1 day old + ≥200 msgs | Daily cap 180 |
| Fingerprint sharing | same `fingerprint_id` across users | Unique fingerprint per account |
| sig_hash sharing | same device hash across users | Per-account fingerprint at OAuth |
| Cross-account correlation | shared IP, TLS, UA, timing | `FREEBUFF_PER_ACCOUNT_PROXY` + identity isolation |
| VPN/proxy/Tor | IPinfo privacy flags | Egress pre-flight rejects flagged proxies (#709) |

## Per-account identity isolation

**Required for multi-account stealth:** distinct residential proxies.

```dotenv
FREEBUFF_IDENTITY_ISOLATION=true
FREEBUFF_PER_ACCOUNT_PROXY=socks5://u:p@res1:1080,socks5://u:p@res2:1080,socks5://u:p@res3:1080
FREEBUFF_PER_ACCOUNT_TLS=chrome124,chrome120,safari17_0
FREEBUFF_LOCAL_OFFSET_HOURS=-5
FREEBUFF_ACCOUNT_STAGGER_MINUTES=15
```

Each account gets a deterministic bundle (stable across restarts):

| Field | Purpose |
|-------|---------|
| `proxy_url` | Distinct egress IP (strongest isolation signal) |
| `tls_profile` | Distinct JA3/JA4 (curl_cffi profile) |
| `cli_user_agent` | `codebuff/<version>` variation |
| `locale` / `accept_language` | Accept-Language header |
| `timezone` | Device timezone in ad-request body |
| `client_id` | 11-char hex (upstream CLI format) |
| `session_id` | UUID per account |
| `device_os` | `windows` / `macos` / `linux` |
| `browser_ua` | Chrome/Firefox/Safari for ad providers |
| `activity_phase_minutes` | Minute-of-hour offset for stagger |

Verify at `/api/health/stealth` — every account should show `"is_isolated": true`
when per-account proxies are configured.

## Rate governor

Tracks per-account usage in a 24h rolling window plus a daily counter that
resets at **local midnight** (not `now + 86400` drift).

### Defaults

| Setting | Default | Purpose |
|---------|---------|---------|
| Daily hard cap | 180 msgs | Below new-account burst flag (200) |
| Soft msg cap | 40 msgs / 24h | Below HIGH-tier flag (50) |
| Soft hours cap | 16 distinct hours / 24h | Below HIGH-tier flag (20) |
| Jitter | 50–350 ms | Break burst timing patterns |
| Idle window | **disabled** | Opt-in via env |
| Activity stagger | 15 min | Spread accounts across the hour |

### Pick logic

1. Exclude accounts at **hard daily cap** → if all capped, return `-1` (caller backs off 2–8s and retries).
2. Prefer **strict-eligible** accounts (not in idle window, in activity phase).
3. Prefer accounts **below soft cap** (msgs + distinct hours).
4. Pick account with **lowest `msgs_24h`**.
5. If only idle/phase-blocked accounts remain → **soft fallback** (still pick least-loaded; never blind round-robin bypass).

Both `/v1/chat/completions` and `/v1/messages` use the governor.

### Idle window (opt-in)

```dotenv
FREEBUFF_IDLE_WINDOW_HOURS=0,8      # sleep 00:00–08:00 local
FREEBUFF_LOCAL_OFFSET_HOURS=-5      # US Eastern
```

When enabled, accounts in idle are deprioritized but the gateway still serves
requests via soft fallback unless you rely on hard cap only.

## Freebuff Unleash (long-run)

### Session lifecycle

- Sessions live **1 hour** upstream.
- Gateway caches one session per **(account, model)** pair.
- **Pre-emptive refresh** at 55 minutes (atomic per-slot lock).
- **Background warmup** every 30 seconds.
- **Warmup stagger:** 3 seconds between accounts (avoids simultaneous creation burst).

### Warmup models

`unleash_warmup_models()` pre-warms tier 0–2 models (excludes Gemini thinker tier 3):

- `minimax/minimax-m3`, `mimo/mimo-v2.5`, `deepseek/deepseek-v4-flash`, `minimax/minimax-m2.7`
- `deepseek/deepseek-v4-pro`, `moonshotai/kimi-k2.6`, `mimo/mimo-v2.5-pro`
- `z-ai/glm-5.2`

### Account health

On HTTP 429 `rate_limited`, the account is marked **exhausted** for that pool
(`pacific_day` or `pacific_week`) until `resetAt`. Banned (403) or geo-blocked
(451) accounts are skipped and their cached slots are **cleared**.

### Ad-chain cache bounds

Each account's `SessionManager` keeps an ad impression dedup cache:

- TTL: 30 minutes
- Max entries: 32 (LRU prune on each refresh)

Without pruning, a 55-minute refresh loop over 7 days × N models would grow
unbounded. Safe for weeks/months of uptime.

## Monitoring

### Stealth diagnostic (admin auth)

```bash
curl -s http://localhost:8000/api/health/stealth \
  -H "Authorization: Bearer $FREEBUFF_ADMIN_KEY" | jq
```

Response includes:

```json
{
  "egress": { "direct": {...}, "proxy": {...}, "ok": true },
  "stealth_tls": { "available": true, "profile": "chrome124", "enabled": true },
  "rate_governor": {
    "daily_msg_cap": 180,
    "soft_msg_cap": 40,
    "soft_hours_cap": 16,
    "accounts": [
      {
        "account_index": 0,
        "msgs_24h": 12,
        "distinct_hours_24h": 4,
        "daily_msg_count": 12,
        "in_idle_window": false,
        "in_activity_phase": true,
        "activity_phase_minutes": 0
      }
    ]
  },
  "identity": [...],
  "longrun": {
    "ad_chain_cache_entries": [3, 2, 1],
    "unleash_active_slots": 21,
    "unleash_inflight_tasks": 0
  },
  "fingerprint_store": "/Users/you/.config/freebaf-re/fingerprints.json"
}
```

### Unleash / GLM health

```bash
curl -s http://localhost:8000/api/health/glm52 \
  -H "Authorization: Bearer $FREEBUFF_API_KEY" | jq '.account_health'
```

### What to watch (cron / alerting)

| Metric | Healthy | Action if bad |
|--------|---------|---------------|
| `longrun.ad_chain_cache_entries[*]` | ≤ 32 each | Should never exceed 32; if higher, file a bug |
| `longrun.unleash_inflight_tasks` | 0–5 typically | Stuck >20 → check logs for warmup failures |
| `rate_governor.accounts[].msgs_24h` | < 40 per account | Add accounts or reduce load |
| `rate_governor.accounts[].distinct_hours_24h` | < 16 | Enable idle window or stagger |
| `identity[].is_isolated` | `true` all | Set `FREEBUFF_PER_ACCOUNT_PROXY` |
| `egress.ok` | `true` | Fix proxy — commercial VPN gets 403 |

## Recommended production setup

1. **3–5 OAuth accounts** via `python scripts/login.py --write-env`
2. **Distinct residential proxies** — one per account (US/CA exit)
3. **US VPS** or non-US VPS + residential US proxy (not NordVPN/Mullvad)
4. **Optional sleep schedule:** `FREEBUFF_IDLE_WINDOW_HOURS=0,8` + `FREEBUFF_LOCAL_OFFSET_HOURS=-5`
5. **Agent passthrough:** `FREEBUFF_SYSTEM_PROMPT_OVERRIDE=` (empty string)
6. **Verify** `/api/health/stealth` after deploy
7. **Cron keep-warm:** `curl http://localhost:8000/api/keep-warm` every 5 min (optional)

## Residual risks

These are operational, not code bugs:

1. **Single shared IP** without per-account proxies — strongest correlation vector remains.
2. **Commercial VPN SOCKS5** — IPinfo flags as `vpn`; all accounts get 403.
3. **All accounts hard-capped** — gateway backs off; requests may queue until local midnight reset.
4. **GLM without referral quota** — tier-0 accounts get 0 GLM sessions/week upstream; rotate accounts with referral quota or use tier-0 unlimited models (`minimax/minimax-m3`).

## Tests

Long-run behavior is covered by `tests/test_stealth_longrun.py`:

- Ad-chain cache prune (TTL + max entries)
- Governor local midnight reset (no drift)
- Activity phase stagger
- Hard cap → `-1` only at daily limit
- `AccountHealthRegistry.mark_success(i, pool)` / `mark_exhausted`
- `unleash_warmup_models()` includes MiniMax M3 + GLM + premium

Run full suite:

```bash
pytest tests/ -q
# Expect 415+ passing
```
