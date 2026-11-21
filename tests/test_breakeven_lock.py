"""保本锁（2026-11-20 用户批准）：资格判定（validate §3.6b）、纯触发数学、demo_watch 检查执行
（注入 move）、下单登记（open_now / place_trigger / 限价重挂保留资格）、R 冻结口径、止损事件
reason 区分（monitor / demo_ws / 暂存）、平仓原因记账。全部离线（注入/打桩，不连网、不下单）。"""
import pytest

from desk import breakeven_lock as BE
from desk import demo_exec as DX, demo_trades as DT, entry_limit as EL, okx_demo as X, settings
from desk import regime_judge as RJ
from desk import tm_order_tool as T

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
BC = {"trigger_r": 1.0}


def _cfg(**over):
    cfg = settings.load_config()
    if over:
        cfg["manager"] = {**cfg["manager"], **over}
    return cfg


def _cfg_be(enabled, trail_enabled=None):
    cfg = _cfg()
    ex = {**cfg.get("execution", {}), "breakeven_lock": {"enabled": enabled, "trigger_r": 1.0}}
    if trail_enabled is not None:
        ex["trailing_stop"] = {"enabled": trail_enabled, "activation_frac_of_tp": 0.5, "trail_atr_mult": 1.0}
    cfg["execution"] = ex
    return cfg


def _judge(**over):
    j = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA50 斜率 -1.20%"], "summary": "空头趋势进行中",
         "exhaustion_signals": []}
    j.update(over)
    if j["stage"] == "late_exhaustion" and "exhaustion_signals" not in over:
        j["exhaustion_signals"] = ["structure_break"]
    return j


def _reg(judge="default", cfg=None):
    cfg = cfg or _cfg()
    j = _judge() if judge == "default" else judge
    return RJ.build_regime(cfg, j, dict(FEAT))


# ---------------- 配置 ----------------

def test_ecfg_breakeven_defaults_and_config():
    assert DX.ecfg({})["breakeven_lock"] == {"enabled": False, "trigger_r": 1.0}
    assert DX.ecfg(settings.load_config())["breakeven_lock"] == {"enabled": True, "trigger_r": 1.0}
    assert BE.enabled(settings.load_config()) and not BE.enabled({})


# ---------------- 纯触发数学 ----------------

def test_trigger_px_both_sides():
    assert BE.trigger_px("short", 100.0, 3.0, 1.0) == 97.0
    assert BE.trigger_px("long", 100.0, 3.0, 1.0) == 103.0
    assert BE.trigger_px("short", 100.0, 3.0, 0.5) == 98.5


def test_step_short_triggers_at_1r():
    r = BE.step("short", 100.0, 3.0, 103.0, 97.5, BC)
    assert not r["triggered"]                       # 只走出 2.5 < R=3
    r = BE.step("short", 100.0, 3.0, 103.0, 97.0, BC)
    assert r["triggered"] and r["lock_px"] == 100.0 and r["need_move"] is True


def test_step_long_mirror():
    r = BE.step("long", 100.0, 3.0, 97.0, 102.9, BC)
    assert not r["triggered"]
    r = BE.step("long", 100.0, 3.0, 97.0, 103.0, BC)
    assert r["triggered"] and r["lock_px"] == 100.0 and r["need_move"] is True


def test_step_already_tighter_no_move():
    # 空头现止损 99 已紧于入场价 100：触发成立但不改单（只进不退）
    r = BE.step("short", 100.0, 3.0, 99.0, 96.0, BC)
    assert r["triggered"] and r["need_move"] is False
    r = BE.step("long", 100.0, 3.0, 101.0, 104.0, BC)
    assert r["triggered"] and r["need_move"] is False


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


def test_validate_marks_breakeven_when_trailing_off(vctx):
    cfg, ctx = vctx(_reg(), cfg_=_cfg_be(True, trail_enabled=False))
    assert T.validate(cfg, _pend(), ctx) == []
    assert "_trailing_exit" not in ctx
    be = ctx["_breakeven_open"]
    assert be["entry_ref"] == 102.0 and be["orig_stop"] == 104.5 and be["r_dist"] == 2.5
    assert be["stage_at_open"] == "mid"
    dec = T.to_decision(_pend(), ctx)
    assert dec._breakeven_open == be


