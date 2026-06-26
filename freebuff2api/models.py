from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreebuffModel:
    """Freebuff model definition matching upstream Codebuff spec.

    Verified against `common/src/constants/freebuff-models.ts` (June 2026)
    and `common/src/constants/model-config.ts`.
    """

    id: str
    agent_id: str
    owned_by: str = "freebuff"
    upstream_model_id: str | None = None
    session_model_id: str | None = None
    parent_agent_id: str | None = None
    # Spec fields (mirror upstream FreebuffModelOption)
    display_name: str = ""
    tagline: str = ""
    tier: int = 0  # 0=unlimited, 1=premium daily, 2=glm weekly referral, 3=thinker child
    premium: bool = False
    multimodal: bool = False
    data_collection: bool = False  # upstream "Collects data for training" warning
    context_window: int = 128_000
    can_spawn_gemini_thinker: bool = False

    @property
    def upstream_id(self) -> str:
        return self.upstream_model_id or self.id

    @property
    def session_id(self) -> str:
        return self.session_model_id or self.upstream_id

    @property
    def pool(self) -> str:
        """Upstream session pool identifier."""
        if self.tier == 2:
            return "pacific_week"
        if self.tier == 1:
            return "pacific_day"
        return ""


# ── Tier 0: Unlimited (no quota gate, always available) ─────────────
# DeepSeek V4 Flash — "Smart & Fast", premium: false, multimodal: false
# Data collection warning (DeepSeek direct API trains on data).
_FREE_DEEPSEEK_FLASH = FreebuffModel(
    id="deepseek/deepseek-v4-flash",
    agent_id="base2-free-deepseek-flash",
    owned_by="deepseek",
    display_name="DeepSeek V4 Flash",
    tagline="Smart & Fast",
    tier=0,
    premium=False,
    multimodal=False,
    data_collection=True,
    context_window=128_000,
)

# MiMo 2.5 (Xiaomi) — "Multimodal", premium: false, multimodal: true
_FREE_MIMO_V25 = FreebuffModel(
    id="mimo/mimo-v2.5",
    agent_id="base2-free-mimo",
    owned_by="mimo",
    display_name="MiMo 2.5",
    tagline="Multimodal",
    tier=0,
    premium=False,
    multimodal=True,
    data_collection=False,
    context_window=128_000,
)

# MiniMax M2.7 (legacy) — "Fastest", premium: false, multimodal: false
# Removed from picker 2026-06-09 but still server-supported.
_FREE_MINIMAX_M27 = FreebuffModel(
    id="minimax/minimax-m2.7",
    agent_id="base2-free",
    owned_by="minimax",
    display_name="MiniMax M2.7",
    tagline="Fastest (legacy)",
    tier=0,
    premium=False,
    multimodal=False,
    data_collection=False,
    context_window=128_000,
)

# MiniMax M3 — "Smartest & Fastest", premium: false, multimodal: true
# Served by Fireworks (no data collection). Newest MiniMax.
_FREE_MINIMAX_M3 = FreebuffModel(
    id="minimax/minimax-m3",
    agent_id="base2-free-minimax-m3",
    owned_by="minimax",
    display_name="MiniMax M3",
    tagline="Smartest & Fastest",
    tier=0,
    premium=False,
    multimodal=True,
    data_collection=False,
    context_window=1_000_000,  # MiniMax M3: 1M context (SWE-Bench leader)
    can_spawn_gemini_thinker=True,
)

# ── Tier 1: Premium daily pool (pacific_day, 5 sessions/day, reset midnight PT) ─
# DeepSeek V4 Pro — "Smartest", premium: true, multimodal: false
# Data collection warning. 1T MoE, MIT license, 1M context.
_PREMIUM_DEEPSEEK_PRO = FreebuffModel(
    id="deepseek/deepseek-v4-pro",
    agent_id="base2-free-deepseek",
    owned_by="deepseek",
    display_name="DeepSeek V4 Pro",
    tagline="Smartest",
    tier=1,
    premium=True,
    multimodal=False,
    data_collection=True,
    context_window=1_000_000,
    can_spawn_gemini_thinker=True,
)

