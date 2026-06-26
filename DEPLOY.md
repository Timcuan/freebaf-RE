# Deploy Guide — Local / VPS

> Single codebase runs on local or VPS. Pick the deployment that fits your use case.

| Option | Best for | Cost | Streaming | Region control |
|--------|----------|------|-----------|----------------|
| **Local** | Dev, agent on same machine | 0 | unlimited | Manual proxy |
| **VPS (US)** | Production agent (Cursor/Hermes) | $4-6/mo | unlimited | Native US IP |
| **VPS (non-US) + proxy** | Asia/EU dev with US proxy | $4-12/mo | unlimited | SOCKS5/HTTP |
| **Docker** | VPS with Caddy + auto-HTTPS | same | unlimited | Same as host |

## 1. Local (dev)

```bash
cp .env.example .env
# Edit .env: set FREEBUFF_TOKEN, FREEBUFF_API_KEY, FREEBUFF_ADMIN_KEY

pip install -r requirements.txt
python main.py
# → http://127.0.0.1:8000/healthz
```

Deploy mode is auto-detected. Egress check runs at startup and warns if non-US.

## 2. VPS (US recommended for agents)

### 2a. Direct (without Docker)

```bash
# On the VPS (Ubuntu 22.04+):
sudo useradd -r -s /bin/false -d /opt/freebuff2api freebuff
sudo mkdir -p /opt/freebuff2api/data
sudo chown freebuff:freebuff /opt/freebuff2api

# Clone + install
sudo -u freebuff -H git clone https://github.com/Timcuan/freebaf-RE.git /opt/freebuff2api
cd /opt/freebuff2api
sudo -u freebuff -H python3 -m venv .venv
sudo -u freebuff -H .venv/bin/pip install -r requirements.txt

# Config
sudo -u freebuff -H cp .env.example .env
sudo -u freebuff -H nano .env  # set tokens, api key, admin key

# systemd service
sudo cp deploy/freebuff2api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freebuff2api
sudo systemctl status freebuff2api
sudo journalctl -u freebuff2api -f
```

### 2b. Docker Compose

```bash
cp .env.example .env
# Edit .env

docker compose up -d --build
docker compose logs -f
curl http://127.0.0.1:8000/healthz
```

### 2c. Caddy reverse proxy (auto HTTPS)

```bash
# Install Caddy: https://caddyserver.com/docs/install
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
# Edit the domain in the Caddyfile
sudo systemctl reload caddy
# → https://api.example.com/healthz
```

Caddy auto-issues a Let's Encrypt cert, is stream-friendly (SSE flush -1), and adds security headers.

## 3. VPS (non-US) + egress proxy

If your VPS is in Asia/EU/non-premium region, set:

```dotenv
FREEBUFF_EGRESS_AUTO=true
FREEBUFF_EGRESS_PROXY_URL=socks5://user:pass@us-proxy.example.com:1080
# or
FREEBUFF_EGRESS_PROXY_URL=socks5h://us-proxy.example.com:1080  # DNS over proxy
```

Verify egress:

```bash
curl http://127.0.0.1:8000/api/health/egress -H "Authorization: Bearer $FREEBUFF_ADMIN_KEY"
# Output:
# {
#   "deploy_mode": "vps",
#   "direct": {"country": "ID", "is_premium": false, ...},
#   "proxy": {"country": "US", "is_premium": true, ...},
#   "ok": true,
#   "recommended_action": "proxy egress is premium — upstream requests will route correctly"
# }
```

US proxy sources:
- **Commercial:** Smartproxy, Bright Data, Oxylabs (~$50-100/mo for 5-20GB)
- **Self-hosted:** small US VPS ($4) + `ssh -D 1080` (SOCKS5) or `gost`/`3proxy`
- **Free (unreliable):** tor (`socks5://127.0.0.1:9050`, US exit node but slow and often blocked by CDNs)

## 4. Available models

Default model: **`minimax/minimax-m3`** (tier 0, unlimited, 1M context, no data collection).

Full tier list and metadata: `curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $KEY"`.

| Model ID | Tier | Notes |
|----------|------|-------|
| `minimax/minimax-m3` | 0 | **Default** — smartest + fastest, unlimited |
| `mimo/mimo-v2.5` | 0 | Multimodal, unlimited |
| `deepseek/deepseek-v4-flash` | 0 | Fast; upstream data-collection warning |
| `minimax/minimax-m2.7` | 0 | Legacy fastest |
| `google/gemini-3.1-flash-lite-preview` | 0 | File picker, 1M context |
| `google/gemini-2.5-flash-lite` | 0 | File picker |
| `deepseek/deepseek-v4-pro` | 1 | Premium daily (5 sessions/day) |
| `moonshotai/kimi-k2.6` | 1 | Premium daily, 256k context |
| `mimo/mimo-v2.5-pro` | 1 | Premium daily |
| `z-ai/glm-5.2` | 2 | GLM weekly referral pool; 24/7 via Unleash |
| `glm-5.2` | 2 | Short alias → `z-ai/glm-5.2` |
| `google/gemini-3.1-pro-preview` | 3 | Thinker (spawned by tier-1 parents) |

