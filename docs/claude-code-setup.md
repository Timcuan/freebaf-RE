# Claude Code / Anthropic SDK — freebaf-RE integration

The gateway supports the Anthropic Messages API (`/v1/messages`) with
bidirectional format conversion. Use it as a drop-in replacement for
the Anthropic API.

## Setup

1. Start the gateway (same as Cursor setup — see [`cursor-setup.md`](cursor-setup.md)).
2. Set `FREEBUFF_SYSTEM_PROMPT_OVERRIDE=` (empty) in `.env` for pure passthrough.
3. For 24/7 VPS deploy with multiple accounts, see [`stealth-longrun.md`](stealth-longrun.md).

## Claude Code CLI

```bash
export ANTHROPIC_BASE_URL=http://localhost:8000
export ANTHROPIC_API_KEY=YOUR_FREEBUFF_API_KEY
claude  # launches Claude Code routing through freebaf-RE
```

## Anthropic SDK (Python)

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8000",
    api_key="YOUR_FREEBUFF_API_KEY",
)

response = client.messages.create(
    model="minimax/minimax-m3",  # default; or z-ai/glm-5.2, deepseek/deepseek-v4-pro, etc.
    max_tokens=1024,
    messages=[{"role": "user", "content": "Write a Python fizzbuzz."}],
)
print(response.content[0].text)
```

## Streaming

```python
with client.messages.stream(
    model="z-ai/glm-5.2",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Stream a story."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

## Tool calling (Anthropic format)

```python
response = client.messages.create(
    model="z-ai/glm-5.2",
    max_tokens=1024,
    tools=[{
        "name": "read_file",
        "description": "Read a file from disk",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }],
    messages=[{"role": "user", "content": "Read /etc/hosts"}],
)
# response.content contains tool_use blocks
```

The gateway converts Anthropic `input_schema` → OpenAI `function.parameters`
on the way upstream, and converts OpenAI `tool_calls` → Anthropic
`tool_use` blocks on the way back.

## Rate governor

`/v1/messages` uses the same rate governor as `/v1/chat/completions` when
multiple accounts are configured — important for long Claude Code sessions on a VPS.

## Supported Anthropic features

- `messages` (system, user, assistant, tool_result)
- `tools` + `tool_choice`
- `max_tokens`, `temperature`, `top_p`, `top_k`
- `stop_sequences`
- `stream: true` (SSE with `message_start`, `content_block_delta`, etc.)
- `system` top-level field

## Not supported (upstream limitation)

- `vision` (image content blocks) — upstream Codebuff models don't accept images
- `cache_control` ephemeral — passed through but upstream ignores
- Computer use, bash use — not upstream models
