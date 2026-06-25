# Deploy Guide — Local / VPS / Vercel

> Setelah refactor, satu codebase jalan di semua environment.
> Pilih deployment sesuai kebutuhan:

| Opsi | Best for | Cost | Streaming | Region control |
|------|----------|------|-----------|----------------|
| **Local** | Dev, agent di mesin sama | 0 | ✓ unlimited | Manual proxy |
| **VPS US** | Production agent (Cursor/Hermes) | $4-6/bln | ✓ unlimited | Native US IP |
| **VPS non-US + proxy** | Asia/EU dev dengan US proxy | $4-12/bln | ✓ unlimited | Via SOCKS5/HTTP |
| **Docker** | VPS dengan Caddy + auto-HTTPS | same | ✓ | Same as host |
| **Vercel** | Demo / low-traffic | Free | ⚠️ 300s max | Fixed `iad1` US |

## 1. Local (dev)

```bash
cp .env.example .env
# Edit .env: isi FREEBUFF_TOKEN, FREEBUFF_API_KEY, FREEBUFF_ADMIN_KEY

pip install -r requirements.txt
python main.py
# → http://127.0.0.1:8000/healthz
```

Mode auto-detected. Egress check jalan di startup, warning kalau non-US.

## 2. VPS US (recommended untuk agent)

### 2a. Direct (Tanpa Docker)

```bash
# Di VPS (Ubuntu 22.04+):
sudo useradd -r -s /bin/false -d /opt/freebuff2api freebuff
sudo mkdir -p /opt/freebuff2api/data
sudo chown freebuff:freebuff /opt/freebuff2api

# Clone + install
sudo -u freebuff -H git clone https://github.com/Timcuan/freebuff2api-vercel.git /opt/freebuff2api
cd /opt/freebuff2api
sudo -u freebuff -H python3 -m venv .venv
sudo -u freebuff -H .venv/bin/pip install -r requirements.txt

# Config
sudo -u freebuff -H cp .env.example .env
sudo -u freebuff -H nano .env  # isi token, api key, admin key

# Systemd service
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
# Edit domain in Caddyfile
sudo systemctl reload caddy
# → https://api.example.com/healthz
```

Caddy auto-issue Let's Encrypt cert, stream-friendly (SSE flush -1), security headers.

## 3. VPS non-US + egress proxy

Kalau VPS Anda di Asia/EU/non-premium region, set:

```dotenv
FREEBUFF_EGRESS_AUTO=true
FREEBUFF_EGRESS_PROXY_URL=socks5://user:pass@us-proxy.example.com:1080
# atau
FREEBUFF_EGRESS_PROXY_URL=socks5h://us-proxy.example.com:1080  # DNS via proxy
```

Verifikasi egress:

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

Sumber proxy US:
- **Commercial:** Smartproxy, Bright Data, Oxylabs (~$50-100/bln untuk 5-20GB)
- **Self-host:** VPS US kecil ($4) + `ssh -D 1080` (SOCKS5) atau `gost`/`3proxy`
- **Free (unreliable):** tor (`socks5://127.0.0.1:9050`, exit node US tapi lambat + diblok banyak CDN)

## 4. Vercel

```bash
# Push ke GitHub → import di Vercel → set env vars
# vercel.json sudah set:
#   regions=["iad1"]  (US East, required for Codebuff free-tier)
#   functions.maxDuration=300  (streaming chat up to 5 menit)
```

**Limit Vercel:**
- Cold start 250-1000ms per cold instance
- Account pool broken (each instance = separate process, no shared lock)
  → use single FREEBUFF_TOKEN, avoid concurrent requests
- 100GB-hr/bln free tier (~3000 invocations @ 30s avg)
- No persistent storage (usage_store reset per cold start)

## 5. Model tersedia

Setelah refactor, registry mencakup:

| Model ID | Vendor | Notes |
|----------|--------|-------|
| `deepseek/deepseek-v4-flash` | DeepSeek | Default, fastest |
| `deepseek/deepseek-v4-pro` | DeepSeek | Smartest DeepSeek |
| `moonshotai/kimi-k2.6` | Moonshot | Kimi K2.6 |
| `minimax/minimax-m2.7` | MiniMax | |
| `minimax/minimax-m3` | MiniMax | M3 |
| `mimo/mimo-v2.5` | Xiaomi | MiMo 2.5 |
| `mimo/mimo-v2.5-pro` | Xiaomi | MiMo 2.5 Pro |
| `zai/glm-5.1` | Z.AI | **Free tier** (9am ET-5pm PT weekdays) |
| `zai/glm-5.2` | Z.AI | Alias → upstream auto-routes to GLM-5.1 → GLM-5.2 per Z.AI docs |
| `z-ai/glm-5.1` | Z.AI | Alias (Codebuff CLI format) |
| `z-ai/glm-5.2` | Z.AI | Alias |
| `glm-5.1` / `glm-5.2` | Z.AI | Short alias |
| `google/gemini-2.5-flash-lite` | Google | File-picker agent |
| `google/gemini-3.1-flash-lite-preview` | Google | File-picker-max agent |
| `google/gemini-3.1-pro-preview` | Google | Thinker-with-files-gemini |

**Alias support (auto-resolve):**
- `claude-sonnet-4`, `claude-3-5-sonnet`, `claude-3-7-sonnet` → default (DeepSeek V4 Flash)
- `gpt-4o`, `gpt-4`, `gpt-5` → DeepSeek V4 Flash / Pro
- `gemini-pro`, `gemini-3.1-pro` → `google/gemini-3.1-pro-preview`

Client (Cursor, Hermes, Claude Code, OpenAI SDK) langsung compatible tanpa konfigurasi tambahan.

## 6. Health check endpoints

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

## 7. Verification checklist

- [ ] `python -m pytest -q` → 120+ tests passing
- [ ] `python main.py` starts, prints `deploy_mode=...`, egress check OK
- [ ] `/healthz` returns 200
- [ ] `/v1/models` lists 16 models including `zai/glm-5.2`
- [ ] `/api/health/egress` shows `direct.is_premium=true` (atau proxy.is_premium=true)
- [ ] `/api/health/upstream` returns `status=ok`
- [ ] `/v1/chat/completions` dengan `model=zai/glm-5.2` berhasil (non-stream + stream)
- [ ] `/admin` login dengan `FREEBUFF_ADMIN_KEY`

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