**Alias auto-resolve:**
- `claude-sonnet-4`, `claude-3-5-sonnet`, `claude-3-7-sonnet` → default (`minimax/minimax-m3`)
- `gpt-4o`, `gpt-4`, `gpt-5` → MiniMax M3 / DeepSeek Pro
- `gemini-pro`, `gemini-3.1-pro` → `google/gemini-3.1-pro-preview`

Clients (Cursor, Hermes, Claude Code, OpenAI SDK) are compatible with no extra configuration.

GLM 5.1/5.2 access is extended to 24/7 by the Freebuff Unleash pool — see README.

## 5. Health check endpoints

```bash
# Basic
curl http://127.0.0.1:8000/healthz -H "Authorization: Bearer $FREEBUFF_API_KEY"

# Egress diagnostic
curl http://127.0.0.1:8000/api/health/egress -H "Authorization: Bearer $FREEBUFF_ADMIN_KEY"

# Stealth + long-run diagnostic (governor, identity, cache bounds)
curl http://127.0.0.1:8000/api/health/stealth -H "Authorization: Bearer $FREEBUFF_ADMIN_KEY"

# Unleash / GLM pool + account health
curl http://127.0.0.1:8000/api/health/glm52 -H "Authorization: Bearer $FREEBUFF_API_KEY"

# Upstream connectivity
curl http://127.0.0.1:8000/api/health/upstream -H "Authorization: Bearer $FREEBUFF_API_KEY"

# Keep-warm (no auth, for cron ping)
curl http://127.0.0.1:8000/api/keep-warm
```

See [`docs/stealth-longrun.md`](docs/stealth-longrun.md) for interpreting stealth metrics.

## 6. Verification checklist

- [ ] `python -m pytest -q` → 415+ tests passing
- [ ] `python main.py` starts, prints `deploy_mode=...`, egress check OK
- [ ] `/healthz` returns 200
- [ ] `/v1/models` lists models including `minimax/minimax-m3` and `z-ai/glm-5.2`
- [ ] `/api/health/egress` shows `direct.is_premium=true` (or `proxy.is_premium=true`)
- [ ] `/api/health/stealth` shows `identity[].is_isolated=true` (multi-account + per-account proxy)
- [ ] `/api/health/upstream` returns `status=ok`
- [ ] `/v1/chat/completions` with `model=minimax/minimax-m3` succeeds (non-stream + stream)
- [ ] `/admin` login with `FREEBUFF_ADMIN_KEY`

## 7. Long-run ops (24/7 VPS)

Multi-account stealth checklist:

```dotenv
FREEBUFF_TOKEN=tok1,tok2,tok3
FREEBUFF_PER_ACCOUNT_PROXY=socks5://...,socks5://...,socks5://...
FREEBUFF_LOCAL_OFFSET_HOURS=-5
FREEBUFF_ACCOUNT_STAGGER_MINUTES=15
FREEBUFF_SYSTEM_PROMPT_OVERRIDE=
```

Weekly monitoring (cron + jq):

```bash
# Stealth posture
curl -s http://127.0.0.1:8000/api/health/stealth \
  -H "Authorization: Bearer $FREEBUFF_ADMIN_KEY" | jq '.longrun, .rate_governor.accounts'

# Keep-warm every 5 min (crontab)
*/5 * * * * curl -sf http://127.0.0.1:8000/api/keep-warm >/dev/null
```

Watch for:
- `ad_chain_cache_entries` — should stay ≤ 32 per account
- `msgs_24h` per account — keep under soft cap (40) for stealth
- `egress.ok` — false means fix proxy before accounts get banned

Full guide: [`docs/stealth-longrun.md`](docs/stealth-longrun.md).

## 8. Ops

```bash
# Logs (systemd)
sudo journalctl -u freebuff2api -f --since "10 min ago"

# Restart
sudo systemctl restart freebuff2api

# Update
cd /opt/freebuff2api && sudo -u freebuff -H git pull
sudo -u freebuff -H .venv/bin/pip install -r requirements.txt
sudo systemctl restart freebuff2api

# Backup
sudo tar czf /tmp/freebuff-backup-$(date +%F).tgz /opt/freebuff2api/data /opt/freebuff2api/.env
```
