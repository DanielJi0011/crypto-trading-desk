"""退出模式切换（2026-11-24 用户批准）：交易经理在复盘时自主裁量把已有核心仓的退出方式在
固定止盈止损 ↔ 移动止损之间原地切换。覆盖：工具校验（两方向、RR 拒绝、只收紧口径、HALT、
同模式、错动作、计划止盈侧向、机制未启用）、执行层换单（FakeOKX 全链路：先挂新再撤旧、
止损原值不动、登记形状、事件留痕、失败保留旧单）、切换后棘轮接管、manager 执行层守卫、
日报渲染（决策备注 + 持仓行模式）。夹具形状照生产生产者：mk= manager.market_ctx、
pos = demo_exec.book 的持仓、登记 = open_now / demo_watch 落盘的 positions[inst]。全部离线。"""
import pytest

from desk import demo_exec as DX, manager, okx_demo as X, settings
from desk import tm_order_tool as T
from desk import trailing_stop as TS
from tests.fake_okx import FakeOKX

INST = "BTC-USDT-SWAP"
OTC = T.OrderToolCall
INV = dict(invalidation="判断错了就认", rationale="测试理由")
# manager.market_ctx 的真实键集
MK = {"last": 100.0, "close": 100.0, "bar_date": "2026-11-20", "ret_5d": -1.0, "ret_20d": -5.0,
      "atr14": 4.0, "hi_20": 120.0, "lo_20": 95.0}
# demo_exec.book 的真实键集
BK = {"cash": 90000.0, "equity": 100000.0, "live_equity": 100000.0, "initial": 100000.0,
      "positions": [], "exposure": 0.0, "margin": 0.0, "triggers": [], "equity_basis": "initial",
      "pending_entries": []}


def _cfg(**exec_over):
    cfg = settings.load_config()
    if exec_over:
        cfg["execution"] = {**cfg.get("execution", {}), **exec_over}
    return cfg


def _pos(**over):
    """demo_exec.book() 的持仓形状（空头核心仓，均价 100、止损 103、止盈 90）。"""
    p = {"trade_id": f"DEMO-{INST}-short", "inst_id": INST, "ticker": "BTC", "side": "short",
         "leverage": 1.0, "margin": 10.0, "notional": 10.0, "liq_px": None, "funding": 0.0,
         "qty": 0.1, "contracts": 10, "pos_side": "short", "avg_px": 100.0, "price": 100.0,
         "value": 10.0, "sl_px": 103.0, "tp_px": 90.0, "upnl": 0.0,
         "entry_time": "2026-11-20 08:05:00+0800", "deadline": "2026-11-29 08:05:00+0800",
         "venue": "okx_demo"}
    p.update(over)
    return p


def _seed_fixed(breakeven=False):
    """open_now 落盘的固定止盈仓位登记形状。"""
    m = {"decision_id": "D-20261120-BTC", "side": "short", "opened": "2026-11-20 08:05:00+0800",
         "child_ordId": "O1", "deadline": "2026-11-29 08:05:00+0800"}
    if breakeven:
        m["breakeven"] = {"eligible": True, "locked": False, "entry_ref": 100.0, "orig_stop": 103.0,
                          "r_dist": 3.0, "stage_at_open": "mid"}
    DX.save_reg({"triggers": {}, "positions": {INST: m}, "watch": {}})
    return m


def _seed_trailing():
    """open_now 落盘的移动止损仓位登记形状（已激活、棘轮止损已跟到 98）。"""
    m = {"decision_id": "D-20261120-BTC", "side": "short", "opened": "2026-11-20 08:05:00+0800",
         "child_ordId": "O1", "deadline": "2026-11-29 08:05:00+0800",
         "exit_mode": "trailing", "plan_tp": 90.0, "atr14_open": 4.0,
         "trail": {"activated": True, "best_px": 94.0, "stop_px": 98.0}}
    DX.save_reg({"triggers": {}, "positions": {INST: m}, "watch": {}})
    return m


def _events(name="exit_mode_switched"):
    # demo_events.jsonl 在同文件用例间累积（与 test_b2 同口径）：断言一律用增量/末条，不数总量
    return [e for e in settings.read_jsonl(X.EVENTS) if e.get("event") == name]


