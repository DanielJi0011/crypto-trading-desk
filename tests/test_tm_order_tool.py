"""2026-10-31 用户决定（manager.tm_order_tool，分支 feat/tm-order-tool）：交易经理通过受限下单工具直接下单。
全部离线：只用 mock 模型（脚本化输出），模拟盘下单函数全部替换为记录器，不调用付费模型、不发任何请求。"""
import pytest

from desk import demo_exec as DX, manager, okx_demo as X, settings, tm_order_tool as T
from tests.test_tm_no_vote import _st, desk_env, MK, SOURCES  # noqa: F401

OTC = T.OrderToolCall
INV = dict(invalidation="站上 106 说明判断错", rationale="测试理由")
EX = {"algoId": "OLD", "side": "short", "trigger_px": 102.0, "condition": "gte", "stop_loss": 105.0, "take_profit": 93.0,
      "sz": "7", "expires": "2026-11-03 20:00:00+0800"}
POS = {"trade_id": "T1", "inst_id": "", "side": "short", "pos_side": "short", "avg_px": 101.0, "price": 100.0, "qty": 1.0,
       "contracts": 1, "notional": 100.0, "margin": 100.0, "leverage": 1, "sl_px": 104.0, "tp_px": 92.0, "upnl": 1.0,
       "liq_px": None, "funding": 0.0, "deadline": "2026-11-05", "opened": "2026-10-29 08:00:00"}


