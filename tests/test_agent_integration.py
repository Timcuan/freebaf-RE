"""Tests for agent integration: model aliases, tool calling, payload construction."""
from __future__ import annotations

import unittest

from freebuff2api.anthropic_compat import anthropic_tools_to_openai
from freebuff2api.codebuff import FreebuffSession
from freebuff2api.models import ALL_MODELS, resolve_model
from freebuff2api.openai_compat import build_upstream_payload


class ModelAliasTests(unittest.TestCase):
    """All common client model name variations resolve correctly."""

    def test_glm52_alias_variations(self) -> None:
        aliases = [
            "z-ai/glm-5.2",
            "z-ai-glm-5.2",
            "zaiglm-5.2",
            "zai/glm-5.2",
            "zai-glm-5.2",
            "glm-5.2",
            "glm5.2",
            "GLM-5.2",
            "Z-AI/GLM-5.2",
        ]
        for alias in aliases:
            with self.subTest(alias=alias):
                m = resolve_model(alias)
                self.assertEqual(m.upstream_id, "z-ai/glm-5.2")

    def test_glm51_falls_back_to_52(self) -> None:
        """GLM 5.1 alias should map to 5.2 (upstream only has 5.2 now)."""
        for alias in ("glm-5.1", "z-ai/glm-5.1", "zai/glm-5.1"):
            with self.subTest(alias=alias):
                m = resolve_model(alias)
                self.assertEqual(m.upstream_id, "z-ai/glm-5.2")

    def test_cursor_claude_aliases(self) -> None:
        """Cursor/Claude Code aliases map to a working model."""
        for alias in ("claude-sonnet-4", "claude-sonnet-4-6", "claude-3-5-sonnet", "gpt-4o", "gpt-4"):
            with self.subTest(alias=alias):
                m = resolve_model(alias)
                # Should resolve to a valid model (not raise)
                self.assertIsNotNone(m.id)

    def test_gpt5_alias_maps_to_pro(self) -> None:
        m = resolve_model("gpt-5")
        self.assertEqual(m.id, "deepseek/deepseek-v4-pro")
        m = resolve_model("gpt-5.2")
        self.assertEqual(m.id, "deepseek/deepseek-v4-pro")

    def test_default_model_when_none_requested(self) -> None:
        m = resolve_model(None)
        self.assertEqual(m.id, "deepseek/deepseek-v4-flash")

    def test_unsupported_model_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_model("nonexistent/totally-fake-model")


class ToolCallingPayloadTests(unittest.TestCase):
    """Tool calling payloads are constructed correctly for agent use cases."""

    def _session(self) -> FreebuffSession:
        return FreebuffSession(
            instance_id="inst-1",
            model="z-ai/glm-5.2",
            expires_at=0,
            remaining_ms=3600000,
        )

    def test_tools_included_in_payload(self) -> None:
        body = {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt="",
        )
        self.assertEqual(len(payload["tools"]), 1)
        self.assertEqual(payload["tools"][0]["function"]["name"], "read_file")

    def test_tool_choice_included(self) -> None:
        body = {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        }
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt="",
        )
        self.assertEqual(payload["tool_choice"], "auto")

    def test_temperature_and_top_p_included(self) -> None:
        body = {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
        }
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt="",
        )
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["top_p"], 0.9)

    def test_stream_always_true_upstream(self) -> None:
        """Upstream always streams; gateway buffers for non-stream clients."""
        body = {"model": "z-ai/glm-5.2", "messages": [], "stream": False}
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt="",
        )
        # Internal payload always stream=True (gateway reassembles for non-stream)
        self.assertTrue(payload["stream"])

    def test_system_prompt_passthrough_empty(self) -> None:
        """Empty system_prompt → no Buffy prefix injected."""
        body = {
            "model": "z-ai/glm-5.2",
            "messages": [
                {"role": "system", "content": "You are a coding agent."},
                {"role": "user", "content": "Fix the bug."},
            ],
        }
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt="",
        )
        sys_msg = payload["messages"][0]
        self.assertEqual(sys_msg["content"], "You are a coding agent.")

    def test_system_prompt_passthrough_none_uses_buffy(self) -> None:
        """None system_prompt → default Buffy neutralizer (upstream requirement)."""
        body = {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt=None,
        )
        # A system message is injected with Buffy prefix
        sys_msgs = [m for m in payload["messages"] if m.get("role") == "system"]
        self.assertTrue(sys_msgs)
        self.assertIn("Buffy", sys_msgs[0]["content"])

    def test_metadata_included(self) -> None:
        body = {"model": "z-ai/glm-5.2", "messages": []}
        payload = build_upstream_payload(
            body, session=self._session(), run_id="r1", client_id="c1",
            upstream_model_id="z-ai/glm-5.2", system_prompt="",
        )
        meta = payload["codebuff_metadata"]
        self.assertEqual(meta["freebuff_instance_id"], "inst-1")
        self.assertEqual(meta["run_id"], "r1")
        self.assertEqual(meta["client_id"], "c1")
        self.assertEqual(meta["cost_mode"], "free")


class AnthropicToolConversionTests(unittest.TestCase):
    """Anthropic tool format converts to OpenAI format for upstream."""

    def test_basic_conversion(self) -> None:
        tools = [
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
        result = anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["function"]["name"], "read_file")
        self.assertEqual(result[0]["function"]["parameters"]["required"], ["path"])

    def test_empty_tools(self) -> None:
        self.assertIsNone(anthropic_tools_to_openai([]))
        self.assertIsNone(anthropic_tools_to_openai(None))

    def test_multiple_tools(self) -> None:
        tools = [
            {"name": f"tool_{i}", "description": f"Tool {i}", "input_schema": {"type": "object"}}
            for i in range(3)
        ]
        result = anthropic_tools_to_openai(tools)
        self.assertEqual(len(result), 3)


class ModelsResponseShapeTests(unittest.TestCase):
    """/v1/models returns OpenAI-compatible shape."""

    def test_models_response_openai_shape(self) -> None:
        from freebuff2api.models import models_response

        resp = models_response()
        self.assertEqual(resp["object"], "list")
        self.assertIsInstance(resp["data"], list)
        self.assertEqual(len(resp["data"]), len(ALL_MODELS))
        for item in resp["data"]:
            self.assertEqual(item["object"], "model")
            self.assertIn("id", item)
            self.assertIn("owned_by", item)

    def test_all_models_have_agent_id(self) -> None:
        """Every model must have an agent_id for upstream spawn."""
        for m in ALL_MODELS:
            with self.subTest(model=m.id):
                self.assertTrue(m.agent_id, f"missing agent_id for {m.id}")


if __name__ == "__main__":
    unittest.main()