# MiMo 2.5 Pro (Xiaomi) — "Smartest & Slow", premium: true, multimodal: true
_PREMIUM_MIMO_PRO = FreebuffModel(
    id="mimo/mimo-v2.5-pro",
    agent_id="base2-free-mimo-pro",
    owned_by="mimo",
    display_name="MiMo 2.5 Pro",
    tagline="Smartest & Slow",
    tier=1,
    premium=True,
    multimodal=True,
    data_collection=False,
    context_window=128_000,
    can_spawn_gemini_thinker=True,
)

# Kimi K2.6 (Moonshot) — "Balanced", premium: true, multimodal: true
# 1T parameters, agent swarms, 256k context.
_PREMIUM_KIMI = FreebuffModel(
    id="moonshotai/kimi-k2.6",
    agent_id="base2-free-kimi",
    owned_by="moonshot",
    display_name="Kimi K2.6",
    tagline="Balanced",
    tier=1,
    premium=True,
    multimodal=True,
    data_collection=False,
    context_window=256_000,
    can_spawn_gemini_thinker=True,
)

# ── Tier 2: GLM weekly referral pool (pacific_week, 5/referral/week, cap 10) ──
# GLM 5.2 (Z.ai) — referral-gated, premium: true (badge), multimodal: false
# Served by Fireworks (no data collection). Availability 'always' but gated
# by referral session pool. 1h sessions exact.
_GLM_V52 = FreebuffModel(
    id="z-ai/glm-5.2",
    agent_id="base2-free-glm",
    owned_by="zai",
    display_name="GLM 5.2",
    tagline="Unlock by referring friends",
    tier=2,
    premium=True,
    multimodal=False,
    data_collection=False,
    context_window=128_000,
    can_spawn_gemini_thinker=False,
)

# Short alias clients may send
_GLM_V52_ALIAS = FreebuffModel(
    id="glm-5.2",
    agent_id="base2-free-glm",
    owned_by="zai",
    upstream_model_id="z-ai/glm-5.2",
    display_name="GLM 5.2",
    tagline="Unlock by referring friends",
    tier=2,
    premium=True,
    multimodal=False,
    data_collection=False,
    context_window=128_000,
)

# ── Tier 3: Gemini thinker (spawned by Tier 1 parents) ─────────────
# Gemini 3.1 Pro Preview — thinker subagent for deeper reasoning.
# Parent models (FREEBUFF_GEMINI_THINKER_PARENT_MODELS):
#   Kimi K2.6, DeepSeek V4 Pro, MiMo 2.5 Pro, MiniMax M3
CONTEXT_PRUNER_AGENT_ID = "context-pruner"
GEMINI_THINKER_AGENT_ID = "thinker-with-files-gemini"
GEMINI_FLASH_LITE_SESSION_MODEL_ID = _FREE_DEEPSEEK_FLASH.id

_GEMINI_PRO_THINKER = FreebuffModel(
    id="google/gemini-3.1-pro-preview",
    agent_id=GEMINI_THINKER_AGENT_ID,
    owned_by="google",
    session_model_id="moonshotai/kimi-k2.6",  # default parent session
    parent_agent_id="base2-free-kimi",  # default parent agent
    display_name="Gemini 3.1 Pro (Thinker)",
    tagline="Deeper reasoning subagent",
    tier=3,
    premium=False,  # quota comes from parent
    multimodal=True,
    data_collection=False,
    context_window=1_000_000,
)

# File-picker agents (Tier 0, Gemini Flash Lite) — used for file finding
_GEMINI_FLASH_LITE = FreebuffModel(
    id="google/gemini-2.5-flash-lite",
    agent_id="file-picker",
    owned_by="google",
    session_model_id=GEMINI_FLASH_LITE_SESSION_MODEL_ID,
    parent_agent_id=_FREE_DEEPSEEK_FLASH.agent_id,
    display_name="Gemini 2.5 Flash Lite",
    tagline="File picker",
    tier=0,
    premium=False,
    multimodal=True,
    data_collection=False,
    context_window=1_000_000,
)

_GEMINI_FLASH_LITE_PREVIEW = FreebuffModel(
    id="google/gemini-3.1-flash-lite-preview",
    agent_id="file-picker-max",
    owned_by="google",
    session_model_id=GEMINI_FLASH_LITE_SESSION_MODEL_ID,
    parent_agent_id=_FREE_DEEPSEEK_FLASH.agent_id,
    display_name="Gemini 3.1 Flash Lite Preview",
    tagline="File picker max",
    tier=0,
    premium=False,
    multimodal=True,
    data_collection=False,
    context_window=1_000_000,
)