@pytest.fixture
def env(desk_env, monkeypatch):
    manager_, calls, box = desk_env
    from desk import demo_ws, drift
    calls.update(add_now=[], place_add_trigger=[], amend=[], expiry=[], check=[], prompts=[], close=[], reduce=[], move_stop=[])
    box.update(script=[], guard=lambda qty, add: [], ex=None, pos=None)
    import desk.llm as dllm

    class Fake:
        def with_structured_output(self, schema):
            box["schema"] = schema
            return self

        def invoke(self, prompt, *a, **k):
            calls["prompts"].append(prompt)
            out = box["script"].pop(0) if box["script"] else None
            if isinstance(out, Exception):
                raise out
            return out
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: Fake())
    monkeypatch.setattr(X, "instrument", lambda inst: {"ctVal": "1", "minSz": "1", "lotSz": "1"})
    monkeypatch.setattr(X, "contracts", lambda inst, q: (str(int(q)) if q >= 1 else None))
    monkeypatch.setattr(X, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(DX, "check_open", lambda cfg, bk, inst, qty, px, sl, side, lev, add=None:
                        (calls["check"].append((qty, px, sl, side, add)), (box["guard"](qty, add), {}))[1])
    monkeypatch.setattr(DX, "existing_trigger", lambda bk, inst: dict(box["ex"]) if box["ex"] else None)
    monkeypatch.setattr(DX, "add_now", lambda *a, **k: calls["add_now"].append(a) or (True, {"sz": a[4]}))
    monkeypatch.setattr(DX, "place_add_trigger", lambda *a, **k: calls["place_add_trigger"].append(a) or (True, {"algoId": "ADD"}))
    monkeypatch.setattr(DX, "amend_trigger_sltp", lambda cfg, inst, e, sl, tp: calls["amend"].append((sl, tp)) or
                        {"ok": True, "how": "amend", "algoId": e["algoId"]})
    monkeypatch.setattr(DX, "close", lambda cfg, p, why: calls["close"].append(why) or {"ok": True})
    monkeypatch.setattr(DX, "reduce", lambda cfg, p, f, why: calls["reduce"].append(f) or {"ok": True, "remaining": 0.5})
    monkeypatch.setattr(DX, "move_stop", lambda cfg, p, sl: calls["move_stop"].append(sl) or {"ok": True})
    monkeypatch.setattr(manager_, "apply_tm_expiry", lambda cfg, inst, a, tm, notes: calls["expiry"].append((a, tm.entry_expire_hours)) or None)
    monkeypatch.setattr(manager_, "pending_widen_guard", lambda *a, **k: (lambda *x, **y: []))
    monkeypatch.setattr(manager_, "pre_trade_gate", lambda *a, **k: None)
    monkeypatch.setattr(drift, "skip", lambda *a, **k: False)
    monkeypatch.setattr(demo_ws, "notify_tm_no_conclusion", lambda *a, **k: calls.setdefault("nc", []).append(a) or True)
    real_book = DX.book

    def book(cfg, prices=None):
        b = real_book(cfg, prices)
        p = box["pos"]
        b["positions"] = [dict(p, inst_id=cfg["coins"][0]["inst_id"])] if p else []
        return b
    monkeypatch.setattr(DX, "book", book)
    return manager_, calls, box


def _cfg(on=True):
    c = settings.load_config()
    c["manager"]["tm_order_tool"] = {"enabled": on}
    return c


def _go(m, box, script, source="daily", on=True, st=None):
    box["script"] = list(script)
    cfg = _cfg(on)
    return m.decide_and_execute(cfg, cfg["coins"][0], "2026-10-31", {}, st or _st("bearish"), None, source=source)


PEND = dict(action="pending", side="short", trigger_px=102.0, condition="gte", stop_loss=105.0, take_profit=93.0,
            expire_hours=48, margin_pct=5, **INV)


# ---------- 开关与默认 ----------
def test_switch_default_false():
    assert settings.load_config()["manager"]["tm_order_tool"]["enabled"] is False
    assert T.enabled(settings.load_config()) is False
    assert "tm_order_tool" in open("config.yaml", encoding="utf-8").read()


def test_switch_off_regression_uses_manager_decision(env):
    m, calls, box = env
    from tests.test_tm_no_vote import _dec
    box["script"] = [_dec(action="pending", entry_side="short", entry_trigger_px=102.0, entry_condition="gte",
                          entry_stop_loss=105.0, entry_take_profit=93.0, entry_size_pct=5)]
    cfg = _cfg(False)
    rec = m.decide_and_execute(cfg, cfg["coins"][0], "2026-10-31", {}, _st("bearish"), None)
    assert box["schema"] is m.ManagerDecision and "tm_order_tool" not in rec
    assert calls["place_trigger"] and "下单工具" not in calls["prompts"][0]


# ---------- 参数校验与重试 ----------
def test_missing_fields_returned_then_retry_ok(env):
    m, calls, box = env
    rec = _go(m, box, [OTC(action="pending", side="short", trigger_px=102.0, **INV), OTC(**PEND)])
    assert len(calls["prompts"]) == 2 and box["schema"] is T.OrderToolCall
    assert "[MISSING]" in calls["prompts"][1] and "stop_loss" in calls["prompts"][1] and "expire_hours" in calls["prompts"][1]
    assert rec["final_action"] == "pending" and rec["tm_order_tool"]["attempts"] == 2
    o = calls["place_trigger"][0][0][3]
    assert (o["trigger_px"], o["stop_loss"], o["take_profit"], o["condition"], o["expire_hours"]) == (102.0, 105.0, 93.0, "gte", 48.0)


def test_parse_error_is_retried(env):
    m, calls, box = env
    rec = _go(m, box, [ValueError("bad json"), OTC(**PEND)])
    assert "[PARSE]" in calls["prompts"][1] and rec["final_action"] == "pending"


@pytest.mark.parametrize("bad,tag", [
    (dict(stop_loss=99.0), "[STOP_SIDE]"), (dict(stop_loss=115.0), "[STOP_MAX]"), (dict(take_profit=101.0), "[RR]"),
    (dict(side="long", condition="lte", stop_loss=99.0, take_profit=110.0), "[DIRECTION]"),
])
def test_level_guards_rejected_with_range(env, bad, tag):
    m, calls, box = env
    rec = _go(m, box, [OTC(**{**PEND, **bad}), OTC(**PEND)])
    assert tag in calls["prompts"][1] and rec["final_action"] == "pending"


def test_guard_reject_then_retry_success(env):
    m, calls, box = env
    box["guard"] = lambda qty, add: ["[POSITION] 保证金 40% 超过上限 33%"] if qty > 300 else []
    rec = _go(m, box, [OTC(**{**PEND, "margin_pct": 40}), OTC(**{**PEND, "margin_pct": 20})])
    p = calls["prompts"][1]
    assert "[GUARD] [POSITION]" in p and "[RANGE]" in p and "允许 margin_pct" in p
    assert rec["final_action"] == "pending" and rec["tm_order_tool"]["rejected"][0]["call"]["margin_pct"] == 40
    assert calls["place_trigger"][0][0][3]["size_pct"] == pytest.approx(20)


def test_three_failures_no_conclusion(env):
    m, calls, box = env
    box["guard"] = lambda qty, add: ["[DAILY_LOSS] 当日亏损已达 2.5%，停止开仓"]
    rec = _go(m, box, [OTC(**PEND)] * 5)
    assert len(calls["prompts"]) == 3
    assert rec["tm_status"] == "absent" and "3 次调用均未通过" in rec["tm_absent_reason"] and "DAILY_LOSS" in rec["tm_absent_reason"]
    assert not calls["place_trigger"] and not calls["open_now"]
    assert calls["nc"] and any("交易经理无结论" in n for n in rec["notes"])


# ---------- 张数、点位以交易经理为准 ----------
def test_open_margin_pct_not_recomputed_and_levels_not_overridden(env, monkeypatch):
    m, calls, box = env
    monkeypatch.setattr(m, "demo_size", lambda *a, **k: pytest.fail("给了 margin_pct 不得按风险重算"))
    rec = _go(m, box, [OTC(action="open", side="short", entry=100.0, stop_loss=103.0, take_profit=94.0, margin_pct=12, **INV)])
    a = calls["open_now"][0][0]
    assert a[3] == pytest.approx(12) and a[4] == 103.0 and a[5] == 94.0 and a[6] == "short"
    assert rec["aggregate"]["stop_loss"] == 103.0 and rec["aggregate"]["take_profit"] == 94.0


def test_risk_scale_sizing_when_no_margin(env, monkeypatch):
    m, calls, box = env
    seen = []
    monkeypatch.setattr(m, "demo_size", lambda cfg, agg, mk, bk, tm, inst: seen.append(tm.risk_scale) or
                        {"ok": True, "notional": 3000.0, "leverage": 1.0, "margin": 3000.0})
    _go(m, box, [OTC(**{**PEND, "margin_pct": None, "risk_scale": 0.4})])
    assert 0.4 in seen and calls["place_trigger"][0][0][3]["size_pct"] == pytest.approx(3.0)


# ---------- 保留 / 重挂 ----------
def test_existing_pending_must_be_explicit(env):
    m, calls, box = env
    box["ex"] = EX
    rec = _go(m, box, [OTC(action="hold", **INV), OTC(**{**PEND, "action": "keep_pending", "condition": None})])
    assert "keep_pending" in calls["prompts"][1] and rec["final_action"] == "pending"
    assert not calls["place_trigger"] and calls["cancel"] == [] and rec["pending"]["kept_sz"] == "7"


def test_keep_no_position(env):
    m, calls, box = env
    box["ex"] = EX
    rec = _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "take_profit": 92.0, "rationale": "重挂一下"})])
    assert rec["final_action"] == "pending" and not calls["place_trigger"] and calls["cancel"] == []   # 不看文字关键词
    assert calls["amend"] == [(None, 92.0)] and calls["expiry"] == [("OLD", 48)]


