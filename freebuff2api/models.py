from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreebuffModel:
    id: str
    agent_id: str
    owned_by: str = "freebuff"
    upstream_model_id: str | None = None
    session_model_id: str | None = None
    parent_agent_id: str | None = None
    # Provider routing: "codebuff" (default) or "cloudflare" (CF Workers AI free)
    provider: str = "codebuff"

    @property
    def upstream_id(self) -> str:
        return self.upstream_model_id or self.id

    @property
    def session_id(self) -> str:
        return self.session_model_id or self.upstream_id


FREEBUFF_MODELS: tuple[FreebuffModel, ...] = (
    FreebuffModel("deepseek/deepseek-v4-flash", "base2-free-deepseek-flash"),
    FreebuffModel("deepseek/deepseek-v4-pro", "base2-free-deepseek"),
    FreebuffModel("moonshotai/kimi-k2.6", "base2-free-kimi"),
    FreebuffModel("minimax/minimax-m2.7", "base2-free"),
    FreebuffModel("minimax/minimax-m3", "base2-free-minimax-m3"),
    FreebuffModel("mimo/mimo-v2.5", "base2-free-mimo"),
    FreebuffModel("mimo/mimo-v2.5-pro", "base2-free-mimo-pro"),
    # GLM 5.1 — free tier, deployment hours 9am ET-5pm PT weekdays
    # Upstream auto-routes GLM-5.1 -> GLM-5.2 per Z.AI docs (1M context, 3x peak / 2x off-peak quota)
    # Upstream agent_id: base2-free (root orchestrator allows glm-5.1)
    FreebuffModel("zai/glm-5.1", "base2-free-glm-5-1", owned_by="zai",
                  upstream_model_id="z-ai/glm-5.1"),
    FreebuffModel("zai/glm-5.2", "base2-free-glm-5-1", owned_by="zai",
                  upstream_model_id="z-ai/glm-5.1"),
    # Common alias forms clients may send
    FreebuffModel("z-ai/glm-5.1", "base2-free-glm-5-1", owned_by="zai"),
    FreebuffModel("z-ai/glm-5.2", "base2-free-glm-5-1", owned_by="zai",
                  upstream_model_id="z-ai/glm-5.1"),
    FreebuffModel("glm-5.2", "base2-free-glm-5-1", owned_by="zai",
                  upstream_model_id="z-ai/glm-5.1"),
    FreebuffModel("glm-5.1", "base2-free-glm-5-1", owned_by="zai",
                  upstream_model_id="z-ai/glm-5.1"),
)

DEFAULT_MODEL = FREEBUFF_MODELS[0]
CONTEXT_PRUNER_AGENT_ID = "context-pruner"
GEMINI_THINKER_AGENT_ID = "thinker-with-files-gemini"
GEMINI_THINKER_PARENT_AGENT_ID = "base2-free-kimi"
GEMINI_THINKER_PARENT_MODEL_ID = "moonshotai/kimi-k2.6"
GEMINI_FLASH_LITE_SESSION_MODEL_ID = DEFAULT_MODEL.id

GEMINI_FREE_MODELS: tuple[FreebuffModel, ...] = (
    FreebuffModel(
        "google/gemini-2.5-flash-lite",
        "file-picker",
        owned_by="google",
        session_model_id=GEMINI_FLASH_LITE_SESSION_MODEL_ID,
        parent_agent_id=DEFAULT_MODEL.agent_id,
    ),
    FreebuffModel(
        "google/gemini-3.1-flash-lite-preview",
        "file-picker-max",
        owned_by="google",
        session_model_id=GEMINI_FLASH_LITE_SESSION_MODEL_ID,
        parent_agent_id=DEFAULT_MODEL.agent_id,
    ),
    FreebuffModel(
        "google/gemini-3.1-pro-preview",
        GEMINI_THINKER_AGENT_ID,
        owned_by="google",
        session_model_id=GEMINI_THINKER_PARENT_MODEL_ID,
        parent_agent_id=GEMINI_THINKER_PARENT_AGENT_ID,
    ),
)

ALL_MODELS = FREEBUFF_MODELS + GEMINI_FREE_MODELS

