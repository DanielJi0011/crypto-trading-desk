"""LLM 相关：TradingAgents 配置、密钥注入、token 用量与成本记录、模拟 LLM（无密钥测试用）。"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import os
import threading
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from desk import settings

STAGE = contextvars.ContextVar("stage", default="未标记")
RUN_ID = os.environ.get("TA_RUN_ID") or settings.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def prepare_env(cfg) -> bool:
    """把 TA_LLM_API_KEY（或备用变量）注入到 TradingAgents provider 读取的变量名中（仅本进程）。"""
    from tradingagents.llm_clients.api_key_env import get_api_key_env
    key = settings.llm_api_key(cfg)
    env_name = get_api_key_env(cfg["llm"]["provider"])
    if key and env_name and not os.environ.get(env_name):
        os.environ[env_name] = key
    os.environ.setdefault("TRADINGAGENTS_NO_UPDATE_CHECK", "1")
    return bool(key)


def ta_config(cfg) -> dict:
    from tradingagents.default_config import build_default_config
    c = build_default_config()
    L = cfg["llm"]
    c.update({
        "results_dir": str(settings.STATE / "ta_results"),
        "data_cache_dir": str(settings.STATE / "ta_cache"),
        "memory_log_path": str(settings.STATE / "ta_memory" / "trading_memory.md"),
        "llm_provider": L["provider"],
        "deep_think_llm": L["deep_model"],
        "quick_think_llm": L["quick_model"],
        "backend_url": settings.llm_base_url(cfg),
        "output_language": L.get("output_language", "Chinese"),
        "max_debate_rounds": L.get("max_debate_rounds", 1),
        "max_risk_discuss_rounds": L.get("max_risk_discuss_rounds", 1),
        "max_tool_rounds": L.get("max_tool_rounds", 8),
        "temperature": L.get("temperature"),
        "llm_max_retries": L.get("max_retries"),
        "holding_period_days": cfg["scoring"]["horizon_days"],
        "benchmark_ticker": "BTC-USD",         # 加密货币的反思以 BTC 为基准（BTC 自身 alpha=0）
        "global_news_queries": [
            "Bitcoin crypto market regulation ETF flows",
            "Federal Reserve interest rates inflation",
            "stablecoin exchange hack liquidation",
        ],
    })
    c["data_vendors"] = dict(c["data_vendors"])
    c["data_vendors"]["core_stock_apis"] = "okx,yfinance" if cfg["data"].get("yahoo_fallback", True) else "okx"
    return c


# ------------------------------------------------------------------ 用量 / 成本
def _is_peak(t: _dt.datetime) -> bool:
    u = t.astimezone(_dt.timezone.utc)
    return u.weekday() < 5 and (1 <= u.hour < 4 or 6 <= u.hour < 10)


def price_of(cfg, model: str) -> dict:
    p = cfg["llm"]["pricing"]
    m = cfg["llm"].get("model_aliases", {}).get(model, model)
    if m in p:
        return p[m]
    from desk.providers import provider_of
    if provider_of(model) in ("grok", "gpt", "claude"):
        return p.get("relay-unknown", p["default"])
    return p["default"]


def price_known(cfg, model: str) -> bool:
    """该模型是否有真实单价（中转 relay-unknown 记 0 只是占位，不算已知）。"""
    p = cfg["llm"]["pricing"]
    m = cfg["llm"].get("model_aliases", {}).get(model, model)
    if m in p and m != "relay-unknown":
        return True
    from desk.providers import provider_of
    return provider_of(model) not in ("grok", "gpt", "claude") and "default" in p


def cost_usd(cfg, model, inp, cached, out, t=None) -> float:
    pr = price_of(cfg, model)
    f = 1.0 if _is_peak(t or settings.now()) else pr.get("offpeak_factor", 1.0)
    return ((inp - cached) * pr["input_miss"] + cached * pr["input_hit"] + out * pr["output"]) / 1e6 * f


class UsageTracker(BaseCallbackHandler):
    """记录每一次 LLM 调用的 token 与成本估算到 state/usage/<日期>.jsonl。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = settings.STATE / "usage" / f"{settings.today()}.jsonl"
        self.calls = []
        self._node = {}
        self._lock = threading.Lock()   # 并行分析：多线程回调时保护 calls/_node 与 jsonl 追加

    @staticmethod
    def _node_name(metadata):
        md = metadata or {}
        top = (md.get("langgraph_checkpoint_ns") or "").split("|")[0].split(":")[0]
        return top or md.get("langgraph_node")

    def on_chat_model_start(self, serialized, messages, *, run_id=None, metadata=None, **kw):
        with self._lock:
            self._node[run_id] = self._node_name(metadata)

    def on_llm_start(self, serialized, prompts, *, run_id=None, metadata=None, **kw):
        with self._lock:
            self._node[run_id] = self._node_name(metadata)

    def on_llm_end(self, response, *, run_id=None, **kw):
        with self._lock:
            node = self._node.pop(run_id, None)
        for gens in response.generations:
            for g in gens:
                msg = getattr(g, "message", None)
                um = getattr(msg, "usage_metadata", None) or {}
                rm = getattr(msg, "response_metadata", None) or {}
                tu = (response.llm_output or {}).get("token_usage", {}) if response.llm_output else {}
                inp = um.get("input_tokens") or tu.get("prompt_tokens") or 0
                out = um.get("output_tokens") or tu.get("completion_tokens") or 0
                cached = (um.get("input_token_details") or {}).get("cache_read") or tu.get("prompt_cache_hit_tokens") or 0
                reasoning = (um.get("output_token_details") or {}).get("reasoning") or 0
                model = rm.get("model_name") or (response.llm_output or {}).get("model_name") or "unknown"
                from desk.providers import provider_of
                rec = {"run_id": RUN_ID, "time": settings.now_str(), "stage": STAGE.get(), "node": node, "model": model,
                       "provider": provider_of(model),
                       "input": inp, "cached": cached, "output": out, "reasoning": reasoning,
                       "cost_usd": round(cost_usd(self.cfg, model, inp, cached, out), 6)}
                with self._lock:
                    self.calls.append(rec)
                    settings.append_jsonl(self.path, rec)

    def on_llm_error(self, error, *, run_id=None, **kw):
        with self._lock:
            rec = {"run_id": RUN_ID, "time": settings.now_str(), "stage": STAGE.get(),
                   "node": self._node.pop(run_id, None), "error": type(error).__name__}
            self.calls.append(rec)
            settings.append_jsonl(self.path, rec)

    def summary(self) -> dict:
        with self._lock:
            calls = list(self.calls)
        ok = [c for c in calls if "error" not in c]
        by_stage, by_model, by_node, by_provider = {}, {}, {}, {}
        for c in ok:
            for d, k in ((by_stage, c["stage"]), (by_model, c["model"]), (by_node, c.get("node") or c["stage"].split("/")[-1]),
                         (by_provider, c.get("provider", "?"))):
                x = d.setdefault(k, {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0.0})
                x["calls"] += 1; x["input"] += c["input"]; x["cached"] += c["cached"]
                x["output"] += c["output"]; x["cost_usd"] = round(x["cost_usd"] + c["cost_usd"], 6)
                x["unpriced_calls"] = x.get("unpriced_calls", 0) + (0 if price_known(self.cfg, c["model"]) else 1)
        return {"run_id": RUN_ID, "calls": len(ok), "errors": len(calls) - len(ok),
                "input": sum(c["input"] for c in ok), "cached": sum(c["cached"] for c in ok),
                "output": sum(c["output"] for c in ok), "cost_usd": round(sum(c["cost_usd"] for c in ok), 6),
                "unpriced_calls": sum(1 for c in ok if not price_known(self.cfg, c["model"])),
                "by_stage": by_stage, "by_model": by_model, "by_node": by_node, "by_provider": by_provider}


