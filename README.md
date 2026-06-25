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

## Models

Run `curl http://localhost:8000/v1/models -H "Authorization: Bearer $KEY"` for the full list.

Highlights:
- `z-ai/glm-5.2` — Z.AI GLM 5.2 (smartest, referral-gated weekly pool upstream; cached 24/7 via Unleash session persistence)
- `minimax/minimax-m2.7` — MiniMax (fastest, always available)
- `moonshotai/kimi-k2.6` — Kimi
- `deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4-flash` — DeepSeek

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
