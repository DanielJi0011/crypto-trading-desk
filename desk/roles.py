"""角色登记、角色手册（playbook）存储与版本、以及把手册注入 TradingAgents 每个角色的提示词。

注入方式：不修改 TradingAgents 源码；在 tradingagents.graph.setup 中把每个 create_* 工厂包一层，
让该角色拿到的 LLM 在每次调用前把"本角色手册"追加到系统提示中（每次调用时实时读取当前版本）。"""
from __future__ import annotations

import contextvars
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, convert_to_messages
from langchain_core.prompt_values import PromptValue
from langchain_core.runnables import Runnable, RunnableLambda

from desk import settings

# key, 中文名, TradingAgents 工厂函数名, 初始权重, 是否"汇总型"角色
ROLES = [
    ("market", "市场分析师", "create_market_analyst", 1.0),
    ("sentiment", "情绪分析师", "create_sentiment_analyst", 1.0),
    ("news", "新闻分析师", "create_news_analyst", 1.0),
    ("flows", "资金流分析师", None, 1.0),            # 本系统新增：衍生品/资金流（TradingAgents 图之外先运行）
    ("bull", "多头研究员", "create_bull_researcher", 1.0),
    ("bear", "空头研究员", "create_bear_researcher", 1.0),
    ("research_manager", "研究经理", "create_research_manager", 1.5),
    ("trader", "交易员", "create_trader", 1.0),
    ("aggressive", "激进风控", "create_aggressive_debator", 0.75),
    ("conservative", "保守风控", "create_conservative_debator", 0.75),
    ("neutral", "中性风控", "create_neutral_debator", 0.75),
    ("portfolio_manager", "投资组合经理", "create_portfolio_manager", 2.0),
]
ROLE_KEYS = [r[0] for r in ROLES]
ROLE_CN = {r[0]: r[1] for r in ROLES}
INIT_WEIGHT = {r[0]: r[3] for r in ROLES}

STANCE_SPEC = ("在你的输出最后单独写一行（这一行必须保留，程序会读取）：\n"
               "【立场】方向=看多/看空/中性；目标价=数字或无；止损价=数字或无；置信度=0到1之间的小数；周期=N天；入场价=数字或无；入场条件=涨到/跌到/现价\n"
               "（标的是 OKX USDT 永续合约，可做多也可做空：看多=建议做多，看空=建议做空，中性=不交易。入场价=你建议的开仓触发价位；涨到=价格≥该价时开仓（做多突破/做空反弹），跌到=价格≤该价时开仓（做多回踩/做空破位），现价=立即开仓；不建议交易就写无。止损价：做多在入场价下方，做空在上方）\n"
               "例：【立场】方向=看多；目标价=92000；止损价=78500；置信度=0.6；周期=5天；入场价=80500；入场条件=跌到")

FIXED_HEADER = """## 输出要求（固定，不参与学习修订）
- {spec}
- 价格一律写 USDT 绝对价位，不写百分比。
- 只用提示中提供的数据和工具结果，不编造数字；数据缺失就明确说缺失。
"""

INIT_LEARNED = "（暂无经验规则。规则只会在本角色累计至少 20 条已打分决策后，由交易经理依据复盘数据添加，每次最多 1～2 处改动。）"


# ------------------------------------------------------------------ 手册存储
def role_dir(role: str) -> Path:
    return settings.PLAYBOOKS / role


def meta_path(role: str) -> Path:
    return role_dir(role) / "meta.json"


def read_meta(role: str) -> dict:
    return settings.read_json(meta_path(role), {"current": 0, "history": []})


def version_path(role: str, v: int) -> Path:
    return role_dir(role) / f"v{v:03d}.md"


def compose(role: str, v: int, learned: str) -> str:
    return (f"# 角色手册：{ROLE_CN[role]}（v{v:03d}）\n\n" + FIXED_HEADER.format(spec=STANCE_SPEC)
            + "\n## 经验规则（由交易经理依据复盘修订）\n" + learned.strip() + "\n")


def learned_section(text: str) -> str:
    m = re.search(r"## 经验规则[^\n]*\n(.*)\Z", text, re.S)
    return m.group(1).strip() if m else ""


def ensure_playbooks():
    for role in ROLE_KEYS:
        if read_meta(role)["current"] == 0:
            role_dir(role).mkdir(parents=True, exist_ok=True)
            version_path(role, 1).write_text(compose(role, 1, INIT_LEARNED), encoding="utf-8")
            settings.write_json(meta_path(role), {"current": 1, "history": [
                {"version": 1, "time": settings.now_str(), "reason": "初始版本", "changes": [],
                 "baseline_hit": None, "baseline_n": 0, "scored_at_edit": 0, "eval_status": "baseline"}]})


def versions() -> dict:
    """L1：当前各角色（含交易经理）手册版本号，写进每条决策记录。"""
    return {r: read_meta(r).get("current", 0) for r in ROLE_KEYS + ["trade_manager"]}