# Cloudflare Workers AI free models
# Verified costs (CF pricing page, 2026-06-26):
# - GLM 5.2: 127k input / 400k output neurons per M tokens → NOT free-viable
#   (10k free neurons/day = ~78 input tokens only)
# - GLM 4.7-flash: 5.5k / 36.4k neurons per M → free-viable (~1.8M in / 275k out per day)
# - Llama 3.2 1b: 2.5k / 18.3k → free-viable (~4M in / 547k out per day)
# - Qwen3 30b fp8: 4.6k / 30.5k → free-viable
# - gpt-oss-20b: 18.2k / 27.3k → free-viable
# - gemma-4-26b: 9.1k / 27.3k → free-viable
# - granite-4.0-h-micro: 1.5k / 10.2k → free-viable (cheapest)
# Available when FREEBUFF_CF_ACCOUNT_IDS + FREEBUFF_CF_API_TOKENS configured.
CLOUDFLARE_FREE_MODELS: tuple[FreebuffModel, ...] = (
    # GLM 5.2 — berbayar di CF (Worker Paid $0.011/1k neurons above 10k free)
    # Free tier hanya cukup ~78 input tokens. Use only if you have Workers Paid.
    FreebuffModel(
        "cf/glm-5.2", "cf-zai-glm-5-2", owned_by="cloudflare",
        upstream_model_id="@cf/zai-org/glm-5.2", provider="cloudflare",
    ),
    FreebuffModel(
        "cf/glm-5.2-fp8", "cf-zai-glm-5-2-fp8", owned_by="cloudflare",
        upstream_model_id="@cf/zai-org/glm-5.2-fp8", provider="cloudflare",
    ),
    # GLM 4.7-flash — free-viable (5.5k/36.4k neurons per M tokens)
    FreebuffModel(
        "cf/glm-4.7-flash", "cf-zai-glm-4-7-flash", owned_by="cloudflare",
        upstream_model_id="@cf/zai-org/glm-4.7-flash", provider="cloudflare",
    ),
    # Cheaper free-viable models
    FreebuffModel(
        "cf/llama-3.2-1b", "cf-meta-llama-3-2-1b", owned_by="cloudflare",
        upstream_model_id="@cf/meta/llama-3.2-1b-instruct", provider="cloudflare",
    ),
    FreebuffModel(
        "cf/qwen3-30b", "cf-qwen3-30b-a3b-fp8", owned_by="cloudflare",
        upstream_model_id="@cf/qwen/qwen3-30b-a3b-fp8", provider="cloudflare",
    ),
    FreebuffModel(
        "cf/gpt-oss-20b", "cf-openai-gpt-oss-20b", owned_by="cloudflare",
        upstream_model_id="@cf/openai/gpt-oss-20b", provider="cloudflare",
    ),
    FreebuffModel(
        "cf/gemma-4-26b", "cf-google-gemma-4-26b", owned_by="cloudflare",
        upstream_model_id="@cf/google/gemma-4-26b-a4b-it", provider="cloudflare",
    ),
    FreebuffModel(
        "cf/granite-4.0-micro", "cf-ibm-granite-4-0-micro", owned_by="cloudflare",
        upstream_model_id="@cf/ibm-granite/granite-4.0-h-micro", provider="cloudflare",
    ),
)

# Z.ai models — free + paid (via 20M free token pool per account)
# GLM-4.7-Flash = FREE tanpa batas (input/cached/output semua gratis)
# GLM-4.5-Flash = FREE tanpa batas
# GLM-5.2 = $1.40/M in + $4.40/M out (berbayar, tapi 20M token free per new account)
ZAI_FREE_MODELS_TUPLE: tuple[FreebuffModel, ...] = (
    # FREE tanpa batas — coding agent default
    FreebuffModel(
        "zai/glm-4.7-flash", "zai-glm-4-7-flash", owned_by="zai",
        upstream_model_id="glm-4.7-flash", provider="zai",
    ),
    FreebuffModel(
        "zai/glm-4.5-flash", "zai-glm-4-5-flash", owned_by="zai",
        upstream_model_id="glm-4.5-flash", provider="zai",
    ),
)

ZAI_PAID_MODELS_TUPLE: tuple[FreebuffModel, ...] = (
    # Berbayar tapi 20M token free per new account → pool rotation
    FreebuffModel(
        "zai/glm-5.2-paid", "zai-glm-5-2-paid", owned_by="zai",
        upstream_model_id="glm-5.2", provider="zai",
    ),
    FreebuffModel(
        "zai/glm-5.1-paid", "zai-glm-5-1-paid", owned_by="zai",
        upstream_model_id="glm-5.1", provider="zai",
    ),
    FreebuffModel(
        "zai/glm-4.7-flashx", "zai-glm-4-7-flashx", owned_by="zai",
        upstream_model_id="glm-4.7-flashx", provider="zai",
    ),
)

def all_models_with_cf(cf_enabled: bool = False) -> tuple[FreebuffModel, ...]:
    """Return all models, optionally including Cloudflare + Z.ai variants."""
    base = ALL_MODELS + CLOUDFLARE_FREE_MODELS + ZAI_FREE_MODELS_TUPLE
    if cf_enabled:
        return base + ZAI_PAID_MODELS_TUPLE
    return base

