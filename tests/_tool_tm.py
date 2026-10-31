"""测试辅助：按交易经理下单工具参数构造执行层使用的决定（固定走工具，与线上同一映射 tm_order_tool.to_decision）。"""
from desk import tm_order_tool as T


def tool_tm(action, existing=None, **kw):
    """action 取工具动作（open/add/pending/keep_pending/replace_pending/cancel_pending/hold/close/reduce）。"""
    kw.setdefault("rationale", "r")
    if action in ("pending", "replace_pending", "keep_pending"):
        kw.setdefault("expire_hours", 24)
    return T.to_decision(T.OrderToolCall(action=action, **kw), {"existing": existing})


def from_side(side, condition="now", pos=False, entry=80000.0, sl=None, tp=None, **kw):
    """方向+条件 → 工具调用：condition=now 为现价 open/add，否则 pending（gte/lte 触发）。"""
    long_ = side == "long"
    sl = sl if sl is not None else (entry * 0.975 if long_ else entry * 1.025)
    tp = tp if tp is not None else (entry * 1.05 if long_ else entry * 0.95)
    if condition == "now":
        return tool_tm("add" if pos else "open", side=side, entry=entry, stop_loss=sl, take_profit=tp, **kw)
    return tool_tm("pending", side=side, trigger_px=entry, condition=condition, stop_loss=sl, take_profit=tp, **kw)