def current_text(role: str) -> str:
    m = read_meta(role)
    return version_path(role, m["current"]).read_text(encoding="utf-8") if m["current"] else ""


def save_new_version(role: str, learned: str, reason: str, changes: list, baseline_hit, baseline_n, scored_at_edit,
                     rollback_of=None) -> int:
    m = read_meta(role)
    v = max(h["version"] for h in m["history"]) + 1
    version_path(role, v).write_text(compose(role, v, learned), encoding="utf-8")
    m["history"].append({"version": v, "time": settings.now_str(), "reason": reason, "changes": changes,
                         "baseline_hit": baseline_hit, "baseline_n": baseline_n, "scored_at_edit": scored_at_edit,
                         "eval_status": "rollback" if rollback_of else "pending", "rollback_of": rollback_of,
                         "previous": m["current"]})
    m["current"] = v
    settings.write_json(meta_path(role), m)
    return v


# ------------------------------------------------------------------ 注入
def _to_messages(inp):
    if isinstance(inp, PromptValue):
        return inp.to_messages()
    if isinstance(inp, str):
        return [HumanMessage(content=inp)]
    if isinstance(inp, list):
        return convert_to_messages(inp)
    return inp


_BRIEFS_VAR: contextvars.ContextVar = contextvars.ContextVar("ta_desk_briefs", default=None)


class _Briefs:
    """角色 → 本轮专属数据简报（每个币分析前设置）。
    并行改造：底层是 ContextVar，每个币的分析线程（copy_context().run）各自持有一份，互不覆盖；
    LangGraph 在执行节点/工具时会复制调用方上下文，所以图内各角色读到的是本币的简报。
    串行（单线程）下与原来的模块级 dict 行为一致。只读接口：get / [] / in / items / len。"""

    def _d(self) -> dict:
        return _BRIEFS_VAR.get() or {}

    def get(self, k, default=None):
        return self._d().get(k, default)

    def __getitem__(self, k):
        return self._d()[k]

    def __contains__(self, k):
        return k in self._d()

    def __len__(self):
        return len(self._d())

    def __iter__(self):
        return iter(self._d())

    def items(self):
        return self._d().items()

    def clear(self):
        _BRIEFS_VAR.set({})


BRIEFS = _Briefs()
FLOWS_READERS = ("bull", "bear", "research_manager", "trader", "portfolio_manager")


def set_briefs(briefs: dict):
    """整体替换当前上下文的简报（新 dict，不原地改，避免别的线程看到半截状态）。"""
    _BRIEFS_VAR.set({k: v for k, v in briefs.items() if v})


class PlaybookLLM(Runnable):
    """包装一个聊天模型：每次调用前把角色手册追加到系统提示。"""

    def __init__(self, inner, role: str):
        self.inner, self.role = inner, role

    def _inject(self, inp):
        msgs = list(_to_messages(inp))
        pb = current_text(self.role)
        block = ""
        if pb:
            block += f"\n\n---\n[交易台给你（{ROLE_CN.get(self.role, self.role)}）的角色手册，请遵守]\n{pb}"
        if BRIEFS.get(self.role):   # 专属数据不再依赖手册是否存在
            block += f"\n\n---\n[只提供给你（{ROLE_CN.get(self.role, self.role)}）的专属数据]\n{BRIEFS[self.role]}"
        if BRIEFS.get("*"):         # B3：全体角色共用（带方向的持仓）
            block += f"\n\n---\n[交易台共享信息]\n{BRIEFS['*']}"
        if not block:
            return msgs
        if msgs and isinstance(msgs[0], SystemMessage) and isinstance(msgs[0].content, str):
            msgs[0] = SystemMessage(content=msgs[0].content + block)
        else:
            msgs.insert(0, SystemMessage(content=block.strip()))
        return msgs

    def invoke(self, input, config=None, **kw):
        return self.inner.invoke(self._inject(input), config, **kw)

    def bind_tools(self, tools, **kw):
        return RunnableLambda(self._inject) | self.inner.bind_tools(tools, **kw)

    def with_structured_output(self, schema, **kw):
        return RunnableLambda(self._inject) | self.inner.with_structured_output(schema, **kw)


_patched = {}


def install(cfg=None, tracker=None):
    """把手册注入 + 按角色换模型装进 TradingAgents 的图构建（幂等）。
    cfg 给出时，每个角色使用 role_models 指定的模型（缺密钥回退 DeepSeek）。"""
    import tradingagents.graph.setup as setup
    ensure_playbooks()
    for key, _, fname, _ in ROLES:
        if fname is None:
            continue
        if fname not in _patched:
            _patched[fname] = getattr(setup, fname)
        orig = _patched[fname]

        def make(o, r):
            def factory(llm):
                if cfg is not None and r in cfg.get("role_models", {}):
                    from desk import providers
                    llm = providers.build(cfg, r, tracker)
                return o(PlaybookLLM(llm, r))
            return factory
        setattr(setup, fname, make(orig, key))