def make_llm(cfg, tier: str, tracker=None, role: str | None = None):
    """本系统自己的 LLM 调用（立场抽取、交易经理、复盘会、手册修订、资金流、研究）用的客户端。
    role 在 role_models 中时按角色选模型。"""
    if role and role in cfg.get("role_models", {}) and os.environ.get("TA_DESK_MOCK_LLM") != "1":
        from desk import providers
        return providers.build(cfg, role, tracker)
    if os.environ.get("TA_DESK_MOCK_LLM") == "1":
        return FakeChatModel(tier=tier, callbacks=[tracker] if tracker else None)
    from tradingagents.llm_clients import create_llm_client
    from tradingagents.llm_clients.factory import build_llm_kwargs
    c = ta_config(cfg)
    kw = build_llm_kwargs(c)
    if tracker:
        kw["callbacks"] = [tracker]
    model = cfg["llm"]["deep_model" if tier == "deep" else "quick_model"]
    return create_llm_client(cfg["llm"]["provider"], model, settings.llm_base_url(cfg), **kw).get_llm()


# ------------------------------------------------------------------ 模拟 LLM（无密钥测试）
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402


def _fake_value(annotation, name):
    import enum
    import typing
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)) and args:
        return _fake_value(args[0], name)
    if origin is typing.Literal:
        return typing.get_args(annotation)[0]
    if origin in (list, tuple):
        return [_fake_value(args[0], name)] if args else []
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return list(annotation)[0]
        if hasattr(annotation, "model_fields"):
            return _fake_instance(annotation)
        if annotation is bool:
            return False
        if annotation is int:
            return 1
        if annotation is float:
            return 0.5
    return f"（模拟输出：{name}）"