def test_keep_with_position(env):
    m, calls, box = env
    box["ex"], box["pos"] = EX, POS
    rec = _go(m, box, [OTC(**{**PEND, "action": "keep_pending"})])
    assert rec["final_action"] == "pending_add" and not calls["place_add_trigger"] and calls["cancel"] == []
    assert rec["pending"]["kept"] == "OLD" and calls["expiry"]


def test_keep_wrong_trigger_rejected(env):
    m, calls, box = env
    box["ex"] = EX
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "trigger_px": 101.0}), OTC(**{**PEND, "action": "keep_pending"})])
    assert "[KEEP]" in calls["prompts"][1]


def test_replace_no_position(env):
    m, calls, box = env
    box["ex"] = EX
    rec = _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "trigger_px": 103.0, "stop_loss": 106.0, "take_profit": 95.0,
                              "rationale": "保留原单"})])
    assert calls["cancel"] == ["交易经理重挂"] and rec["final_action"] == "pending"
    o = calls["place_trigger"][0][0][3]
    assert (o["trigger_px"], o["stop_loss"], o["take_profit"]) == (103.0, 106.0, 95.0)


def test_replace_with_position_uses_tm_levels(env):
    m, calls, box = env
    box["ex"], box["pos"] = EX, POS
    rec = _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "margin_pct": 3})])
    assert calls["cancel"] == ["交易经理重挂"] and rec["final_action"] == "pending_add"
    o = calls["place_add_trigger"][0][4]
    assert (o["trigger_px"], o["stop_loss"], o["take_profit"]) == (102.0, 105.0, 93.0)
    assert calls["check"][-1][4] is not None   # 加仓模式护栏


def test_cancel_pending(env):
    m, calls, box = env
    box["ex"] = EX
    rec = _go(m, box, [OTC(action="cancel_pending", **INV)])
    assert calls["cancel"] == ["交易经理撤单"] and not calls["place_trigger"]


# ---------- 持仓 ----------
def test_add_now_uses_tm_levels(env):
    m, calls, box = env
    box["pos"] = POS
    rec = _go(m, box, [OTC(action="open", side="short", entry=100.0, stop_loss=103.0, take_profit=94.0, margin_pct=2, **INV),
                       OTC(action="add", side="short", entry=100.0, stop_loss=103.0, take_profit=94.0, margin_pct=2, **INV)])
    assert "[STATE]" in calls["prompts"][1]
    a = calls["add_now"][0]
    assert a[5] == 103.0 and a[6] == 94.0 and rec["final_action"] == "add_short"


