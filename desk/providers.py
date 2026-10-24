"""按角色选择模型（DeepSeek / 中转的 Grok、GPT），缺密钥时回退 DeepSeek。"""
from __future__ import annotations

import os

from desk import settings

FALLBACKS: list[str] = []          # 本次运行发生的回退说明（写入日报）
MODEL_PROVIDER: dict[str, str] = {}  # 模型名 → 厂商（用于按厂商统计）
_cache: dict = {}


def resolve(cfg, role: str) -> dict:
    """返回 {provider, model, base_url, key_env, fallback}；不返回密钥本身。"""
    rm = cfg.get("role_models", {}).get(role)
    L = cfg["llm"]
    ds = {"provider": "deepseek", "base_url": settings.llm_base_url(cfg), "key_env": settings.key_source(cfg)}
    if not rm:
        return {**ds, "model": L["quick_model"], "fallback": None}
    tier_model = L["deep_model" if rm.get("tier") == "deep" else "quick_model"]
    pv = cfg["providers"].get(rm["provider"], {"kind": "deepseek"})
    if pv["kind"] == "deepseek":
        return {**ds, "model": os.environ.get(f"TA_MODEL_{role.upper()}") or rm["model"], "fallback": None}
    base = os.environ.get(pv["base_url_env"]) or pv.get("default_base_url")
    if not os.environ.get(pv["key_env"]) or not base:
        why = f"{role}: {rm['provider']}/{rm['model']} 缺少 {pv['key_env'] if not os.environ.get(pv['key_env']) else pv['base_url_env']} → 回退 DeepSeek {tier_model}"
        return {**ds, "model": tier_model, "fallback": why}
    return {"provider": rm["provider"], "model": os.environ.get(f"TA_MODEL_{role.upper()}") or rm["model"],
            "base_url": base, "key_env": pv["key_env"], "fallback": None}


def build(cfg, role: str, tracker=None):
    """为角色创建聊天模型（同一角色复用实例）。"""
    if os.environ.get("TA_DESK_MOCK_LLM") == "1":
        from desk.llm import FakeChatModel
        return FakeChatModel(tier=role, callbacks=[tracker] if tracker else None)
    key = (role, id(tracker))
    if key in _cache:
        return _cache[key]
    r = resolve(cfg, role)
    if r["fallback"] and r["fallback"] not in FALLBACKS:
        FALLBACKS.append(r["fallback"])
    MODEL_PROVIDER[r["model"]] = r["provider"]
    from tradingagents.llm_clients import create_llm_client
    kw = {"callbacks": [tracker]} if tracker else {}
    kw["max_retries"] = cfg["llm"].get("max_retries", 3)
    if r["provider"] == "deepseek":
        from desk.llm import prepare_env
        prepare_env(cfg)
        llm = create_llm_client("deepseek", r["model"], r["base_url"], **kw).get_llm()
    else:
        kw["api_key"] = os.environ[r["key_env"]]
        kw["timeout"] = 180
        llm = create_llm_client("openai_compatible", r["model"], r["base_url"], **kw).get_llm()
    _cache[key] = llm
    return llm


def assignment_table(cfg) -> list[dict]:
    out = []
    for role in cfg.get("role_models", {}):
        r = resolve(cfg, role)
        out.append({"role": role, "provider": r["provider"], "model": r["model"], "fallback": r["fallback"]})
    return out


def provider_of(model: str) -> str:
    if model in MODEL_PROVIDER:
        return MODEL_PROVIDER[model]
    m = (model or "").lower()
    return "deepseek" if "deepseek" in m else "grok" if m.startswith("grok") else "gpt" if m.startswith("gpt") else "其他"
