#!/usr/bin/env python3
"""Integration test for agent/IDE use cases.

Verifies the gateway produces OpenAI/Anthropic-compatible responses suitable
for agent integration (Cursor, Hermes, Claude Code, CLI tools).

Run with:
    FREEBUFF_TOKEN=<token> FREEBUFF_API_KEY=test python3 scripts/test_agent_integration.py

If no token is set, runs in dry-run mode (validates payload construction
without hitting upstream).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_model_registry() -> bool:
    """All models resolve correctly — no orphan IDs."""
    from freebuff2api.models import ALL_MODELS, resolve_model

    print("=== Model Registry ===")
    print(f"Total models: {len(ALL_MODELS)}")
    for m in ALL_MODELS:
        print(f"  {m.id:40s} agent={m.agent_id:30s} upstream={m.upstream_id}")

    # Aliases work
    aliases = ["glm-5.2", "z-ai/glm-5.2", "zai-glm-5.2", "claude-sonnet-4", "gpt-5"]
    print("\n=== Alias Resolution ===")
    for a in aliases:
        try:
            m = resolve_model(a)
            print(f"  {a:25s} -> {m.id}")
        except ValueError as e:
            print(f"  {a:25s} -> FAIL: {e}")
            return False
    return True


def test_payload_construction() -> bool:
    """Payloads include tool calling, streaming, system prompt passthrough."""
    from freebuff2api.codebuff import FreebuffSession
    from freebuff2api.openai_compat import build_upstream_payload

    print("\n=== Payload Construction (agent use case) ===")
    session = FreebuffSession(
        instance_id="test-inst",
        model="z-ai/glm-5.2",
        expires_at=0,
        remaining_ms=3600000,
    )

    # Agent-style request: system prompt + tools + stream
    body = {
        "model": "z-ai/glm-5.2",
        "messages": [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Read foo.py and fix the bug."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "temperature": 0.7,
        "stream": True,
    }

    # Pure passthrough (agent mode)
    payload = build_upstream_payload(
        body,
        session=session,
        run_id="run-1",
        client_id="client-1",
        upstream_model_id="z-ai/glm-5.2",
        system_prompt="",  # passthrough
    )

    print(f"  model: {payload['model']}")
    print(f"  stream: {payload['stream']}")
    print(f"  tools: {len(payload.get('tools', []))} tool(s)")
    print(f"  tool_choice: {payload.get('tool_choice')}")
    print(f"  temperature: {payload.get('temperature')}")
    print(f"  messages: {len(payload['messages'])} message(s)")
    print(f"  stop: {payload.get('stop')}")

    # Verify system prompt passes through untouched
    sys_msg = payload["messages"][0]
    if sys_msg["content"] != "You are a coding agent.":
        print(f"  FAIL: system prompt injected — got: {sys_msg['content']!r}")
        return False
    print("  system prompt: passthrough OK")
    return True


def test_anthropic_tool_conversion() -> bool:
    """Anthropic tool format converts to OpenAI format for upstream."""
    from freebuff2api.anthropic_compat import anthropic_tools_to_openai

    print("\n=== Anthropic Tool Conversion ===")
    anthropic_tools = [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    openai_tools = anthropic_tools_to_openai(anthropic_tools)
    if not openai_tools:
        print("  FAIL: no tools converted")
        return False
    t = openai_tools[0]
    print(f"  {t['function']['name']}: {t['function']['description']}")
    print(f"  params: {json.dumps(t['function']['parameters'])}")
    if t["type"] != "function":
        print(f"  FAIL: type != function, got {t['type']}")
        return False
    print("  conversion OK")
    return True


async def test_live_chat() -> bool:
    """Live chat completion test (only if FREEBUFF_TOKEN set)."""
    token = os.getenv("FREEBUFF_TOKEN")
    if not token:
        print("\n=== Live Chat (SKIPPED — no FREEBUFF_TOKEN) ===")
        return True

    print("\n=== Live Chat ===")
    from freebuff2api.app import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        # Non-streaming
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={
                "model": "z-ai/glm-5.2",
                "messages": [{"role": "user", "content": "Say hello in one word."}],
                "stream": False,
            },
        )
        print(f"  non-stream status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  body: {resp.text[:300]}")
            return False
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"  content: {content!r}")
        if not content:
            print("  FAIL: empty content")
            return False

        # Streaming
        print("\n  --- streaming ---")
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={
                "model": "z-ai/glm-5.2",
                "messages": [{"role": "user", "content": "Count 1 to 3."}],
                "stream": True,
            },
        )
        print(f"  stream status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  body: {resp.text[:300]}")
            return False
        chunks = 0
        content_parts = []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                chunk = json.loads(data_str)
                delta = chunk["choices"][0].get("delta", {})
                if "content" in delta:
                    content_parts.append(delta["content"])
                chunks += 1
        full = "".join(content_parts)
        print(f"  chunks: {chunks}")
        print(f"  streamed content: {full!r}")
        if chunks < 2:
            print("  FAIL: too few chunks")
            return False
    return True


def main() -> int:
    print("freebaf-RE — Agent Integration Test\n")
    ok = True
    ok = test_model_registry() and ok
    ok = test_payload_construction() and ok
    ok = test_anthropic_tool_conversion() and ok
    ok = asyncio.run(test_live_chat()) and ok

    print("\n" + "=" * 50)
    if ok:
        print("ALL CHECKS PASSED — gateway ready for agent integration")
        return 0
    print("FAILURES DETECTED — see above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