def test_hold_new_stop_only_tighten(env):
    m, calls, box = env
    box["pos"] = POS
    _go(m, box, [OTC(action="hold", new_stop=106.0, **INV), OTC(action="hold", new_stop=103.0, **INV)])
    assert "[NEW_STOP]" in calls["prompts"][1] and "只能收紧" in calls["prompts"][1]


def test_hold_empty_keeps_levels_no_weighting(env, monkeypatch):
    m, calls, box = env
    box["pos"] = POS
    from desk import sizing
    monkeypatch.setattr(sizing, "drop_self_levels", lambda *a, **k: pytest.fail("工具模式不得加权调点"))
    monkeypatch.setattr(sizing, "wavg_levels", lambda *a, **k: pytest.fail("工具模式不得加权调点"))
    monkeypatch.setattr(m, "add_preview", lambda *a, **k: None)
    rec = _go(m, box, [OTC(action="hold", **INV)])
    assert rec["final_action"] == "hold" and calls["move_stop"] == []


def test_reduce_requires_fraction(env):
    m, calls, box = env
    box["pos"] = POS
    _go(m, box, [OTC(action="reduce", **INV), OTC(action="reduce", reduce_fraction=0.3, **INV)])
    assert "[MISSING]" in calls["prompts"][1] and calls["reduce"] == [0.3]


# ---------- 各入口同一工具 ----------
@pytest.mark.parametrize("source", SOURCES)
def test_all_sources_use_tool(env, source):
    m, calls, box = env
    rec = _go(m, box, [OTC(**PEND)], source=source)
    assert box["schema"] is T.OrderToolCall and "下单工具" in calls["prompts"][0]
    assert rec["tm_order_tool"]["attempts"] == 1 and rec["source"] == source


OLD_FIELDS = ("entry_side", "entry_trigger_px", "entry_condition", "entry_stop_loss", "entry_take_profit", "entry_size_pct",
              "entry_expire_hours", "existing_pending", "entry_reason", "action=pending", "buy/short",
              "ATR+2R", "【立场】行")


def _prompt(tool_mode, ex=None, pos=None):
    cfg = settings.load_config()
    bk = {"equity": 1e5, "initial": 1e5, "cash": 1e5, "exposure": 0.0, "positions": [], "pending_entries": [],
          "triggers": ([{"instId": cfg["coins"][0]["inst_id"], "algoId": "A1", "triggerPx": "102", "side": "sell", "sz": "7"}] if ex else [])}
    return manager._prompt(cfg, cfg["coins"][0], dict(MK), pos, bk, _st("bearish"), manager.weights(cfg), -0.5,
                           {"portfolio_manager": "pm", "trader": "t"}, None, tool_mode=tool_mode)


def test_tool_mode_prompt_drops_old_fields(desk_env):
    """2026-10-31 冒烟：工具模式下提示词不再出现旧 ManagerDecision 字段说明（模型在理由里写 existing_pending=keep），只保留工具说明。"""
    p = _prompt(True, ex=True)
    for k in OLD_FIELDS:
        assert k not in p, k
    assert "## 硬护栏" in p and "你不能翻转多空" in p and "## 当前挂着的条件单" in p
    cfg = settings.load_config()
    cap = min(float(cfg["guardrails"]["max_stop_loss_pct"]), float(cfg["risk"]["stop_max_pct"]) * 100)
    assert f"止损距入场≤{cap:g}%" in p and f"止损距离≤{cap:g}%" in p


def test_legacy_prompt_unchanged_when_off(desk_env):
    p = _prompt(False, ex=True)
    assert "entry_trigger_px" in p and "existing_pending" in p and "## 条件入场（挂单）字段说明" in p and "## 硬护栏" not in p


def test_decide_passes_tool_mode(env):
    m, calls, box = env
    _go(m, box, [OTC(action="hold", **INV)])
    p = calls["prompts"][0]
    p = p if isinstance(p, str) else str(p)
    assert "## 下单工具" in p and "entry_trigger_px" not in p and "existing_pending" not in p


def test_tool_mode_prompt_with_position(desk_env):
    cfg = settings.load_config()
    p = _prompt(True, ex=True, pos=dict(POS, inst_id=cfg["coins"][0]["inst_id"]))
    assert "本币持仓" in p and "不加仓用 action=hold" in p and "risk_scale=0" not in p
    for k in OLD_FIELDS:
        assert k not in p, k