def test_validate_trailing_takes_precedence_mutual_exclusion(vctx):
    cfg, ctx = vctx(_reg(), cfg_=_cfg_be(True, trail_enabled=True))
    assert T.validate(cfg, _pend(), ctx) == []
    assert "_trailing_exit" in ctx and "_breakeven_open" not in ctx   # 互斥：移动止损优先


def test_validate_no_breakeven_on_late_exhaustion(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(stage="late_exhaustion")), cfg_=_cfg_be(True, trail_enabled=False))
    assert T.validate(cfg, _pend(risk_scale=0.5), ctx) == []
    assert "_breakeven_open" not in ctx and "_trailing_exit" not in ctx   # 衰竭仓双排除


def test_validate_marks_breakeven_when_judge_failed(vctx):
    # 判定失败 = 无趋势、固定止盈，且不是末段衰竭 → 照常纳入保本锁
    cfg, ctx = vctx(_reg(judge=None), cfg_=_cfg_be(True, trail_enabled=True))
    assert T.validate(cfg, _pend(), ctx) == []
    assert "_trailing_exit" not in ctx and ctx["_breakeven_open"]["stage_at_open"] is None


def test_validate_no_breakeven_when_switch_off(vctx):
    cfg, ctx = vctx(_reg(), cfg_=_cfg_be(False, trail_enabled=False))
    assert T.validate(cfg, _pend(), ctx) == []
    assert "_breakeven_open" not in ctx


def test_validate_no_breakeven_for_add_leg(vctx):
    pos = {"side": "short", "contracts": 10, "margin": 500.0, "notional": 5000.0, "avg_px": 100.0}
    cfg, ctx = vctx(_reg(), pos=pos, cfg_=_cfg_be(True, trail_enabled=False))
    assert T.validate(cfg, _pend(margin_pct=5.0), ctx) == []
    assert "_breakeven_open" not in ctx   # 条件加仓腿不在范围


def test_validate_breakeven_open_market_uses_entry(vctx):
    cfg, ctx = vctx(_reg(judge=None), cfg_=_cfg_be(True, trail_enabled=False))
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=95.5, **INV)
    assert T.validate(cfg, call, ctx) == []
    assert ctx["_breakeven_open"]["r_dist"] == 2.5 and ctx["_breakeven_open"]["entry_ref"] == 100.0


