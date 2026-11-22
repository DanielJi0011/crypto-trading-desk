"""交易经理「先交表、后分析」（2026-11-22 用户批准，冻结期例外）。

事故：2026-11-22 晚场 SOL 交易经理先写长分析、撞满 8192 输出上限被截断，下单工具调用
没来得及发出 → 解析为空（[PARSE] 工具调用输出为空），3 次重试同形状全灭 → 缺席。

修复（仅 tm_order_tool 内）：
1) instructions() 输出顺序硬要求：先发工具调用，分析只进 rationale 字段、其后文字从简；
2) retry_prompt()：失败属于「解析为空/截断类」时，重试追加「只交订单表、不写分析」；
   参数校验失败的重试话术不变；
3) 解析容忍经本文件探针验证：生产同款客户端（LocalCompatibleChatOpenAI +
   with_structured_output，function_calling）下，「工具调用已完整发出 + 其后分析文字
   被截断」的响应可被现解析路径提取 → 未改解析代码。

夹具形状 = 生产真实形状：AIMessage（content=分析文字，tool_calls=[{name, args, id}]），
经生产同款结构化输出链 invoke。全部离线，不发任何请求（_generate 被覆写为回放预置消息）。
"""
from typing import ClassVar

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from desk import settings, tm_order_tool as T

ARGS = {"action": "hold", "invalidation": "站上 106 说明判断错", "rationale": "趋势未变，维持观望"}
CTX = {"inst": "BTC-USDT-SWAP", "mk": {"last": 100.0, "atr14": 3.0}, "bk": {"equity": 100000.0},
       "pos": None, "existing": None, "consensus_side": None, "S": 0.0, "trend": None}
TRUNC = "综合各角色观点，本次趋势延续的证据主要在于……（长分析写到一半被输出上限截断，未及收尾"


def _msg(content, args=None):
    tc = [{"name": "OrderToolCall", "args": dict(args), "id": "call_1", "type": "tool_call"}] if args else []
    return AIMessage(content=content, tool_calls=tc)


def _chain(queue):
    """按生产方式构造结构化输出链；queue 是逐次 invoke 回放的 AIMessage 列表。"""
    from tradingagents.llm_clients.openai_client import LocalCompatibleChatOpenAI

    class Stub(LocalCompatibleChatOpenAI):
        queue: ClassVar[list] = []
        seen: ClassVar[list] = []

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            type(self).seen.append("\n".join(str(getattr(m, "content", "")) for m in messages))
            return ChatResult(generations=[ChatGeneration(message=type(self).queue.pop(0))])

    Stub.queue = list(queue)
    Stub.seen = []
    client = Stub(model="claude-opus-5-5", api_key="test-dummy", base_url="http://127.0.0.1:9")
    return client.with_structured_output(T.OrderToolCall), Stub


def _cfg():
    return settings.load_config()


# ---------- (a) 工具调用完整 + 其后文字被截断 → 订单被提取（现解析路径，不改解析） ----------
def test_complete_tool_call_with_truncated_prose_is_extracted():
    chain, _ = _chain([_msg(TRUNC, ARGS)])
    res = T.run(_cfg(), chain, "PROMPT", dict(CTX))
    assert res["dec"] is not None and res["attempts"] == 1 and res["absent_why"] is None
    assert res["call"]["action"] == "hold"


def test_prose_only_truncated_reproduces_incident_shape():
    """事故原形复现：只有分析文字、工具调用没发出 → 解析为空，本场无结论。"""
    chain, _ = _chain([_msg(TRUNC), _msg(TRUNC), _msg(TRUNC)])
    res = T.run(_cfg(), chain, "PROMPT", dict(CTX))
    assert res["dec"] is None and res["attempts"] == T.TOOL_MAX_ATTEMPTS
    assert all("[PARSE]" in e and "工具调用输出为空" in e for h in res["history"] for e in h["errors"])


# ---------- (b) 解析为空 → 重试提示含「只交表」要求；校验失败 → 话术不变 ----------
def test_retry_after_parse_empty_demands_form_only():
    chain, stub = _chain([_msg(TRUNC), _msg("（按要求只交表）", ARGS)])
    res = T.run(_cfg(), chain, "PROMPT", dict(CTX))
    assert res["dec"] is not None and res["attempts"] == 2
    assert "ONLY the order-tool call" in stub.seen[1] and "no analysis prose" in stub.seen[1]


def test_retry_prompt_form_only_only_for_parse_empty():
    parse_hist = [{"call": None, "errors": ["[PARSE] 工具参数无法解析（ValueError: 工具调用输出为空）"]}]
    p = T.retry_prompt("PROMPT", parse_hist)
    assert "ONLY the order-tool call" in p and "no analysis prose" in p and "(unparseable)" in p
    guard_hist = [{"call": {"action": "pending"}, "errors": ["[MISSING] action=pending 缺必填参数：stop_loss"]}]
    p2 = T.retry_prompt("PROMPT", guard_hist)
    assert "ONLY the order-tool call" not in p2 and "Rejection reasons" in p2 and "[MISSING]" in p2


# ---------- (c) 正常完整响应行为不变 ----------
def test_normal_complete_response_unchanged():
    chain, _ = _chain([_msg("简短分析。", ARGS)])
    res = T.run(_cfg(), chain, "PROMPT", dict(CTX))
    assert res["dec"] is not None and res["attempts"] == 1 and res["history"] == []
    assert T.retry_prompt("PROMPT", []) == "PROMPT"


# ---------- (1) 首场提示词带输出顺序硬要求 ----------
def test_instructions_demand_form_first():
    text = T.instructions(dict(CTX))
    assert "OUTPUT ORDER (mandatory)" in text and "emit the order-tool call FIRST" in text