@pytest.fixture(autouse=True)
def _clean_registry():
    """登记文件在整个测试会话间共享：本文件的切换/棘轮用例会把 BTC 登记留成 trailing，
    若不清理会污染后面文件靠登记判退出原因的用例（如 test_tp_rediscuss 的 WS 止损冷却）。"""
    yield
    DX.save_reg({"triggers": {}, "positions": {}, "watch": {}})


# ---------------- validate 校验 ----------------

@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])
    cfg = _cfg()

    def make(pos, mk=None, cfg_=None):
        return cfg_ or cfg, {"inst": INST, "mk": dict(MK, **(mk or {})), "bk": dict(BK), "pos": pos,
                             "existing": None, "S": -0.5, "consensus_side": "short", "trend": None}
    return make


def _hold(**kw):
    return OTC(action="hold", **INV, **kw)


def test_validate_fixed_to_trailing_ok(vctx):
    _seed_fixed()
    cfg, ctx = vctx(_pos())
    call = _hold(exit_mode_switch="trailing", switch_take_profit=88.0)
    assert T.validate(cfg, call, ctx) == []
    dec = T.to_decision(call, ctx)
    assert dec.exit_mode_switch == "trailing" and dec.switch_take_profit == 88.0


def test_validate_fixed_to_trailing_default_plan_ok(vctx):
    _seed_fixed()
    cfg, ctx = vctx(_pos())   # 不给 switch_take_profit：沿用原固定止盈 90 作计划止盈
    assert T.validate(cfg, _hold(exit_mode_switch="trailing"), ctx) == []


def test_validate_trailing_to_fixed_ok(vctx):
    _seed_trailing()
    cfg, ctx = vctx(_pos(sl_px=98.0, tp_px=None), mk={"last": 94.0})
    # RR = |85-94| / |94-98| = 2.25 ≥ 1.5
    assert T.validate(cfg, _hold(exit_mode_switch="fixed", switch_take_profit=85.0), ctx) == []


def test_validate_trailing_to_fixed_rr_rejected(vctx):
    _seed_trailing()
    cfg, ctx = vctx(_pos(sl_px=98.0, tp_px=None), mk={"last": 94.0})
    errs = T.validate(cfg, _hold(exit_mode_switch="fixed", switch_take_profit=90.0), ctx)
    assert any(e.startswith("[EXIT_SWITCH]") and "盈亏比" in e and "1.00" in e for e in errs), errs


def test_validate_trailing_to_fixed_tp_required(vctx):
    _seed_trailing()
    cfg, ctx = vctx(_pos(sl_px=98.0, tp_px=None), mk={"last": 94.0})
    errs = T.validate(cfg, _hold(exit_mode_switch="fixed"), ctx)
    assert any("必须给 switch_take_profit" in e for e in errs), errs


def test_validate_same_mode_rejected(vctx):
    _seed_fixed()
    cfg, ctx = vctx(_pos())
    errs = T.validate(cfg, _hold(exit_mode_switch="fixed", switch_take_profit=85.0), ctx)
    assert any("已是固定止盈止损" in e for e in errs), errs
    _seed_trailing()
    cfg, ctx = vctx(_pos(sl_px=98.0, tp_px=None), mk={"last": 94.0})
    errs = T.validate(cfg, _hold(exit_mode_switch="trailing"), ctx)
    assert any("已是移动止损" in e for e in errs), errs


def test_validate_switch_wrong_action_rejected(vctx):
    _seed_fixed()
    cfg, ctx = vctx(None)
    call = OTC(action="open", side="short", entry=100.0, stop_loss=103.0, take_profit=90.0,
               exit_mode_switch="trailing", **INV)
    errs = T.validate(cfg, call, ctx)
    assert any(e.startswith("[EXIT_SWITCH]") and "hold/reduce" in e for e in errs), errs


def test_validate_switch_halt_blocked(vctx, monkeypatch):
    _seed_fixed()
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    cfg, ctx = vctx(_pos())
    errs = T.validate(cfg, _hold(exit_mode_switch="trailing", switch_take_profit=88.0), ctx)
    assert any("HALT/KILL" in e for e in errs), errs


def test_validate_switch_trailing_disabled_rejected(vctx):
    _seed_fixed()
    cfg, ctx = vctx(_pos(), cfg_=_cfg(trailing_stop={"enabled": False, "activation_frac_of_tp": 0.5,
                                                      "trail_atr_mult": 1.0}))
    errs = T.validate(cfg, _hold(exit_mode_switch="trailing", switch_take_profit=88.0), ctx)
    assert any("移动止损机制未启用" in e for e in errs), errs