def _fake_instance(schema):
    hook = FAKE_STRUCTURED.get(schema.__name__)
    if hook:
        return hook(schema)
    vals = {n: _fake_value(f.annotation, n) for n, f in schema.model_fields.items() if f.is_required()}
    return schema(**vals)


FAKE_STRUCTURED: dict = {}   # 名称 → 构造函数（测试/模拟时注册更真实的输出）


class FakeChatModel(BaseChatModel):
    """模拟聊天模型：
    - 绑定工具且尚未调用过工具时，发起一次真实工具调用（验证 OKX 数据接入整个图）
    - with_structured_output 返回按 schema 构造的实例
    - 每次调用产生 usage_metadata，验证用量记录"""
    tier: str = "quick"
    tools: list = []

    @property
    def _llm_type(self) -> str:
        return "fake-ta-desk"

    def bind_tools(self, tools, **kw):
        return FakeChatModel(tier=self.tier, tools=list(tools), callbacks=self.callbacks)

    def with_structured_output(self, schema, **kw):
        def run(inp):
            self.invoke(inp)  # 走一遍以记录用量
            return _fake_instance(schema)
        return RunnableLambda(run)

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        n_in = sum(len(str(m.content)) for m in messages) // 3
        has_tool_result = any(isinstance(m, ToolMessage) for m in messages)
        if self.tools and not has_tool_result:
            names = [getattr(t, "name", "") for t in self.tools]
            calls = []
            if "get_stock_data" in names:
                calls.append({"name": "get_stock_data", "args": {"start_date": "2026-09-01", "end_date": "2099-01-01"}, "id": "c1"})
            if "get_verified_market_snapshot" in names:
                calls.append({"name": "get_verified_market_snapshot", "args": {"curr_date": "2099-01-01", "look_back_days": 10}, "id": "c2"})
            if "get_news" in names:
                calls.append({"name": "get_news", "args": {"start_date": "2026-10-01", "end_date": "2099-01-01"}, "id": "c3"})
            if calls:
                msg = AIMessage(content="", tool_calls=calls)
                msg.usage_metadata = {"input_tokens": n_in, "output_tokens": 20, "total_tokens": n_in + 20}
                msg.response_metadata = {"model_name": f"fake-{self.tier}"}
                return ChatResult(generations=[ChatGeneration(message=msg)])
        text = ("（模拟报告）根据工具返回的数据，价格处于区间震荡。观点：中性偏多，目标价 90000，止损 78000，置信度 0.55。\n"
                "**Rating**: Hold\nFINAL TRANSACTION PROPOSAL: **HOLD**\n"
                "【立场】方向=看多；目标价=90000；止损价=78000；置信度=0.55；周期=5天")
        if any("交易台给你" in str(m.content) for m in messages):
            text += "\n（模拟：已读到角色手册）"
        tool_snips = [str(m.content)[:300] for m in messages if isinstance(m, ToolMessage)]
        if tool_snips:
            text += "\n\n工具数据摘录：\n" + "\n".join(tool_snips)
        msg = AIMessage(content=text)
        msg.usage_metadata = {"input_tokens": n_in, "output_tokens": len(text) // 3, "total_tokens": n_in + len(text) // 3}
        msg.response_metadata = {"model_name": f"fake-{self.tier}"}
        return ChatResult(generations=[ChatGeneration(message=msg)])


def install_fake_into_tradingagents():
    """让 TradingAgentsGraph 使用模拟 LLM。"""
    import tradingagents.graph.trading_graph as tg

    class _C:
        def __init__(self, tier, callbacks):
            self.tier, self.callbacks = tier, callbacks

        def get_llm(self):
            return FakeChatModel(tier=self.tier, callbacks=self.callbacks)

    def fake_create_tier_client(config, tier, **extra):
        return _C(tier, extra.get("callbacks"))
    tg.create_tier_client = fake_create_tier_client


def dumps(o):
    return json.dumps(o, ensure_ascii=False, default=str)
