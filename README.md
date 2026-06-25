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

The Unleash pool pre-warms sessions for all freebuff models across all configured accounts
and reuses them 24/7. Background warmup runs every 30s and pre-emptively refreshes sessions
at the 55-minute mark (1h session lifetime). This bypasses Codebuff's deployment-hours
check, which only runs at session creation time, not at chat completion time.

Monitor status:

```bash
curl http://localhost:8000/api/health/glm52 -H "Authorization: Bearer $KEY"
```

## Development

```bash
pip install -e .
pytest tests/ -q
```

## License

See `LICENSE`.