def test_validate_switch_with_new_tp_rejected(vctx):
    _seed_fixed()
    cfg, ctx = vctx(_pos())
    errs = T.validate(cfg, _hold(exit_mode_switch="trailing", switch_take_profit=88.0, new_tp=85.0), ctx)
    assert any("不要同时给 new_tp" in e for e in errs), errs


def test_validate_switch_plan_wrong_side_rejected(vctx):
    _seed_fixed()
    cfg, ctx = vctx(_pos())   # 空头：计划止盈 105 在现价 100 上方（亏损一侧）
    errs = T.validate(cfg, _hold(exit_mode_switch="trailing", switch_take_profit=105.0), ctx)
    assert any("计划止盈" in e and "盈利一侧" in e for e in errs), errs


def test_validate_no_switch_unaffected(vctx):
    _seed_trailing()
    cfg, ctx = vctx(_pos(sl_px=98.0, tp_px=None), mk={"last": 94.0})
    assert T.validate(cfg, _hold(new_stop=97.0), ctx) == []   # 无切换字段：原调点路径不变


def test_instructions_mention_switch(vctx):
    _seed_fixed()
    cfg = _cfg()
    ctx = {"inst": INST, "mk": dict(MK), "bk": dict(BK), "pos": _pos(), "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": None}
    s = T.instructions(ctx, cfg)
    assert "exit mode is currently FIXED" in s and "exit_mode_switch" in s and "your own judgment call" in s
    _seed_trailing()
    s2 = T.instructions(ctx, cfg)
    assert "exit mode is currently TRAILING" in s2


# ---------------- 执行层换单（FakeOKX 全链路） ----------------

def _fake_with_algo(monkeypatch, algo):
    f = FakeOKX().install(monkeypatch)
    f.algos.append(algo)
    return f


FIXED_ALGO = {"algoId": "OLD", "instId": INST, "ordType": "oco", "posSide": "short", "side": "buy",
              "sz": "10", "slTriggerPx": "103.00", "tpTriggerPx": "90.00", "reduceOnly": True}
TRAIL_ALGO = {"algoId": "OLD", "instId": INST, "ordType": "conditional", "posSide": "short", "side": "buy",
              "sz": "10", "slTriggerPx": "98.00", "reduceOnly": True}


def test_switch_fixed_to_trailing_end_to_end(monkeypatch):
    _seed_fixed(breakeven=True)
    f = _fake_with_algo(monkeypatch, dict(FIXED_ALGO))
    ok, msg = DX.switch_exit_mode(_cfg(), _pos(), "trailing", take_profit=88.0, atr14_open=4.0,
                                  decision_id="D-SW", reason="趋势延续，改移动止损跟随")
    assert ok, msg
    assert len(f.algos) == 1
    new = f.algos[0]
    assert new["ordType"] == "conditional" and new["slTriggerPx"] == "103.00" and "tpTriggerPx" not in new
    assert new["sz"] == "10" and new["reduceOnly"] is True
    m = DX.reg()["positions"][INST]
    assert m["exit_mode"] == "trailing" and m["plan_tp"] == 88.0 and m["atr14_open"] == 4.0
    assert m["trail"] == {"activated": False, "best_px": None, "stop_px": 103.0}
    assert "breakeven" not in m and m["deadline"] == "2026-11-29 08:05:00+0800"   # 保本锁元数据摘除、到期保留
    ev = _events()[-1]
    assert ev["from_mode"] == "fixed" and ev["to_mode"] == "trailing"
    assert ev["stop"] == 103.0 and ev["plan_tp"] == 88.0 and ev["decision_id"] == "D-SW"
    assert ev["breakeven_dropped"] is True and "移动止损" in msg and "止损保持 103" in msg


def test_switch_fixed_to_trailing_default_plan(monkeypatch):
    _seed_fixed()
    _fake_with_algo(monkeypatch, dict(FIXED_ALGO))
    ok, _ = DX.switch_exit_mode(_cfg(), _pos(), "trailing", take_profit=None, atr14_open=4.0, decision_id="D-SW")
    assert ok and DX.reg()["positions"][INST]["plan_tp"] == 90.0   # 原固定止盈转作计划止盈