# Order = priority (newest first per provider, tier-sorted).
FREEBUFF_MODELS: tuple[FreebuffModel, ...] = (
    # Tier 2: GLM 5.2 (newest, referral-gated, smartest)
    _GLM_V52,
    _GLM_V52_ALIAS,
    # Tier 1: Premium daily (newest per provider)
    _PREMIUM_DEEPSEEK_PRO,      # DeepSeek V4 Pro
    _PREMIUM_KIMI,              # Kimi K2.6
    _PREMIUM_MIMO_PRO,          # MiMo 2.5 Pro
    # Tier 0: Unlimited (newest first)
    _FREE_MINIMAX_M3,           # MiniMax M3 (newest, smartest & fastest)
    _FREE_MIMO_V25,             # MiMo 2.5
    _FREE_DEEPSEEK_FLASH,       # DeepSeek V4 Flash
    _FREE_MINIMAX_M27,          # MiniMax M2.7 (legacy)
    # Tier 3: Thinker + file pickers
    _GEMINI_PRO_THINKER,
    _GEMINI_FLASH_LITE_PREVIEW, # newer
    _GEMINI_FLASH_LITE,
)

DEFAULT_MODEL = _FREE_MINIMAX_M3  # upstream default = MiniMax M3 (smartest & fastest, no quota)

GEMINI_THINKER_PARENT_AGENT_ID = _PREMIUM_KIMI.agent_id
GEMINI_THINKER_PARENT_MODEL_ID = _PREMIUM_KIMI.id

GEMINI_FREE_MODELS: tuple[FreebuffModel, ...] = (
    _GEMINI_FLASH_LITE,
    _GEMINI_FLASH_LITE_PREVIEW,
    _GEMINI_PRO_THINKER,
)

ALL_MODELS = FREEBUFF_MODELS

# ── Aliases ──────────────────────────────────────────────────────────
_MODEL_ALIASES: dict[str, str] = {}
for _m in ALL_MODELS:
    _MODEL_ALIASES[_m.id.lower()] = _m.id
    _MODEL_ALIASES[_m.id.lower().replace("/", "")] = _m.id
    _MODEL_ALIASES[_m.id.lower().replace("/", "-")] = _m.id
    _MODEL_ALIASES[_m.id.split("/")[-1].lower()] = _m.id

# Common Claude Code / Cursor aliases → MiniMax M3 (default, smart+fast, unlimited)
_MODEL_ALIASES["claude-sonnet-4"] = DEFAULT_MODEL.id
_MODEL_ALIASES["claude-sonnet-4-6"] = DEFAULT_MODEL.id
_MODEL_ALIASES["claude-3-5-sonnet"] = DEFAULT_MODEL.id
_MODEL_ALIASES["claude-3-7-sonnet"] = DEFAULT_MODEL.id
_MODEL_ALIASES["gpt-4o"] = DEFAULT_MODEL.id
_MODEL_ALIASES["gpt-4"] = DEFAULT_MODEL.id
_MODEL_ALIASES["gpt-5"] = _PREMIUM_DEEPSEEK_PRO.id
_MODEL_ALIASES["gpt-5.2"] = _PREMIUM_DEEPSEEK_PRO.id
_MODEL_ALIASES["gemini-3.1-pro"] = _GEMINI_PRO_THINKER.id
_MODEL_ALIASES["gemini-pro"] = _GEMINI_PRO_THINKER.id