# Alias map: normalized (lowercase, no slash) -> canonical model id
_MODEL_ALIASES: dict[str, str] = {}
for _m in ALL_MODELS:
    _MODEL_ALIASES[_m.id.lower()] = _m.id
    _MODEL_ALIASES[_m.id.lower().replace("/", "")] = _m.id
    _MODEL_ALIASES[_m.id.lower().replace("/", "-")] = _m.id
    _MODEL_ALIASES[_m.id.split("/")[-1].lower()] = _m.id
# Common Claude Code / Cursor aliases
_MODEL_ALIASES["claude-sonnet-4"] = DEFAULT_MODEL.id
_MODEL_ALIASES["claude-sonnet-4-6"] = DEFAULT_MODEL.id
_MODEL_ALIASES["claude-3-5-sonnet"] = DEFAULT_MODEL.id
_MODEL_ALIASES["claude-3-7-sonnet"] = DEFAULT_MODEL.id
_MODEL_ALIASES["gpt-4o"] = DEFAULT_MODEL.id
_MODEL_ALIASES["gpt-4"] = DEFAULT_MODEL.id
_MODEL_ALIASES["gpt-5"] = "deepseek/deepseek-v4-pro"
_MODEL_ALIASES["gpt-5.2"] = "deepseek/deepseek-v4-pro"
_MODEL_ALIASES["gemini-3.1-pro"] = "google/gemini-3.1-pro-preview"
_MODEL_ALIASES["gemini-pro"] = "google/gemini-3.1-pro-preview"
del _m


def resolve_model(requested: str | None, cf_enabled: bool = False) -> FreebuffModel:
    """Resolve model by ID or alias. If cf_enabled, include paid Z.ai variants."""
    # Z.ai free models always available; paid only when cf_enabled (any external provider configured)
    pool = all_models_with_cf(cf_enabled)
    # If cf_enabled is False but ZAI is configured, still include free Z.ai models
    # all_models_with_cf already includes ZAI_FREE_MODELS_TUPLE unconditionally

    if not requested:
        return DEFAULT_MODEL

    # Direct match.
    for model in pool:
        if model.id == requested:
            return model

    # Alias match (case-insensitive, slash-stripped).
    key = requested.lower()
    for normalize in (key, key.replace("/", ""), key.replace("/", "-"),
                      requested.split("/")[-1].lower()):
        if normalize in _MODEL_ALIASES:
            canonical = _MODEL_ALIASES[normalize]
            for model in pool:
                if model.id == canonical:
                    return model

    raise ValueError(f"Unsupported Freebuff model: {requested}")


def models_response(cf_enabled: bool = False) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": model.id,
                "object": "model",
                "created": 0,
                "owned_by": model.owned_by,
                "provider": model.provider,
            }
            for model in all_models_with_cf(cf_enabled)
        ],
    }


def model_response(model_id: str) -> dict[str, object] | None:
    for model in ALL_MODELS:
        if model.id == model_id:
            return {
                "id": model.id,
                "object": "model",
                "created": 0,
                "owned_by": model.owned_by,
            }
    return None


def agent_validation_payload() -> dict[str, object]:
    models_by_agent: dict[str, FreebuffModel] = {}
    spawnable_by_agent: dict[str, set[str]] = {}
    for model in ALL_MODELS:
        models_by_agent.setdefault(model.agent_id, model)
        spawnable_by_agent.setdefault(model.agent_id, set()).add(CONTEXT_PRUNER_AGENT_ID)
        if model.parent_agent_id:
            spawnable_by_agent.setdefault(model.parent_agent_id, set()).add(model.agent_id)

    definitions = [
        _agent_definition(
            agent_id=model.agent_id,
            model_id=model.upstream_id,
            display_name=f"Freebuff {model.upstream_id}",
            spawnable_agents=sorted(spawnable_by_agent.get(model.agent_id, set())),
        )
        for model in models_by_agent.values()
    ]
    definitions.append(
        _agent_definition(
            agent_id=CONTEXT_PRUNER_AGENT_ID,
            model_id=DEFAULT_MODEL.id,
            display_name="Context Pruner",
            spawnable_agents=[],
        )
    )

    return {"agentDefinitions": definitions}


def _agent_definition(
    *,
    agent_id: str,
    model_id: str,
    display_name: str,
    spawnable_agents: list[str],
) -> dict[str, object]:
    return {
        "id": agent_id,
        "publisher": "codebuff",
        "model": model_id,
        "displayName": display_name,
        "spawnerPrompt": "Freebuff OpenAI-compatible orchestrator",
        "inputSchema": {
            "prompt": {
                "type": "string",
                "description": "A coding task to complete",
            },
            "params": {"type": "object", "properties": {}, "required": []},
        },
        "outputMode": "last_message",
        "includeMessageHistory": True,
        "toolNames": ["spawn_agents"] if spawnable_agents else [],
        "spawnableAgents": spawnable_agents,
        "systemPrompt": "Act as a helpful coding assistant.",
    }