def test_switch_trailing_to_fixed_end_to_end(monkeypatch):
    _seed_trailing()
    f = _fake_with_algo(monkeypatch, dict(TRAIL_ALGO))
    pos = _pos(sl_px=98.0, tp_px=None)
    ok, msg = DX.switch_exit_mode(_cfg(), pos, "fixed", take_profit=85.0, decision_id="D-SW", reason="衰竭迹象，锁定目标")
    assert ok, msg
    assert len(f.algos) == 1
    new = f.algos[0]
    assert new["ordType"] == "oco" and new["slTriggerPx"] == "98.00" and new["tpTriggerPx"] == "85.00"
    m = DX.reg()["positions"][INST]
    assert "exit_mode" not in m and "plan_tp" not in m and "atr14_open" not in m and "trail" not in m
    assert m["deadline"] == "2026-11-29 08:05:00+0800" and m["decision_id"] == "D-20261120-BTC"
    ev = _events()[-1]
    assert ev["from_mode"] == "trailing" and ev["to_mode"] == "fixed"
    assert ev["stop"] == 98.0 and ev["tp"] == 85.0 and "棘轮位 98" in msg


def test_switch_same_mode_no_exchange_writes(monkeypatch):
    _seed_fixed()
    f = _fake_with_algo(monkeypatch, dict(FIXED_ALGO))
    n0 = len(_events())
    ok, _ = DX.switch_exit_mode(_cfg(), _pos(), "fixed", take_profit=85.0)
    assert not ok and not any(c[0] == "POST" for c in f.calls) and len(_events()) == n0


def test_switch_without_stop_algo_aborts(monkeypatch):
    _seed_fixed()
    FakeOKX().install(monkeypatch)   # 交易所无任何保护单
    n0 = len(_events())
    ok, msg = DX.switch_exit_mode(_cfg(), _pos(), "trailing", take_profit=88.0, atr14_open=4.0)
    assert not ok and "无止损单" in msg
    assert "exit_mode" not in DX.reg()["positions"][INST] and len(_events()) == n0


def test_switch_place_failure_keeps_old_algo_and_registry(monkeypatch):
    _seed_fixed()
    f = _fake_with_algo(monkeypatch, dict(FIXED_ALGO))
    f.fail["/api/v5/trade/order-algo"] = X.DemoError("boom")
    monkeypatch.setattr("desk.demo_ws.notify", lambda ev: True)
    n0 = len(_events())
    ok, _ = DX.switch_exit_mode(_cfg(), _pos(), "trailing", take_profit=88.0, atr14_open=4.0)
    assert not ok and [a["algoId"] for a in f.algos] == ["OLD"]
    assert "exit_mode" not in DX.reg()["positions"][INST] and len(_events()) == n0


# ---------------- 切换后棘轮接管 ----------------

def test_ratchet_takes_over_after_switch(monkeypatch):
    _seed_fixed()
    _fake_with_algo(monkeypatch, dict(FIXED_ALGO))
    ok, _ = DX.switch_exit_mode(_cfg(), _pos(), "trailing", take_profit=88.0, atr14_open=4.0, decision_id="D-SW")
    assert ok
    moves = []
    mv = lambda c, p, ns: moves.append(ns) or (True, "ok")
    # 激活线 = 100 + (88-100)×0.5 = 94；现价 95 未越线：只记最有利价，止损不动
    TS.check(_cfg(), positions=[_pos(price=95.0)], move=mv, tick_of=lambda i: 0.1)
    assert moves == [] and DX.reg()["positions"][INST]["trail"]["best_px"] == 95.0
    # 现价 93 越线激活：候选 = 93 + 1.0×ATR4 = 97，比现止损 103 更紧 → 跟进
    lines = TS.check(_cfg(), positions=[_pos(price=93.0)], move=mv, tick_of=lambda i: 0.1)
    assert moves == [97.0] and any("已激活" in ln for ln in lines)
    st = DX.reg()["positions"][INST]["trail"]
    assert st["activated"] is True and st["stop_px"] == 97.0