# Extra aliases for common client variations
_EXTRA_ALIASES = {
    "zai/glm-5.2": "z-ai/glm-5.2",
    "zai-glm-5.2": "z-ai/glm-5.2",
    "zaiglm-5.2": "z-ai/glm-5.2",
    "glm5.2": "glm-5.2",
    "z-ai-glm-5.2": "z-ai/glm-5.2",
    "zai/glm-5.1": "z-ai/glm-5.2",
    "z-ai/glm-5.1": "z-ai/glm-5.2",
    "glm-5.1": "glm-5.2",
    # MiniMax aliases
    "minimax-m3": "minimax/minimax-m3",
    "minimaxm3": "minimax/minimax-m3",
    "minimax-m2.7": "minimax/minimax-m2.7",
    "minimaxm2.7": "minimax/minimax-m2.7",
    # MiMo aliases
    "mimo-v2.5": "mimo/mimo-v2.5",
    "mimov2.5": "mimo/mimo-v2.5",
    "mimo-v2.5-pro": "mimo/mimo-v2.5-pro",
    "mimov2.5pro": "mimo/mimo-v2.5-pro",
    # DeepSeek aliases
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseekv4pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseekv4flash": "deepseek/deepseek-v4-flash",
    # Kimi aliases
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "kimik2.6": "moonshotai/kimi-k2.6",
    "kimi": "moonshotai/kimi-k2.6",
}
for _k, _v in _EXTRA_ALIASES.items():
    _MODEL_ALIASES[_k.lower()] = _v
del _m


def resolve_model(requested: str | None) -> FreebuffModel:
    """Resolve model by ID or alias."""
    if not requested:
        return DEFAULT_MODEL

    # Direct match.
    for model in ALL_MODELS:
        if model.id == requested:
            return model

    # Alias match (case-insensitive, slash-stripped).
    key = requested.lower()
    for normalize in (key, key.replace("/", ""), key.replace("/", "-"),
                      requested.split("/")[-1].lower()):
        if normalize in _MODEL_ALIASES:
            canonical = _MODEL_ALIASES[normalize]
            for model in ALL_MODELS:
                if model.id == canonical:
                    return model

    raise ValueError(f"Unsupported Freebuff model: {requested}")


def models_response() -> dict[str, object]:
    """OpenAI-compatible /v1/models response with tier + spec metadata."""
    return {
        "object": "list",
        "data": [
            {
                "id": model.id,
                "object": "model",
                "created": 0,
                "owned_by": model.owned_by,
                # Extended metadata for client selection
                "display_name": model.display_name,
                "tagline": model.tagline,
                "tier": model.tier,
                "tier_name": _tier_name(model.tier),
                "premium": model.premium,
                "multimodal": model.multimodal,
                "data_collection": model.data_collection,
                "context_window": model.context_window,
                "can_spawn_gemini_thinker": model.can_spawn_gemini_thinker,
            }
            for model in ALL_MODELS
        ],
    }


def _tier_name(tier: int) -> str:
    return {0: "unlimited", 1: "premium_daily", 2: "glm_weekly", 3: "thinker"}.get(tier, "unknown")


def model_response(model_id: str) -> dict[str, object] | None:
    for model in ALL_MODELS:
        if model.id == model_id:
            return {
                "id": model.id,
                "object": "model",
                "created": 0,
                "owned_by": model.owned_by,
                "display_name": model.display_name,
                "tagline": model.tagline,
                "tier": model.tier,
                "tier_name": _tier_name(model.tier),
                "premium": model.premium,
                "multimodal": model.multimodal,
                "data_collection": model.data_collection,
                "context_window": model.context_window,
                "can_spawn_gemini_thinker": model.can_spawn_gemini_thinker,
            }
    return None


def models_by_tier(tier: int) -> tuple[FreebuffModel, ...]:
    """Return all models at a given tier (sorted by priority)."""
    return tuple(m for m in ALL_MODELS if m.tier == tier)


def latest_model_per_provider() -> dict[str, FreebuffModel]:
    """Return the newest model from each provider (for default selection)."""
    by_provider: dict[str, FreebuffModel] = {}
    for model in ALL_MODELS:
        provider = model.owned_by
        existing = by_provider.get(provider)
        if existing is None:
            by_provider[provider] = model
            continue
        if model.tier < existing.tier:
            by_provider[provider] = model
        elif model.tier == existing.tier and model.context_window > existing.context_window:
            by_provider[provider] = model
    return by_provider


def unleash_warmup_models() -> tuple[str, ...]:
    """Models to pre-warm in UnleashPool (tier 0-2, unique upstream ids).

    Skips tier 3 thinker/file-picker agents — they spawn from parent sessions.
    Order follows ALL_MODELS priority (newest per provider first).
    """
    seen: set[str] = set()
    out: list[str] = []
    for model in ALL_MODELS:
        if model.tier > 2:
            continue
        uid = model.upstream_id
        if uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return tuple(out)


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
            display_name=model.display_name or f"Freebuff {model.upstream_id}",
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
