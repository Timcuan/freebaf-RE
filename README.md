# freebaf-RE

Reverse-engineering edition of the Freebuff (Codebuff) OpenAI-compatible API gateway.
Deploy on a local machine or VPS and call Freebuff models like OpenAI Chat Completions.

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint (streaming + non-streaming)
- Anthropic-compatible `/v1/messages` endpoint (bidirectional format conversion)
- Multi-account Freebuff token pool with concurrent request routing
- Freebuff Unleash: multi-account × multi-model session pool for 24/7 access
  (bypasses Codebuff's deployment-hours limit on GLM via session persistence)
- Egress region control (route upstream through a US/CA proxy when needed)
- Admin panel at `/admin` for tokens, API keys, request records, logs, network checks
- Local `.env` persistence (no Vercel dependency)

## Quick start (local / VPS)

```bash
git clone https://github.com/Timcuan/freebaf-RE.git
cd freebaf-RE
pip install -r requirements.txt
cp .env.example .env
# edit .env: set FREEBUFF_TOKEN and FREEBUFF_API_KEY
python main.py
```

Open `http://localhost:8000/admin` and sign in with `sk-admin` (change it on the Settings tab).

## Stealth

The gateway mimics the official Codebuff CLI closely enough to avoid bot
detection. Verified against `CodebuffAI/codebuff` source (June 2026),
including the anti-abuse commits:
- `#527` hourly bot-sweep (abuse-detection.ts)
- `#558` harden country gating
- `#628` opaque CLI auth tokens
- `#709` block VPN/proxy/Tor traffic via IPinfo privacy signals

| Signal | Upstream detection | freebaf-RE counter-measure |
|---|---|---|
| Fingerprint format | `codebuff-cli-<8 base64url>` (legacy) or `enhanced-<sha256>` | `stealth.py` generates valid `codebuff-cli-<8 base64url>` |
| Fingerprint stability | cached per machine for process lifetime | persisted per-account to `~/.config/freebaf-re/fingerprints.json` (0600) |
| Fingerprint sharing | bot-sweep flags fingerprints shared across users | unique fingerprint per account (sha256-derived key) |
| sig_hash sharing | bot-sweep catches rotated fingerprints from same device | each account has its own fingerprint, claimed at OAuth — no sharing |
| User-Agent | `codebuff/<version>` (latest: 1.0.682) | `codebuff/1.0.682` (env-overridable) |
| Accept-Language | matches device locale | mapped from `FREEBUFF_LOCALE` (zh-CN → `zh-CN,zh;q=0.9,en;q=0.8`) |
| Device fingerprint | `os`, `timezone`, `locale` in ad-request body | mirrored from settings (`windows`, `Asia/Shanghai`, `zh-CN`) |
| Browser UA (ad providers) | current Chrome | `Chrome/137.0.0.0` |
| TLS JA3/JA4 fingerprint | Cloudflare sees Python ssl stack vs real browser | `curl_cffi` impersonates Chrome124 TLS ClientHello (`FREEBUFF_STEALTH_TLS=true`) |
| VPN/proxy/Tor egress | IPinfo privacy signals → hard-block 403 (commit #709) | `proxy_validation.py` pre-flight rejects flagged proxies before any token is spent |
| Country block | non-US/CA → `session_model_mismatch` 409 | `FREEBUFF_EGRESS_PROXY_URL` routes through residential US/CA proxy |
| 24/7 usage pattern | 50+ msgs in 20+ distinct hours → HIGH tier (score 100) | `rate_governor.py` distributes load + idle windows per account |
| New-account burst | <1d old + 200 msgs → +40 score | governor daily cap 180 < 200 |
| Heavy usage | 500 msgs/24h → +50 score | governor soft cap 40 msgs + 16 hours |
| Plus-alias email | `user+abc123@` → +10 score | login flow uses real OAuth accounts (Google/GitHub), no aliases |
| Email-digits / duck.com / handle-userN | +5/+10/+5 score | real OAuth accounts only |
| **Cross-account correlation** | bot-sweep clusters accounts by shared IP, TLS, UA, timing | `account_identity.py` per-account proxy/TLS/UA/locale/timezone/phase |
| **Activity phase** | concurrent activity across accounts = cluster signal | per-account stagger offset (FREEBUFF_ACCOUNT_STAGGER_MINUTES) |

### Per-account identity isolation (multi-account stealth)

When running multiple accounts from one gateway, the strongest detection
vector is **cross-account correlation** — all accounts share egress IP,
TLS JA3, User-Agent, and timing. Upstream's bot-sweep (commit #527)
clusters accounts by these signals.

`account_identity.py` assigns each account a distinct identity bundle:

- **proxy** — `FREEBUFF_PER_ACCOUNT_PROXY` (distinct residential IP per account)
- **TLS profile** — `FREEBUFF_PER_ACCOUNT_TLS` (rotates curl_cffi browser profiles)
- **CLI version** — `FREEBUFF_PER_ACCOUNT_CLI_VERSION` (distinct User-Agent per account)
- **locale** — `FREEBUFF_PER_ACCOUNT_LOCALE` (distinct Accept-Language)
- **timezone** — `FREEBUFF_PER_ACCOUNT_TIMEZONE` (distinct device timezone)
- **activity phase** — `FREEBUFF_ACCOUNT_STAGGER_MINUTES` (distributes activity across the hour)

Status visible at `/api/health/stealth` (admin-auth required):

```json
{
  "identity": [
    {
      "account_index": 0,
      "proxy_url": "***",
      "tls_profile": "chrome124",
      "cli_version": "1.0.682",
      "locale": "en-US",
      "timezone": "America/New_York",
      "activity_phase_minutes": 0,
      "is_isolated": true
    },
    ...
  ]
}
```

**Recommended multi-account setup:**
1. Login 3-5 accounts via `/admin` (web login flow)
2. Set `FREEBUFF_PER_ACCOUNT_PROXY` to distinct residential proxies (one per account)
3. Leave other identity fields at defaults (auto-rotate)
4. Set `FREEBUFF_IDLE_WINDOW_HOURS=0,8` + `FREEBUFF_LOCAL_OFFSET_HOURS=-5` (US East sleep schedule)
5. Verify at `/api/health/stealth` — `is_isolated: true` for all accounts

### Egress pre-flight

On startup, if `FREEBUFF_EGRESS_AUTO=true` and at least one account is
configured, the gateway runs `validate_egress_for_upstream()` which:

1. Probes the direct egress IP through IPinfo (privacy signals)
2. If `FREEBUFF_EGRESS_PROXY_URL` is set, probes the proxy egress too
3. Classifies each as:
   - **clean** — no privacy signals, premium region → safe
   - **limited** — `hosting` signal only → DeepSeek Flash path only
   - **hard-blocked** — `vpn`/`proxy`/`tor`/`res_proxy` → reject
4. Logs OK/WARN before any token is spent on a doomed session

**Critical:** commercial VPN/SOCKS5 services (NordVPN, ExpressVPN, Mullvad,
etc.) are flagged as `vpn` by IPinfo and will get every account banned.
Use a **residential proxy** service (Bright Data, Soax, Smartproxy) with
US/CA exit IPs — those are not flagged.

### Rate governor

`rate_governor.py` tracks per-account 24h rolling usage and:

1. Skips accounts in their idle window (default 00:00–07:00 local)
2. Skips accounts at daily cap (default 180 < 200 burst flag)
3. Prefers accounts NOT approaching soft cap (40 msgs / 16 hours)
4. Picks the account with lowest `msgs_24h` to distribute load evenly
5. Falls back to least-recently-used when all exhausted (degraded service
   over total outage)
6. Injects 50–350ms jitter before each request to break burst patterns

Status visible at `/api/health/stealth` (admin auth).

### Bot-sweep evasion

Codebuff runs an hourly bot-sweep (commit #527) that emails james@codebuff.com
with ranked suspects. The sweep does NOT auto-ban — it's a dry-run that
feeds manual review. Our counter-measures ensure none of the trigger
signals fire:

- **No fingerprint sharing** — each account has a unique stable fingerprint
- **No sig_hash sharing** — fingerprints are claimed per-user at OAuth
- **No 24/7 pattern** — rate governor keeps usage below 50 msgs / 20 hours
- **No new-account burst** — daily cap 180 < 200 threshold
- **No plus-alias / duck.com / email-digits** — real OAuth accounts only

Monitor your stealth posture:

```bash
curl http://localhost:8000/api/health/stealth -H "Authorization: Bearer $ADMIN_KEY"
```

The response includes egress validation, TLS stealth status, rate governor
per-account usage, and the fingerprint store path.

## Multi-account login

The Codebuff CLI only stores one `default` profile in
`~/.config/codebuff/credentials.json` — logging in a second account
overwrites the first, forcing a logout/re-login every time you switch.
`scripts/login.py` bypasses this so you can stack N accounts into a pool:

```bash
# login a new account (opens browser, polls for authToken)
python scripts/login.py

# append the new token to .env FREEBUFF_TOKEN (comma-joined with existing)
python scripts/login.py --write-env

# list stored tokens
python scripts/login.py list

# verify a token still works
python scripts/login.py verify 0

# remove a token by index
python scripts/login.py remove 0

# print FREEBUFF_TOKEN=tok1,tok2,... for paste into .env
python scripts/login.py export

# use codebuff.com instead of freebuff.com
python scripts/login.py --codebuff
```

Tokens are stored at `~/.config/freebaf-re/tokens.json` (mode 0600) — the
CLI's own `credentials.json` is never touched, so the official CLI keeps
working with whatever account it already had. Each `python scripts/login.py`
run produces a fresh fingerprint and stores the resulting token independently;
run it N times to build an N-account pool, then set
`FREEBUFF_TOKEN=tok1,tok2,...,tokN` in `.env` and restart the gateway.

## Environment variables

Required:
- `FREEBUFF_TOKEN` — one or more Codebuff tokens, comma-separated for multi-account pool
- `FREEBUFF_API_KEY` — key clients use to call `/v1/*` (or set `FREEBUFF_API_KEYS` JSON for multi-key with model whitelists)

Recommended:
- `FREEBUFF_ADMIN_KEY` — admin panel login key (default `sk-admin`, change in production)

Optional:
- `FREEBUFF_EGRESS_PROXY_URL` — SOCKS5/HTTP proxy URL for upstream egress (e.g. `socks5://us-proxy:1080`)
- `FREEBUFF_EGRESS_REGION_REQUIRED` — expected egress country code (e.g. `US`)
- `FREEBUFF_SYSTEM_PROMPT_OVERRIDE` — inject a custom system prompt
- `FREEBUFF_DEBUG` — enable debug logging
- `FREEBUFF_LOG_LEVEL` — `INFO` (default), `DEBUG`, `WARNING`, `ERROR`

See `.env.example` for the full list with descriptions.

## Deployment

### Docker

```bash
docker compose up -d
```

The compose file mounts `./data` for persistent request records and `.env` for config.

### systemd (Linux VPS)

```bash
sudo cp deploy/freebuff2api.service /etc/systemd/system/
sudo systemctl enable --now freebuff2api
```

### Caddy reverse proxy (auto HTTPS)

```bash
sudo cp deploy/Caddyfile /etc/caddy/
sudo systemctl reload caddy
```

See `DEPLOY.md` for full instructions including egress proxy setup.

## API usage

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $FREEBUFF_API_KEY" \
  -d '{
    "model": "z-ai/glm-5.2",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

Compatible with any OpenAI SDK client — set `base_url=http://localhost:8000/v1`.

### Client integration examples

**Python (OpenAI SDK):**
```python
from openai import OpenAI
client = OpenAI(api_key="YOUR_FREEBUFF_API_KEY", base_url="http://localhost:8000/v1")
resp = client.chat.completions.create(
    model="z-ai/glm-5.2",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

**Node.js (openai):**
```js
import OpenAI from "openai";
const client = new OpenAI({ apiKey: "YOUR_FREEBUFF_API_KEY", baseURL: "http://localhost:8000/v1" });
const resp = await client.chat.completions.create({
  model: "z-ai/glm-5.2",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(resp.choices[0].message.content);
```

**curl:**
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_FREEBUFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"Hello"}]}'
```

**Cursor IDE:** Settings → Models → OpenAI API Key = `YOUR_FREEBUFF_API_KEY`, Base URL = `http://localhost:8000/v1`. Model name = `z-ai/glm-5.2`.

**Claude Code / Anthropic SDK:** Use `/v1/messages` with `base_url=http://localhost:8000`. The gateway translates Anthropic ↔ OpenAI formats bidirectionally.

**Agent integration (passthrough):** Set `FREEBUFF_SYSTEM_PROMPT_OVERRIDE=` (empty) in `.env` to disable the default Buffy-neutralizer prefix. Your agent's system prompt passes through to the model untouched.

## Models

Run `curl http://localhost:8000/v1/models -H "Authorization: Bearer $KEY"` for the full list with spec metadata (tier, premium, multimodal, context_window, data_collection).

### Tier 0 — Unlimited (no quota gate, always available)

| Model | Display | Tagline | Multimodal | Context | Data collection |
|---|---|---|---|---|---|
| `minimax/minimax-m3` | MiniMax M3 | Smartest & Fastest | yes | 1M | no (Fireworks) |
| `mimo/mimo-v2.5` | MiMo 2.5 | Multimodal | yes | 128k | no |
| `deepseek/deepseek-v4-flash` | DeepSeek V4 Flash | Smart & Fast | no | 128k | **yes** |
| `minimax/minimax-m2.7` | MiniMax M2.7 | Fastest (legacy) | no | 128k | no |
| `google/gemini-3.1-flash-lite-preview` | Gemini 3.1 Flash Lite | File picker | yes | 1M | no |
| `google/gemini-2.5-flash-lite` | Gemini 2.5 Flash Lite | File picker | yes | 1M | no |

**Default model: `minimax/minimax-m3`** (smartest + fastest, unlimited, no data collection, 1M context).

### Tier 1 — Premium daily (5 sessions/day, reset midnight Pacific)

| Model | Display | Tagline | Multimodal | Context | Thinker |
|---|---|---|---|---|---|
| `deepseek/deepseek-v4-pro` | DeepSeek V4 Pro | Smartest | no | 1M | yes |
| `moonshotai/kimi-k2.6` | Kimi K2.6 | Balanced | yes | 256k | yes |
| `mimo/mimo-v2.5-pro` | MiMo 2.5 Pro | Smartest & Slow | yes | 128k | yes |

### Tier 2 — GLM weekly referral (5 sessions/referral/week, cap 10 = 50/week)

| Model | Display | Tagline | Multimodal | Context |
|---|---|---|---|---|
| `z-ai/glm-5.2` | GLM 5.2 | Unlock by referring friends | no | 128k |

### Tier 3 — Gemini thinker (spawned by Tier 1 parents + MiniMax M3)

| Model | Display | Parent models |
|---|---|---|
| `google/gemini-3.1-pro-preview` | Gemini 3.1 Pro (Thinker) | Kimi, DeepSeek Pro, MiMo Pro, MiniMax M3 |

### Provider priority (newest first)

| Provider | Latest model | Tier |
|---|---|---|
| minimax | `minimax/minimax-m3` | 0 (unlimited) |
| deepseek | `deepseek/deepseek-v4-flash` | 0 (unlimited) |
| mimo | `mimo/mimo-v2.5` | 0 (unlimited) |
| moonshot | `moonshotai/kimi-k2.6` | 1 (premium) |
| zai | `z-ai/glm-5.2` | 2 (referral) |
| google | `google/gemini-3.1-pro-preview` | 3 (thinker) |

## Freebuff Unleash

The Unleash pool pre-warms sessions for all Freebuff models across all configured
accounts and reuses them 24/7.

**Verified loopholes (from CodebuffAI/codebuff source):**

1. **Quota gates run only on POST /session.** Chat completions on an active
   session are NOT quota-checked → one 1h session = unlimited chat requests.
2. **Sessions are bound to a model**, but the server uses `session.model` for
   chat. We cache one session per (account, model) pair and reuse without
   re-admission.
3. **One instance per account, not per IP** → N accounts = N concurrent
   sessions = N× throughput.
4. **No per-session rate limit on chat completion** → unlimited concurrent
   requests within an active session.
5. **Sessions live 1h, refresh can run while still active** → pre-emptive
   refresh at the 55-min mark, atomic switch.

**GLM 5.2 gate (verified):** `availability: 'always'`, gated by the **weekly
referral pool** (5 sessions/referral/week, cap 10 referrals). Tier-0 accounts
(no referrals) get 0 GLM sessions/week. The Unleash pool bypasses this by
rotating across accounts that DO have referral quota — once a session is
admitted, it serves unlimited chat completions for 1h.

**Premium daily pool** (DeepSeek Pro, Kimi, MiMo Pro): 5 sessions/day for
tier-0 accounts, resets at midnight Pacific. Bypassed via multi-account
rotation: N tier-0 accounts = 5N premium sessions/day.

**Non-premium models** (DeepSeek Flash, MiMo, MiniMax M2.7/M3): no upstream
quota gate, unlimited sessions/day on any account.

**Account health registry** tracks per-account quota per pool. On 429
`rate_limited`, the account is marked exhausted for that pool and skipped
until `resetAt`; the next account with quota is picked automatically.

Background warmup runs every 30s and pre-emptively refreshes sessions
approaching the 55-min mark (only on accounts with remaining quota for the
model's pool).

Monitor status:

```bash
curl http://localhost:8000/api/health/glm52 -H "Authorization: Bearer $KEY"
```

The response includes `account_health` with per-account quota snapshots for
both the premium daily pool and the GLM weekly referral pool.

## Development

```bash
pip install -e .
pytest tests/ -q
```

## License

See `LICENSE`.
