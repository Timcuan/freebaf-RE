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

The registry includes:

| Model ID | Vendor | Notes |
|----------|--------|-------|
| `deepseek/deepseek-v4-flash` | DeepSeek | Default, fastest |
| `deepseek/deepseek-v4-pro` | DeepSeek | Smartest DeepSeek |
| `moonshotai/kimi-k2.6` | Moonshot | Kimi K2.6 |
| `minimax/minimax-m2.7` | MiniMax | |
| `minimax/minimax-m3` | MiniMax | M3 |
| `mimo/mimo-v2.5` | Xiaomi | MiMo 2.5 |
| `mimo/mimo-v2.5-pro` | Xiaomi | MiMo 2.5 Pro |
| `z-ai/glm-5.2` | Z.AI | GLM 5.2 — referral-gated weekly pool (5 sessions/referral/week, cap 10); bypassed 24/7 via Unleash session persistence |
| `glm-5.2` | Z.AI | Short alias → `z-ai/glm-5.2` |
| `google/gemini-2.5-flash-lite` | Google | File-picker agent |
| `google/gemini-3.1-flash-lite-preview` | Google | File-picker-max agent |
| `google/gemini-3.1-pro-preview` | Google | Thinker-with-files-gemini |

**Alias auto-resolve:**
- `claude-sonnet-4`, `claude-3-5-sonnet`, `claude-3-7-sonnet` → default (DeepSeek V4 Flash)
- `gpt-4o`, `gpt-4`, `gpt-5` → DeepSeek V4 Flash / Pro
- `gemini-pro`, `gemini-3.1-pro` → `google/gemini-3.1-pro-preview`

Clients (Cursor, Hermes, Claude Code, OpenAI SDK) are compatible with no extra configuration.

GLM 5.1/5.2 access is extended to 24/7 by the Freebuff Unleash pool — see README.

## 5. Health check endpoints

```bash
# Basic
curl http://127.0.0.1:8000/healthz -H "Authorization: Bearer $FREEBUFF_API_KEY"

# Egress diagnostic
curl http://127.0.0.1:8000/api/health/egress -H "Authorization: Bearer $FREEBUFF_ADMIN_KEY"

# Upstream connectivity
curl http://127.0.0.1:8000/api/health/upstream -H "Authorization: Bearer $FREEBUFF_API_KEY"

# Keep-warm (no auth, for cron ping)
curl http://127.0.0.1:8000/api/keep-warm
```

## 6. Verification checklist

- [ ] `python -m pytest -q` → 150+ tests passing
- [ ] `python main.py` starts, prints `deploy_mode=...`, egress check OK
- [ ] `/healthz` returns 200
- [ ] `/v1/models` lists models including `z-ai/glm-5.2`
- [ ] `/api/health/egress` shows `direct.is_premium=true` (or `proxy.is_premium=true`)
- [ ] `/api/health/upstream` returns `status=ok`
- [ ] `/v1/chat/completions` with `model=z-ai/glm-5.2` succeeds (non-stream + stream)
- [ ] `/admin` login with `FREEBUFF_ADMIN_KEY`

## 7. Ops

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
