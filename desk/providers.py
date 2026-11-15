"""按角色选择模型（DeepSeek / 中转的 Grok、GPT、Claude），缺密钥时回退 DeepSeek。"""
from __future__ import annotations

import os
import threading

from desk import settings

FALLBACKS: list[str] = []          # 本次运行发生的回退说明（写入日报）
MODEL_PROVIDER: dict[str, str] = {}  # 模型名 → 厂商（用于按厂商统计）
_cache: dict = {}
_LOCK = threading.RLock()   # 并行分析：首次创建客户端加锁，避免同一角色建两个实例


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
            "base_url": base, "key_env": pv["key_env"], "fallback": None, "reasoning": rm.get("reasoning")}


def build(cfg, role: str, tracker=None):
    """为角色创建聊天模型（同一角色复用实例）。"""
    if os.environ.get("TA_DESK_MOCK_LLM") == "1":
        from desk.llm import FakeChatModel
        return FakeChatModel(tier=role, callbacks=[tracker] if tracker else None)
    with _LOCK:
        return _build_locked(cfg, role, tracker)


def _make_relay_client(cfg, r, tracker, api_key):
    """按解析结果构造一个中继（openai_compatible）客户端；api_key 由调用方给出（不读 env、不打印）。"""
    from tradingagents.llm_clients import create_llm_client
    kw = {"callbacks": [tracker]} if tracker else {}
    kw["max_retries"] = cfg["llm"].get("max_retries", 3)
    kw["api_key"] = api_key
    kw["timeout"] = 180
    llm = create_llm_client("openai_compatible", r["model"], r["base_url"], **kw).get_llm()
    # TradingAgents 的 openai_compatible 只对 gpt-5+/o 系列转发 reasoning_effort；中转站对 grok/gpt/claude 均接受，这里直接设置
    if r.get("reasoning") in ("low", "medium", "high"):
        llm.reasoning_effort = r["reasoning"]
    return llm


def _register_backup(cfg, r, tracker, primary_llm) -> None:
    """claude 渠道主备 key 切换（2026-11-15）：备用 key 环境变量 <key_env>_BACKUP 存在时，
    为主客户端建一个同参数的备用孪生并登记到 llm_retry——主 key 重试耗尽后同一次调用自动改走
    备用（见 desk/llm_retry.py）。备用更贵，绝不默认走；备用 env 缺失（冷备副本即如此）时
    不登记、行为与无此功能时完全一致。孪生构造失败绝不影响主客户端。"""
    if r["provider"] != "claude":
        return
    bkey = os.environ.get(r["key_env"] + "_BACKUP")
    if not bkey:
        return
    try:
        twin = _make_relay_client(cfg, r, tracker, bkey)
        from desk import llm_retry
        llm_retry.register_backup(primary_llm, twin)
    except Exception:
        pass


def _build_locked(cfg, role: str, tracker=None):
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
        llm = _make_relay_client(cfg, r, tracker, os.environ[r["key_env"]])
        _register_backup(cfg, r, tracker, llm)
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
    return "deepseek" if "deepseek" in m else "grok" if m.startswith("grok") else "gpt" if m.startswith("gpt") else "claude" if m.startswith("claude") else "其他"
