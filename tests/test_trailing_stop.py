"""移动止损替代固定止盈（2026-11-14 用户批准）：资格判定（validate §3.6）、纯棘轮数学、
demo_watch 棘轮执行（注入 move）、下单不挂止盈（open_now / place_trigger 的附带单与登记）、
限价重挂保留退出方式、RR 复核用计划止盈兜底、止损事件 reason 区分（monitor / demo_ws / 暂存）、
平仓原因记账。全部离线（注入/打桩，不连网、不下单）。"""
import pytest

from desk import demo_exec as DX, demo_trades as DT, entry_limit as EL, okx_demo as X, settings
from desk import regime_judge as RJ
from desk import rr_recheck
from desk import tm_order_tool as T
from desk import trailing_stop as TS

INST = "BTC-USDT-SWAP"
INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}
OTC = T.OrderToolCall
INV = dict(invalidation="判断错了就认", rationale="测试理由")
MK = {"last": 100.0, "close": 100.0, "bar_date": "2026-11-10", "ret_5d": -1.0, "ret_20d": -5.0,
      "atr14": 10.0, "hi_20": 120.0, "lo_20": 95.0}
BK = {"equity": 100000.0, "live_equity": 100000.0, "cash": 100000.0, "exposure": 0.0,
      "positions": [], "triggers": [], "pending_entries": []}
FEAT = {"bars": 260, "ma20": 97.0, "ma20_slope_pct": -0.8, "dev_ma20_pct": -2.9,
        "ma50": 104.0, "ma50_slope_pct": -1.1, "dev_ma50_pct": -3.8,
        "ma200": 130.0, "ma200_slope_pct": -0.4, "dev_ma200_pct": -23.1,
        "rsi14": 38.5, "pos_in_range": 0.2}
TC = {"activation_frac_of_tp": 0.5, "trail_atr_mult": 1.0}


def _cfg(**over):
    cfg = settings.load_config()
    if over:
        cfg["manager"] = {**cfg["manager"], **over}
    return cfg


def _cfg_trail(enabled):
    cfg = _cfg()
    cfg["execution"] = {**cfg.get("execution", {}),
                        "trailing_stop": {"enabled": enabled, "activation_frac_of_tp": 0.5, "trail_atr_mult": 1.0}}
    return cfg


def _judge(**over):
    j = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA50 斜率 -1.20%"], "summary": "空头趋势进行中",
         "exhaustion_signals": []}
    j.update(over)
    if j["stage"] == "late_exhaustion" and "exhaustion_signals" not in over:
        # v2 代码门（regime_judge.apply_stage_gate）：无衰竭证据的 late 会被降为 mid；
        # 测「真 late 不给移动止损」时默认带合法信号，保住原语义。
        j["exhaustion_signals"] = ["structure_break"]
    return j


def _reg(judge="default", cfg=None):
    cfg = cfg or _cfg()
    j = _judge() if judge == "default" else judge
    return RJ.build_regime(cfg, j, dict(FEAT))


# ---------------- 配置 ----------------

def test_ecfg_trailing_defaults_and_config():
    assert DX.ecfg({})["trailing_stop"] == {"enabled": False, "activation_frac_of_tp": 0.5, "trail_atr_mult": 1.0}
    assert DX.ecfg(settings.load_config())["trailing_stop"] == {
        "enabled": True, "activation_frac_of_tp": 0.5, "trail_atr_mult": 1.0}
    assert TS.enabled(settings.load_config()) and not TS.enabled({})


# ---------------- 纯棘轮数学 ----------------

def test_activation_px_both_sides():
    assert TS.activation_px("short", 100.0, 90.0, 0.5) == 95.0
    assert TS.activation_px("long", 100.0, 110.0, 0.5) == 105.0
    assert TS.activation_px("short", 100.0, 90.0, 0.25) == 97.5