def test_instructions_mention_breakeven(vctx):
    cfg = _cfg_be(True)
    ctx = {"inst": INST, "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(cfg=cfg)}
    s = T.instructions(ctx, cfg)
    assert "Breakeven lock" in s and "ORIGINAL stop distance" in s
    s2 = T.instructions(ctx, _cfg_be(False))
    assert "Breakeven lock" not in s2


# ---------------- demo_watch 检查执行（breakeven_lock.check） ----------------

def _seed_reg(meta=None, inst=INST):
    m = {"decision_id": "D-B", "side": "short",
         "breakeven": {"eligible": True, "locked": False, "entry_ref": 100.0,
                       "orig_stop": 103.0, "r_dist": 3.0, "stage_at_open": "mid"}}
    if meta:
        m.update(meta)
    DX.save_reg({"triggers": {}, "positions": {inst: m}, "watch": {}})
    return m


def _pos(price, sl=103.0, avg=100.0, inst=INST):
    return {"inst_id": inst, "side": "short", "avg_px": avg, "sl_px": sl, "price": price,
            "contracts": 10, "pos_side": "short", "legacy": False, "venue": "okx_demo"}


def test_check_locks_and_moves_stop_to_entry():
    _seed_reg()
    moves = []
    lines = BE.check(_cfg(), positions=[_pos(96.9)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"))
    assert moves == [100.0] and any("保本锁上锁" in ln for ln in lines)
    b = DX.reg()["positions"][INST]["breakeven"]
    assert b["locked"] is True and b["lock_px"] == 100.0 and b["locked_at"]


def test_check_not_triggered_below_1r():
    _seed_reg()
    moves = []
    lines = BE.check(_cfg(), positions=[_pos(97.5)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"))
    assert moves == [] and lines == []
    assert DX.reg()["positions"][INST]["breakeven"]["locked"] is False


def test_check_r_frozen_ignores_tightened_stop():
    # TM 已把止损从原始 103 收紧到 101：触发仍按冻结 R=3 判定（现价 96.9 走出 3.1 ≥ 3 → 触发），
    # 且上锁目标仍是入场价 100（比现止损 101 更紧，照常改单）。若按现止损重算 R=1 会更早误触发。
    _seed_reg()
    moves = []
    BE.check(_cfg(), positions=[_pos(96.9, sl=101.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"))
    assert moves == [100.0]
    # 反面：现价 98（走出 2.0）——按冻结 R=3 未触发；若按收紧后 R=1 则会误判触发
    _seed_reg()
    moves2 = []
    BE.check(_cfg(), positions=[_pos(98.0, sl=101.0)], move=lambda c, p, ns: moves2.append(ns) or (True, "ok"))
    assert moves2 == []


def test_check_already_tighter_marks_locked_without_move():
    _seed_reg()
    moves = []
    lines = BE.check(_cfg(), positions=[_pos(96.0, sl=99.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok"))
    assert moves == [] and any("已紧于入场价" in ln for ln in lines)
    b = DX.reg()["positions"][INST]["breakeven"]
    assert b["locked"] is True and b.get("already_tighter") is True


def test_check_halt_and_disabled_do_nothing():
    _seed_reg()
    moves = []
    mv = lambda c, p, ns: moves.append(ns) or (True, "ok")
    assert BE.check(_cfg(), positions=[_pos(90.0)], halt=True, move=mv) == []
    assert BE.check(_cfg_be(False), positions=[_pos(90.0)], move=mv) == []
    assert moves == [] and DX.reg()["positions"][INST]["breakeven"]["locked"] is False


def test_check_ignores_trailing_and_unmarked_positions():
    _seed_reg({"exit_mode": "trailing", "plan_tp": 90.0})   # 互斥：移动止损仓不适用
    moves = []
    assert BE.check(_cfg(), positions=[_pos(90.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok")) == []
    assert moves == []
    DX.save_reg({"triggers": {}, "positions": {INST: {"decision_id": "D-F", "side": "short"}}, "watch": {}})
    assert BE.check(_cfg(), positions=[_pos(90.0)], move=lambda c, p, ns: moves.append(ns) or (True, "ok")) == []
    assert moves == []   # 无 breakeven 元数据（衰竭仓/加仓腿/旧仓）一律不动


def test_check_move_failure_alerts_and_retries():
    _seed_reg()
    lines = BE.check(_cfg(), positions=[_pos(96.0)], move=lambda c, p, ns: (False, "amend 失败"))
    assert any("上锁失败" in ln and "告警" in ln for ln in lines)   # 失败必须告警、不得静默
    assert DX.reg()["positions"][INST]["breakeven"]["locked"] is False   # 状态不前进，下轮重试


def test_check_missing_sl_skips():
    _seed_reg()
    assert BE.check(_cfg(), positions=[_pos(90.0, sl=None)], move=lambda c, p, ns: (True, "ok")) == []


# ---------------- 下单层：登记 ----------------

def _stub_exec(monkeypatch, posts):
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "check_open", lambda *a, **k: ([], {}))
    monkeypatch.setattr(DX, "cancel_triggers", lambda *a, **k: [])
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "_ps", lambda side: "net")
    monkeypatch.setattr(DX, "_set_lev", lambda *a: None)
    monkeypatch.setattr(X, "instrument", lambda inst: INSTR)
    monkeypatch.setattr(X, "post", lambda p, b: posts.append((p, b)) or {"data": [{"algoId": "Z9", "ordId": "O1"}]})


_BE_META = {"eligible": True, "locked": False, "entry_ref": 102.0, "orig_stop": 104.5,
            "r_dist": 2.5, "stage_at_open": "mid"}


def test_place_trigger_breakeven_registry_and_limit_entry(monkeypatch):
    posts = []
    _stub_exec(monkeypatch, posts)
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    o = {"side": "short", "trigger_px": 65000, "size_pct": 10, "leverage": 1, "stop_loss": 68000, "take_profit": 60000}
    ok, meta = DX.place_trigger(_cfg(), {"equity": 100000, "triggers": []}, INST, o,
                                extra={"breakeven": dict(_BE_META)})
    assert ok and meta["breakeven"]["r_dist"] == 2.5
    body = [b for p, b in posts if p == "/api/v5/trade/order-algo"][0]
    att = body["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "68000" and att["tpTriggerPx"] == "60000"   # 固定止盈照常挂（与移动止损不同）
    e = EL._load()["entries"]["Z9"]                                          # 限价登记保留资格（超时重挂靠它）
    assert e["breakeven"]["r_dist"] == 2.5 and e["take_profit"] == 60000


def test_open_now_breakeven_registers_meta(monkeypatch):
    posts = []
    _stub_exec(monkeypatch, posts)
    monkeypatch.setattr(DX, "pending_entry_orders", lambda inst: [])
    monkeypatch.setattr(DX, "clip_to_risk", lambda *a, **k: "10")
    monkeypatch.setattr(X, "contracts", lambda inst, usd: "10")
    ok, inf = DX.open_now(_cfg(), {"equity": 100000, "positions": [], "triggers": []}, INST, 10, 103.0, 90.0,
                          "short", 1, "D-B", 5, 100.0,
                          exit_meta={"breakeven": dict(_BE_META)})
    assert ok and inf["breakeven_eligible"] is True
    m = DX.reg()["positions"][INST]
    assert m["breakeven"]["r_dist"] == 2.5 and "trail" not in m   # 不污染移动止损状态
    body = [b for p, b in posts if p == "/api/v5/trade/order"][0]
    att = body["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "103" and att["tpTriggerPx"] == "90"   # 固定止盈照常挂


def test_rearm_preserves_breakeven(monkeypatch):
    captured = {}

    def fake_submit(cfg, inst, o, sz, expires, extra=None, keep_lev=False, **kw):
        captured.update(o=o, extra=extra)
        return {"algoId": "NEW1"}
    monkeypatch.setattr(DX, "submit_trigger", fake_submit)
    monkeypatch.setattr(EL, "_guard", lambda cfg, e=None: [])
    monkeypatch.setattr(EL, "_resolve_condition_expires", lambda aid, e: ("gte", "2099-01-01 00:00:00+0800"))
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [{"last": "99"}]})
    e = {"inst": INST, "side": "short", "pos_side": "short", "trigger_px": 100.0, "stop_loss": 103.0,
         "take_profit": 90.0, "breakeven": dict(_BE_META),
         "decision_id": "D-B", "source": "daily", "lever": 1, "horizon_days": 5}
    assert EL._wait_or_end(_cfg(), "A1", e, 0.0, 5.0) == "rearmed"
    assert captured["extra"]["breakeven"]["r_dist"] == 2.5   # 重挂保留冻结 R


# ---------------- 止损事件 reason 区分 ----------------

def test_note_and_consume_exit_reason():
    reg_d = {"watch": {}}
    assert BE.note_exit(reg_d, INST, {"breakeven": {"locked": False}}, 1_000_000) is False
    meta = {"decision_id": "D-B", "breakeven": {"locked": True, "lock_px": 100.0}}
    assert BE.note_exit(reg_d, INST, meta, 1_000_000) is True
    assert BE.consume_exit_reason(reg_d, INST, 1_000_000, 100.1) == "breakeven_lock"   # 出场价≈入场价
    assert BE.consume_exit_reason(reg_d, INST, 1_000_000, 100.0) is None              # 已消费
    BE.note_exit(reg_d, INST, meta, 1_000_000)
    assert BE.consume_exit_reason(reg_d, INST, 1_000_000, 98.0) is None   # 出场价离入场价 2%：TM 收紧过的止损，不误标
    BE.note_exit(reg_d, INST, meta, 1_000_000)
    assert BE.consume_exit_reason(reg_d, INST, 1_000_000 + BE.STASH_WINDOW_MS + 1, 100.0) is None   # 太晚
    BE.prune_stash(reg_d, 2_000_000)
    assert not reg_d["watch"].get("breakeven_exits")


def test_position_is_locked_reads_registry():
    DX.save_reg({"triggers": {}, "positions": {INST: {"breakeven": {"eligible": True, "locked": True,
                                                                    "lock_px": 100.0}}}, "watch": {}})
    assert BE.position_is_locked(INST) is True and BE.lock_px_of(INST) == 100.0
    DX.save_reg({"triggers": {}, "positions": {}, "watch": {}})
    assert BE.position_is_locked(INST) is False and BE.lock_px_of(INST) is None


@pytest.fixture()
def mon(monkeypatch, tmp_path):
    from desk import monitor
    monkeypatch.setattr(monitor, "REDISCUSS", tmp_path / "rediscuss.json")
    calls = []
    monkeypatch.setattr(monitor, "SPAWN", lambda args, lf: calls.append(args))
    return settings.load_config(), monitor, calls


def test_monitor_passes_breakeven_reason(mon):
    cfg, monitor, calls = mon
    tr = {"inst_id": INST, "trade_id": "DEMO-556", "reason": "breakeven_lock"}
    assert monitor.schedule_rediscuss(cfg, tr) == "已启动"
    assert calls and calls[0][calls[0].index("--reason") + 1] == "breakeven_lock"   # 不被压回 sl


def test_rediscuss_brief_breakeven():
    from desk import rediscuss
    b = rediscuss.reason_brief("breakeven_lock", "DEMO-1")
    assert "保本锁触发平仓" in b and "中性退出" in b
    assert rediscuss.REASON_CN["breakeven_lock"] == "保本锁"


def test_demo_ws_handle_breakeven_reason(tmp_path):
    from desk import demo_ws as W
    DX.save_reg({"triggers": {}, "positions": {INST: {"decision_id": "D-B", "side": "short",
                                                      "breakeven": {"eligible": True, "locked": True,
                                                                    "lock_px": 100.0}}}, "watch": {}})
    algo = {"arg": {"channel": "orders-algo", "instType": "SWAP"}, "data": [{
        "instType": "SWAP", "instId": INST, "algoId": "779", "ordId": "o779", "ordType": "conditional",
        "state": "effective", "side": "buy", "posSide": "net", "sz": "1", "actualSz": "1",
        "actualPx": "100", "actualSide": "sl", "triggerPx": "100"}]}
    trades = []
    W.handle(W.parse(algo)[0], W.State(tmp_path / "seen.json"), cfg={},
             rediscuss=lambda c, t: trades.append(t) or "已启动", notifier=lambda e: None)
    assert trades and trades[0]["reason"] == "breakeven_lock"


def test_demo_ws_handle_tightened_stop_stays_sl(tmp_path):
    # 上锁后 TM 又把止损收到 98，出场价 98 ≠ 入场价 100 → 普通止损，不误标保本
    from desk import demo_ws as W
    DX.save_reg({"triggers": {}, "positions": {INST: {"decision_id": "D-B", "side": "short",
                                                      "breakeven": {"eligible": True, "locked": True,
                                                                    "lock_px": 100.0}}}, "watch": {}})
    algo = {"arg": {"channel": "orders-algo", "instType": "SWAP"}, "data": [{
        "instType": "SWAP", "instId": INST, "algoId": "780", "ordId": "o780", "ordType": "conditional",
        "state": "effective", "side": "buy", "posSide": "net", "sz": "1", "actualSz": "1",
        "actualPx": "98", "actualSide": "sl", "triggerPx": "98"}]}
    trades = []
    W.handle(W.parse(algo)[0], W.State(tmp_path / "seen.json"), cfg={},
             rediscuss=lambda c, t: trades.append(t) or "已启动", notifier=lambda e: None)
    assert trades and trades[0]["reason"] == "sl"


# ---------------- 平仓记账 ----------------

def test_exit_reason_labels_breakeven():
    row = {"instId": INST, "type": "2", "openAvgPx": "100", "closeAvgPx": "100.1", "uTime": 1700000000000}
    ws = [{"inst_id": INST, "kind": "stop_loss", "time": DT._ts(1700000000000)}]
    assert DT._exit_reason(row, 103.0, 90.0, ws, None, breakeven=True) == "保本锁"
    assert DT._exit_reason(row, 103.0, 90.0, ws, None, breakeven=False) == "止损"
    # 无 WS 事件时靠出场价≈入场价 + 已上锁标记同样标注
    assert DT._exit_reason(row, 103.0, 90.0, [], None, breakeven=True) == "保本锁"
    # 已上锁但出场价在原始止损位（先触原始止损的情形不存在于已上锁仓位，仅口径防御）：不冒标
    row2 = {"instId": INST, "type": "2", "openAvgPx": "100", "closeAvgPx": "103", "uTime": 1700000000000}
    assert DT._exit_reason(row2, 103.0, 90.0, [], None, breakeven=True) == "止损"
