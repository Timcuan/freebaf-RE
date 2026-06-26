# Cursor IDE — freebaf-RE integration

## Setup

1. Start the gateway:
   ```bash
   cd freebaf-RE
   cp .env.example .env
   # edit .env: set FREEBUFF_TOKEN (from freebuff.071129.xyz) and FREEBUFF_API_KEY
   # set FREEBUFF_SYSTEM_PROMPT_OVERRIDE=  (empty — pure passthrough for Cursor)
   python main.py
   ```

2. In Cursor: Settings → Models
   - OpenAI API Key: `YOUR_FREEBUFF_API_KEY`
   - Base URL: `http://localhost:8000/v1`
   - Override model: `z-ai/glm-5.2` (or any from `/v1/models`)

3. Verify:
   ```bash
   curl http://localhost:8000/v1/models -H "Authorization: Bearer YOUR_FREEBUFF_API_KEY"
   ```

## Available models

| Model ID | Best for |
|---|---|
| `z-ai/glm-5.2` | Smartest, coding, reasoning (referral-gated, cached 24/7 via Unleash) |
| `deepseek/deepseek-v4-pro` | Coding, general |
| `moonshotai/kimi-k2.6` | Long context (256k) |
| `minimax/minimax-m2.7` | Fast, cheap |
| `google/gemini-3.1-pro-preview` | Multimodal, files |

## Tool calling

Cursor's tool calling (apply edits, run commands) works via the OpenAI
`tools` field. The gateway passes tools through to upstream — no extra
config needed.

## Streaming

Streaming is supported (`stream: true`). Cursor uses streaming by default
for responsive UI.

## Troubleshooting

- **401 Invalid API key**: check `Authorization: Bearer <FREEBUFF_API_KEY>` matches `.env`
- **503 Set FREEBUFF_TOKEN**: no upstream token configured — add to `.env` or admin panel
- **409 session_model_mismatch**: egress IP not in US/CA — set `FREEBUFF_EGRESS_PROXY_URL` to a residential proxy
- **403 country_blocked**: same as above (proxy required)
- **empty content**: check `/admin` → Logs tab for upstream errors