def test_step_short_activates_and_trails():
    r = TS.step("short", 100.0, 90.0, 4.0, 103.0, None, 97.0, TC, tick=0.1)
    assert not r["activated"] and r["best_px"] == 97.0 and r["new_stop"] is None      # 未到激活线 95
    r = TS.step("short", 100.0, 90.0, 4.0, 103.0, 97.0, 95.0, TC, tick=0.1)
    assert r["activated"] and r["new_stop"] == 99.0                                    # 95+1.0×ATR4
    r = TS.step("short", 100.0, 90.0, 4.0, 99.0, 95.0, 94.0, TC, tick=0.1)
    assert r["best_px"] == 94.0 and r["new_stop"] == 98.0                              # 跟随最有利价


def test_step_never_loosens_and_min_step():
    r = TS.step("short", 100.0, 90.0, 4.0, 98.0, 94.0, 96.0, TC, tick=0.1)   # 候选 98 == 现止损
    assert r["new_stop"] is None
    r = TS.step("short", 100.0, 90.0, 4.0, 97.9, 94.0, 94.0, TC, tick=0.1)   # 候选 98 比现止损还松
    assert r["new_stop"] is None
    r = TS.step("short", 100.0, 90.0, 4.0, 98.15, 94.0, 94.0, TC, tick=0.1)  # 改善 0.15 < max(tick, 0.05×ATR=0.2)
    assert r["new_stop"] is None


def test_step_long_mirror():
    r = TS.step("long", 100.0, 110.0, 4.0, 97.0, None, 104.0, TC, tick=0.1)
    assert not r["activated"] and r["new_stop"] is None
    r = TS.step("long", 100.0, 110.0, 4.0, 97.0, 104.0, 106.0, TC, tick=0.1)
    assert r["activated"] and r["new_stop"] == 102.0                                   # 106-1.0×ATR4


def test_step_pre_activation_stop_untouched_even_if_trail_would_be_tighter():
    # 价格还没走出激活线，哪怕 best∓ATR 已越过原始止损也不动（激活前止损就是原始止损）
    r = TS.step("short", 100.0, 99.0, 4.0, 103.0, None, 99.6, TC, tick=0.1)   # 激活线 99.5，未到
    assert not r["activated"] and r["new_stop"] is None


# ---------------- validate 资格判定 ----------------

@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])
    cfg = _cfg()

    def make(regime, existing=None, pos=None, cfg_=None):
        return cfg_ or cfg, {"inst": INST, "mk": dict(MK), "bk": dict(BK), "pos": pos,
                             "existing": existing, "S": -0.5, "consensus_side": "short", "trend": regime}
    return make


def _pend(trig=102.0, sl=104.5, tp=98.0, **kw):
    return OTC(action="pending", side="short", trigger_px=trig, condition="gte", stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV, **kw)


def test_validate_marks_trailing_on_trend_pending(vctx):
    cfg, ctx = vctx(_reg())
    assert T.validate(cfg, _pend(), ctx) == []
    assert ctx["_trailing_exit"] == {"plan_tp": 98.0, "atr14": 10.0}
    dec = T.to_decision(_pend(), ctx)
    assert dec._trailing_exit == {"plan_tp": 98.0, "atr14": 10.0}
    assert dec._order_tool["call"]["take_profit"] == 98.0   # 决策记录里计划止盈照常保留（校验口径不变）


def test_validate_marks_trailing_on_trend_open(vctx):
    cfg, ctx = vctx(_reg())
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=95.5, **INV)
    assert T.validate(cfg, call, ctx) == []
    assert ctx["_trailing_exit"] == {"plan_tp": 95.5, "atr14": 10.0}


def test_validate_no_trailing_on_late_exhaustion(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(stage="late_exhaustion")))
    assert T.validate(cfg, _pend(risk_scale=0.5), ctx) == []   # 降级口径下合法，但保持固定止盈
    assert "_trailing_exit" not in ctx


def test_validate_no_trailing_when_judge_failed_or_inactive(vctx):
    cfg, ctx = vctx(_reg(judge=None))
    assert T.validate(cfg, _pend(), ctx) == [] and "_trailing_exit" not in ctx
    cfg, ctx = vctx(_reg(judge=_judge(strength="none")))
    assert T.validate(cfg, _pend(), ctx) == [] and "_trailing_exit" not in ctx


def test_validate_no_trailing_on_direction_mismatch(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(direction="long")))   # 判定多头、订单随共识做空：不生效
    assert T.validate(cfg, _pend(), ctx) == [] and "_trailing_exit" not in ctx