def test_ratchet_after_switch_never_loosens_when_already_past_activation(monkeypatch):
    _seed_fixed()
    _fake_with_algo(monkeypatch, dict(FIXED_ALGO))
    # 切换时价格已深过激活线（计划止盈 70 → 激活线 85，现价已到 80）：止损 103、ATR 4
    ok, _ = DX.switch_exit_mode(_cfg(), _pos(), "trailing", take_profit=70.0, atr14_open=4.0, decision_id="D-SW")
    assert ok
    moves = []
    TS.check(_cfg(), positions=[_pos(price=80.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"),
             tick_of=lambda i: 0.1)
    assert moves == [84.0]   # 候选 80+4=84：立即激活并收紧到 84，绝不高于（放松于）切换时的止损逻辑之外


# ---------------- manager 执行层守卫 ----------------

def _dec(**kw):
    base = dict(action="hold", rationale="趋势延续", exit_mode_switch="trailing", switch_take_profit=88.0)
    base.update(kw)
    return manager.ManagerDecision(**base)


def test_apply_switch_executes_and_notes(monkeypatch):
    seen = {}
    monkeypatch.setattr(DX, "switch_exit_mode",
                        lambda cfg, p, sw, **kw: seen.update(sw=sw, **kw) or (True, "退出模式切换：固定止盈止损 → 移动止损"))
    notes = []
    manager.apply_exit_mode_switch(_cfg(), _pos(), _dec(), "hold", None, notes, dict(MK), "D-SW")
    assert seen["sw"] == "trailing" and seen["take_profit"] == 88.0 and seen["atr14_open"] == 4.0
    assert seen["decision_id"] == "D-SW" and any("退出模式切换" in n for n in notes)


def test_apply_switch_guards(monkeypatch):
    calls = []
    monkeypatch.setattr(DX, "switch_exit_mode", lambda *a, **k: calls.append(a) or (True, "x"))
    cfg = _cfg()
    n = []
    manager.apply_exit_mode_switch(cfg, _pos(), _dec(), "hold", None, n, dict(MK), "D", tm_absent=True)
    manager.apply_exit_mode_switch(cfg, _pos(legacy=True), _dec(), "hold", None, n, dict(MK), "D")
    manager.apply_exit_mode_switch(cfg, _pos(venue="paper"), _dec(), "hold", None, n, dict(MK), "D")
    manager.apply_exit_mode_switch(cfg, _pos(), _dec(), "reduce", {"ok": False}, n, dict(MK), "D")
    assert calls == [] and any("减仓未成功" in x for x in n)
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    n2 = []
    manager.apply_exit_mode_switch(cfg, _pos(), _dec(), "hold", None, n2, dict(MK), "D")
    assert calls == [] and any("HALT/KILL" in x for x in n2)


def test_apply_switch_after_successful_reduce_uses_remaining(monkeypatch):
    seen = {}
    monkeypatch.setattr(DX, "switch_exit_mode", lambda cfg, p, sw, **kw: seen.update(p=p) or (True, "ok"))
    manager.apply_exit_mode_switch(_cfg(), _pos(), _dec(), "reduce", {"ok": True, "remaining": 6.0}, [], dict(MK), "D")
    assert seen["p"]["contracts"] == 6.0


# ---------------- 日报渲染 ----------------

def test_report_shows_switch_note_and_position_mode(monkeypatch):
    import json, os
    from desk import report
    decs = json.load(open(os.path.join(os.path.dirname(__file__), "data", "decisions_20261031_evening.json"),
                          encoding="utf-8"))
    d0 = decs[0]
    inst = d0["inst_id"]
    d0["notes"] = list(d0.get("notes") or []) + [
        "退出模式切换：固定止盈止损 → 移动止损（止损保持 103 不动，固定止盈已摘除；计划止盈 88 仅作激活线基准，ATR14 4，棘轮自当前止损起步）"]
    pos = _pos(inst_id=inst, trade_id=f"DEMO-{inst}-short")
    bk = {**BK, "positions": [pos]}
    reg = {"triggers": {}, "positions": {inst: {"decision_id": "D-X", "side": "short", "exit_mode": "trailing",
                                                "plan_tp": 88.0, "atr14_open": 4.0,
                                                "trail": {"activated": False, "best_px": None, "stop_px": 103.0}}},
           "watch": {}}
    monkeypatch.setattr(DX, "is_demo", lambda cfg: True)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)
    monkeypatch.setattr(DX, "reg", lambda: reg)
    usage = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0,
             "by_stage": {}, "by_model": {}, "by_provider": {}}
    p = report.write(_cfg(), "2099-01-02", decs, [], [], [], [], usage, {}, [], name="t-exit-switch")
    txt = open(p, encoding="utf-8").read()
    assert "退出模式切换：固定止盈止损 → 移动止损" in txt          # 决策段备注
    assert "移动止损[TRAILING]（未激活）" in txt                    # 持仓行模式已变