def test_validate_no_trailing_when_switch_off(vctx):
    cfg, ctx = vctx(_reg(), cfg_=_cfg_trail(False))
    assert T.validate(cfg, _pend(), ctx) == [] and "_trailing_exit" not in ctx


def test_validate_no_trailing_for_add_leg(vctx):
    pos = {"side": "short", "contracts": 10, "margin": 500.0, "notional": 5000.0, "avg_px": 100.0}
    cfg, ctx = vctx(_reg(), pos=pos)
    assert T.validate(cfg, _pend(margin_pct=5.0), ctx) == []
    assert "_trailing_exit" not in ctx   # 条件加仓腿不在移动止损范围（保持固定止盈）


def test_instructions_mention_trailing_exit(vctx):
    cfg = _cfg()
    ctx = {"inst": INST, "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(cfg=cfg)}
    s = T.instructions(ctx, cfg)
    assert "改用移动止损" in s and "只用于盈亏比校验与移动止损激活线" in s
    s2 = T.instructions(ctx, _cfg_trail(False))
    assert "改用移动止损" not in s2


# ---------------- demo_watch 棘轮执行（trailing_stop.check） ----------------

def _seed_reg(meta=None, inst=INST):
    m = {"decision_id": "D-T", "side": "short", "exit_mode": "trailing", "plan_tp": 90.0, "atr14_open": 4.0,
         "trail": {"activated": False, "best_px": None, "stop_px": 103.0}}
    if meta:
        m.update(meta)
    DX.save_reg({"triggers": {}, "positions": {inst: m}, "watch": {}})
    return m


def _pos(price, sl=103.0, inst=INST):
    return {"inst_id": inst, "side": "short", "avg_px": 100.0, "sl_px": sl, "price": price,
            "contracts": 10, "pos_side": "short", "legacy": False, "venue": "okx_demo"}


def test_check_activates_and_moves_stop():
    _seed_reg()
    moves = []
    lines = TS.check(_cfg(), positions=[_pos(94.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"),
                     tick_of=lambda i: 0.1)
    assert moves == [98.0] and any("已激活" in ln for ln in lines) and any("跟进" in ln for ln in lines)
    st = DX.reg()["positions"][INST]["trail"]
    assert st["activated"] is True and st["best_px"] == 94.0 and st["stop_px"] == 98.0


def test_check_pre_activation_records_best_only():
    _seed_reg()
    moves = []
    lines = TS.check(_cfg(), positions=[_pos(97.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"),
                     tick_of=lambda i: 0.1)
    assert moves == [] and lines == []
    st = DX.reg()["positions"][INST]["trail"]
    assert st["activated"] is False and st["best_px"] == 97.0 and st["stop_px"] == 103.0


def test_check_ratchet_never_loosens_across_rounds():
    _seed_reg({"trail": {"activated": True, "best_px": 94.0, "stop_px": 98.0}})
    moves = []
    TS.check(_cfg(), positions=[_pos(96.0, sl=98.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"),
             tick_of=lambda i: 0.1)
    assert moves == []   # 价格回撤、最有利价不变 → 候选仍 98，不动


def test_check_halt_and_disabled_do_nothing():
    _seed_reg()
    moves = []
    mv = lambda c, p, ns: moves.append(ns) or (True, "ok")
    assert TS.check(_cfg(), positions=[_pos(90.0)], halt=True, move=mv, tick_of=lambda i: 0.1) == []
    assert TS.check(_cfg_trail(False), positions=[_pos(90.0)], move=mv, tick_of=lambda i: 0.1) == []
    assert moves == []


def test_check_ignores_fixed_positions_and_missing_sl():
    DX.save_reg({"triggers": {}, "positions": {INST: {"decision_id": "D-F", "side": "short"}}, "watch": {}})
    moves = []
    assert TS.check(_cfg(), positions=[_pos(90.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"),
                    tick_of=lambda i: 0.1) == [] and moves == []
    _seed_reg()
    assert TS.check(_cfg(), positions=[_pos(90.0, sl=None)], move=lambda c, p, ns: (True, "ok"),
                    tick_of=lambda i: 0.1) == []   # 止损缺失：只记 trailing_skip 事件，不乱动


def test_check_move_failure_keeps_state():
    _seed_reg()
    lines = TS.check(_cfg(), positions=[_pos(94.0)], move=lambda c, p, ns: (False, " amend 失败"),
                     tick_of=lambda i: 0.1)
    assert any("失败" in ln for ln in lines)
    assert DX.reg()["positions"][INST]["trail"]["stop_px"] == 103.0   # 状态不前进，下轮重试


# ---------------- 下单层：不挂止盈 + 登记 ----------------

def _stub_exec(monkeypatch, posts):
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "check_open", lambda *a, **k: ([], {}))
    monkeypatch.setattr(DX, "cancel_triggers", lambda *a, **k: [])
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "_ps", lambda side: "net")
    monkeypatch.setattr(DX, "_set_lev", lambda *a: None)
    monkeypatch.setattr(X, "instrument", lambda inst: INSTR)
    monkeypatch.setattr(X, "post", lambda p, b: posts.append((p, b)) or {"data": [{"algoId": "Z9", "ordId": "O1"}]})


def test_place_trigger_trailing_no_tp_attach_and_registry(monkeypatch):
    posts = []
    _stub_exec(monkeypatch, posts)
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    o = {"side": "short", "trigger_px": 65000, "size_pct": 10, "leverage": 1, "stop_loss": 68000, "take_profit": None}
    ok, meta = DX.place_trigger(_cfg(), {"equity": 100000, "triggers": []}, INST, o,
                                extra={"exit_mode": "trailing", "plan_tp": 60000, "atr14_open": 900.0})
    assert ok and meta["exit_mode"] == "trailing"
    body = [b for p, b in posts if p == "/api/v5/trade/order-algo"][0]
    att = body["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "68000" and "tpTriggerPx" not in att   # 只挂止损
    e = EL._load()["entries"]["Z9"]                                     # 限价登记保留退出方式（超时重挂靠它）
    assert e["exit_mode"] == "trailing" and e["plan_tp"] == 60000 and e["take_profit"] is None


def test_open_now_trailing_registers_exit_mode(monkeypatch):
    posts = []
    _stub_exec(monkeypatch, posts)
    monkeypatch.setattr(DX, "pending_entry_orders", lambda inst: [])
    monkeypatch.setattr(DX, "clip_to_risk", lambda *a, **k: "10")
    monkeypatch.setattr(X, "contracts", lambda inst, usd: "10")
    ok, inf = DX.open_now(_cfg(), {"equity": 100000, "positions": [], "triggers": []}, INST, 10, 103.0, None,
                          "short", 1, "D-T", 5, 100.0,
                          exit_meta={"exit_mode": "trailing", "plan_tp": 90.0, "atr14_open": 4.0})
    assert ok and inf["exit_mode"] == "trailing"
    body = [b for p, b in posts if p == "/api/v5/trade/order"][0]
    att = body["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "103" and "tpTriggerPx" not in att
    m = DX.reg()["positions"][INST]
    assert m["exit_mode"] == "trailing" and m["plan_tp"] == 90.0 and m["atr14_open"] == 4.0
    assert m["trail"] == {"activated": False, "best_px": None, "stop_px": 103.0}


# ---------------- 限价重挂保留退出方式 ----------------

def test_rearm_preserves_trailing_exit(monkeypatch):
    captured = {}

    def fake_submit(cfg, inst, o, sz, expires, extra=None, keep_lev=False, **kw):
        captured.update(o=o, extra=extra)
        return {"algoId": "NEW1"}
    monkeypatch.setattr(DX, "submit_trigger", fake_submit)
    monkeypatch.setattr(EL, "_guard", lambda cfg, e=None: [])
    monkeypatch.setattr(EL, "_resolve_condition_expires", lambda aid, e: ("gte", "2099-01-01 00:00:00+0800"))
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [{"last": "99"}]})   # 现价在等待侧
    e = {"inst": INST, "side": "short", "pos_side": "short", "trigger_px": 100.0, "stop_loss": 103.0,
         "take_profit": None, "exit_mode": "trailing", "plan_tp": 90.0, "atr14_open": 4.0,
         "decision_id": "D-T", "source": "daily", "lever": 1, "horizon_days": 5}
    assert EL._wait_or_end(_cfg(), "A1", e, 0.0, 5.0) == "rearmed"
    assert captured["o"]["take_profit"] is None                                  # 重挂仍不挂止盈
    assert captured["extra"]["exit_mode"] == "trailing" and captured["extra"]["plan_tp"] == 90.0
    assert captured["extra"]["rearm_of"] == "A1"


# ---------------- RR 复核：计划止盈兜底 ----------------

def _pos_raw(inst="ETH-USDT-SWAP", pos_id="P9", avg="100"):
    return {"posId": pos_id, "cTime": "1700000000000", "instId": inst, "posSide": "short", "pos": "10", "avgPx": avg}


@pytest.fixture()
def rrenv(monkeypatch, tmp_path):
    monkeypatch.setattr(rr_recheck, "PATH", tmp_path / "rr_recheck.json")
    calls = []
    return settings.load_config(), calls, (lambda c, t: calls.append(t) or "已启动")


def test_rr_recheck_trailing_uses_plan_tp(rrenv):
    cfg, calls, schedule = rrenv
    DX.save_reg({"triggers": {}, "positions": {"ETH-USDT-SWAP": {"exit_mode": "trailing", "plan_tp": 90.0}}, "watch": {}})
    rr_recheck.check(cfg, positions=[], schedule=schedule)   # 基线
    rr_recheck.check(cfg, positions=[_pos_raw()], schedule=schedule,
                     sltp=lambda i, p: (103.0, None), planned=lambda i, s, m: {})
    assert not calls                                          # RR=(100-90)/(103-100)=3.33 ≥ 1.5 → ok，不误报缺止盈
    rec = settings.read_json(rr_recheck.PATH, {})["opens"]["P9:1700000000000"]
    assert rec["status"] == "ok" and rec["tp"] == 90.0 and rec["tp_source"] == "plan_trailing"


def test_rr_recheck_trailing_bad_plan_rr_fires_with_plan_tp(rrenv):
    cfg, calls, schedule = rrenv
    DX.save_reg({"triggers": {}, "positions": {"ETH-USDT-SWAP": {"exit_mode": "trailing", "plan_tp": 101.0}}, "watch": {}})
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    rr_recheck.check(cfg, positions=[_pos_raw()], schedule=schedule,
                     sltp=lambda i, p: (103.0, None), planned=lambda i, s, m: {})
    assert len(calls) == 1 and calls[0]["detail"]["tp"] == 101.0   # 事实包里的止盈 = 计划止盈（校验基准）


def test_planned_for_trailing_from_entry_limit(rrenv):
    EL.register("A9", inst="ETH-USDT-SWAP", side="short", pos_side="short", sz="10", order_px="100",
                trigger_px=100, stop_loss=103, take_profit=None, attach_cl="c", lever=1, decision_id="D-T",
                exit_mode="trailing", plan_tp=90.0, atr14_open=4.0)
    out = rr_recheck.planned_for("ETH-USDT-SWAP", "short", {"from_algo": "A9"})
    assert out["planned_tp"] == 90.0 and out["planned_rr"] == pytest.approx(10 / 3)


# ---------------- 止损事件 reason 区分 ----------------

def test_note_and_consume_exit_reason():
    reg_d = {"watch": {}}
    assert TS.note_exit(reg_d, INST, {"exit_mode": "fixed"}, 1_000_000) is False
    assert TS.note_exit(reg_d, INST, {"exit_mode": "trailing", "decision_id": "D-T"}, 1_000_000) is True
    assert TS.consume_exit_reason(reg_d, INST, 1_000_000 - 60_000) == "trailing_stop"   # 成交略早于暂存
    assert TS.consume_exit_reason(reg_d, INST, 1_000_000) is None                        # 已消费
    TS.note_exit(reg_d, INST, {"exit_mode": "trailing"}, 1_000_000)
    assert TS.consume_exit_reason(reg_d, INST, 1_000_000 + TS.STASH_WINDOW_MS + 1) is None   # 太晚：不误标
    TS.prune_stash(reg_d, 2_000_000)
    assert not reg_d["watch"].get("trailing_exits")


def test_position_is_trailing_reads_registry():
    DX.save_reg({"triggers": {}, "positions": {INST: {"exit_mode": "trailing"}}, "watch": {}})
    assert TS.position_is_trailing(INST) is True
    DX.save_reg({"triggers": {}, "positions": {}, "watch": {}})
    assert TS.position_is_trailing(INST) is False


@pytest.fixture()
def mon(monkeypatch, tmp_path):
    from desk import monitor
    monkeypatch.setattr(monitor, "REDISCUSS", tmp_path / "rediscuss.json")
    calls = []
    monkeypatch.setattr(monitor, "SPAWN", lambda args, lf: calls.append(args))
    return settings.load_config(), monitor, calls


def test_monitor_passes_trailing_reason(mon):
    cfg, monitor, calls = mon
    tr = {"inst_id": INST, "trade_id": "DEMO-555", "reason": "trailing_stop"}
    assert monitor.schedule_rediscuss(cfg, tr) == "已启动"
    assert calls and calls[0][calls[0].index("--reason") + 1] == "trailing_stop"   # 不被压回 sl


def test_rediscuss_brief_trailing():
    from desk import rediscuss
    b = rediscuss.reason_brief("trailing_stop", "DEMO-1")
    assert "移动止损触发平仓" in b and "不要当作看错方向的普通止损" in b
    assert rediscuss.REASON_CN["trailing_stop"] == "移动止损"


def test_demo_ws_handle_trailing_reason(tmp_path):
    from desk import demo_ws as W
    DX.save_reg({"triggers": {}, "positions": {INST: {"decision_id": "D-T", "side": "short",
                                                      "exit_mode": "trailing", "plan_tp": 90.0}}, "watch": {}})
    algo = {"arg": {"channel": "orders-algo", "instType": "SWAP"}, "data": [{
        "instType": "SWAP", "instId": INST, "algoId": "777", "ordId": "o777", "ordType": "conditional",
        "state": "effective", "side": "buy", "posSide": "net", "sz": "1", "actualSz": "1",
        "actualPx": "98", "actualSide": "sl", "triggerPx": "98"}]}
    trades = []
    W.handle(W.parse(algo)[0], W.State(tmp_path / "seen.json"), cfg={},
             rediscuss=lambda c, t: trades.append(t) or "已启动", notifier=lambda e: None)
    assert trades and trades[0]["reason"] == "trailing_stop"


def test_demo_ws_handle_plain_sl_reason(tmp_path):
    from desk import demo_ws as W
    DX.save_reg({"triggers": {}, "positions": {}, "watch": {}})
    algo = {"arg": {"channel": "orders-algo", "instType": "SWAP"}, "data": [{
        "instType": "SWAP", "instId": INST, "algoId": "778", "ordId": "o778", "ordType": "conditional",
        "state": "effective", "side": "buy", "posSide": "net", "sz": "1", "actualSz": "1",
        "actualPx": "103", "actualSide": "sl", "triggerPx": "103"}]}
    trades = []
    W.handle(W.parse(algo)[0], W.State(tmp_path / "seen.json"), cfg={},
             rediscuss=lambda c, t: trades.append(t) or "已启动", notifier=lambda e: None)
    assert trades and trades[0]["reason"] == "sl"


# ---------------- 平仓记账 ----------------

def test_exit_reason_labels_trailing_only_when_activated():
    row = {"instId": INST, "type": "2", "closeAvgPx": "95", "uTime": 1700000000000}
    ws = [{"inst_id": INST, "kind": "stop_loss", "time": DT._ts(1700000000000)}]
    assert DT._exit_reason(row, 103.0, None, ws, "trailing") == "移动止损"
    assert DT._exit_reason(row, 103.0, None, ws, None) == "止损"
    ws_tp = [{"inst_id": INST, "kind": "take_profit", "time": DT._ts(1700000000000)}]
    assert DT._exit_reason(row, 103.0, 90.0, ws_tp, "trailing") == "止盈"
