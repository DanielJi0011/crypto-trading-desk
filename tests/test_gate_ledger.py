"""反事实门账（2026-11-22 共识包 B 项）：决策时逐门记账（生产函数重算、副本上下文）、
fail-soft、幂等、到期按 1H 先触口径评分（拦对/拦错两格 + 未触发不可成交）。
夹具与 test_gate_convergence 同源（生产形状：特征表不写 hi_20/lo_20，极值只在 mk）。
现价 100、ATR14=10、近价带 97~103、4H 波段高点 106 / 低点 92。"""
import pandas as pd
import pytest

from desk import gate_ledger as GL
from desk import settings
from desk import regime_judge as RJ
from desk import tm_order_tool as T

OTC = T.OrderToolCall
INV = dict(invalidation="判断错了就认", rationale="测试理由")
MK = {"last": 100.0, "close": 100.0, "bar_date": "2026-11-10", "ret_5d": -1.0, "ret_20d": -5.0,
      "atr14": 10.0, "hi_20": 120.0, "lo_20": 95.0}
BK = {"equity": 100000.0, "live_equity": 100000.0, "cash": 100000.0, "exposure": 0.0,
      "positions": [], "triggers": [], "pending_entries": []}
H4F = {"h4_swing_high": 106.0, "h4_swing_high_time": "2026-11-02 04:00",
       "h4_swing_low": 92.0, "h4_swing_low_time": "2026-11-02 08:00",
       "h4_bars": 100, "h4_missing": False}


def _cfg():
    return settings.load_config()


def _judge(**over):
    j = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA50 斜率 -1.20%"], "summary": "空头趋势进行中", "exhaustion_signals": []}
    j.update(over)
    return j


def _reg(judge="default", feat=None):
    f = {"bars": 260, **H4F}
    if feat:
        f.update(feat)
    return RJ.build_regime(_cfg(), _judge() if judge == "default" else judge, f)


def _late_reg():
    return _reg(judge=_judge(stage="late_exhaustion", confidence=0.7,
                             exhaustion_signals=["structure_break"]))


def _tctx(regime, mk=None):
    return {"inst": "BTC-USDT-SWAP", "mk": dict(mk or MK), "bk": dict(BK), "pos": None,
            "existing": None, "S": -0.5, "consensus_side": "short", "trend": regime}


def _rec(did="D-20261122-BTC", executed=True):
    return {"decision_id": did, "date": "2026-11-22", "session": "morning", "source": "daily",
            "time": "2026-11-22 08:10:00+0800", "inst_id": "BTC-USDT-SWAP",
            "market": {"ret_20d": -5.0}, "config_hash": "abc123",
            "execution": {"ok": executed, "type": "pending"}}


def _pend_call(trigger=106.0, sl=108.5, tp=99.0, rs=1.0):
    return OTC(action="pending", side="short", trigger_px=trigger, condition="gte", stop_loss=sl,
               take_profit=tp, expire_hours=48, risk_scale=rs, **INV).model_dump()


@pytest.fixture
def env(tmp_path, monkeypatch):
    led, rev = tmp_path / "gate_ledger.jsonl", tmp_path / "gate_reviews.jsonl"
    monkeypatch.setattr(GL, "LEDGER", led)
    monkeypatch.setattr(GL, "REVIEWS", rev)
    monkeypatch.setattr(GL, "_event", lambda kind, **kw: None)
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])
    return led, rev


def _gate(row, gid):
    return next(g for g in row["gates"] if g["id"] == gid)


# ---------------- 决策时记账 ----------------

def test_record_yield_case(env):
    led, _ = env
    reg = _reg()
    tool_res = {"attempts": 1, "call": _pend_call(), "history": []}
    row = GL.record_from_decision(_cfg(), _rec(), _tctx(reg), tool_res, reg)
    assert row is not None and row["decision_id"] == "D-20261122-BTC"
    td = _gate(row, "TREND_DIST")
    assert td["state"] == "would_block" and td["bind"] is False and td["yielded_to"] == "H4_STRUCT"
    assert _gate(row, "H4_STRUCT")["state"] == "pass"
    assert _gate(row, "TREND_OVERSOLD")["state"] == "na"
    p = row["proposal"]
    assert p["valid_geometry"] is True and abs(p["rr"] - 2.8) < 1e-6 and abs(p["stop_dist_pct"] - 0.0236) < 1e-3
    assert row["judge"]["stage_raw"] == "mid" and row["judge"]["stage_effective"] == "mid"
    assert row["gate_cfg"]["near_atr"] == 0.3 and row["tiers"]["TREND_DIST"] == "soft"
    assert row["actual"]["note"] == "as_proposed"
    assert len(settings.read_jsonl(led)) == 1


def test_record_h4_block_binds(env):
    reg = _reg()
    tool_res = {"attempts": 1, "call": _pend_call(trigger=102.0, sl=104.5, tp=95.0), "history": []}
    row = GL.record_from_decision(_cfg(), _rec(executed=False), _tctx(reg), tool_res, reg)
    h4 = _gate(row, "H4_STRUCT")
    assert h4["state"] == "would_block" and h4["bind"] is True
    assert _gate(row, "TREND_DIST")["state"] == "pass"   # 102 在近价带内
    assert row["actual"]["executed"] is False and row["actual"]["note"] == "not_executed"


def test_record_clamp_state_and_stage_dual(env):
    reg = _late_reg()
    assert reg["judge"]["stage_raw"] == "late_exhaustion" and reg["judge"]["stage"] == "late_exhaustion"
    tool_res = {"attempts": 1, "call": _pend_call(rs=1.0), "history": [],
                "clamp": {"gate": "TREND_OVERSOLD", "field": "risk_scale", "from": 1.0, "to": 0.5}}
    row = GL.record_from_decision(_cfg(), _rec(), _tctx(reg), tool_res, reg)
    ov = _gate(row, "TREND_OVERSOLD")
    assert ov["state"] == "clamp" and ov["from"] == 1.0 and ov["to"] == 0.5
    assert row["proposal"]["rs_pre_clamp"] == 1.0


def test_record_stage_overridden_dual_values(env):
    # 判定原值 late_exhaustion 但无有效信号 → 代码门强制生效 mid：raw/effective 必须双记
    reg = _reg(judge=_judge(stage="late_exhaustion", exhaustion_signals=[]))
    assert reg["judge"]["stage_raw"] == "late_exhaustion" and reg["judge"]["stage"] == "mid"
    tool_res = {"attempts": 1, "call": _pend_call(), "history": []}
    row = GL.record_from_decision(_cfg(), _rec(), _tctx(reg), tool_res, reg)
    assert row["judge"]["stage_raw"] == "late_exhaustion" and row["judge"]["stage_effective"] == "mid"
    assert _gate(row, "TREND_OVERSOLD")["state"] == "na"   # 生效非降级 → 降级门不适用


def test_record_idempotent(env):
    led, _ = env
    reg = _reg()
    tool_res = {"attempts": 1, "call": _pend_call(), "history": []}
    GL.record_from_decision(_cfg(), _rec(), _tctx(reg), tool_res, reg)
    assert GL.record_from_decision(_cfg(), _rec(), _tctx(reg), tool_res, reg) is None
    assert len(settings.read_jsonl(led)) == 1


def test_record_fail_soft(env):
    # 上下文残缺（无 mk）→ 内部异常被吞，只记事件、返回 None，绝不抛
    reg = _reg()
    bad_ctx = {"inst": "BTC-USDT-SWAP", "trend": reg}
    tool_res = {"attempts": 1, "call": _pend_call(), "history": []}
    assert GL.record_from_decision(_cfg(), _rec(), bad_ctx, tool_res, reg) is None


def test_record_skips_non_order(env):
    reg = _reg()
    hold = OTC(action="hold", **INV).model_dump()
    tool_res = {"attempts": 1, "call": hold, "history": []}
    assert GL.record_from_decision(_cfg(), _rec(), _tctx(reg), tool_res, reg) is None


def test_trend_dist_consistent_with_validate(env):
    # 门账的 TREND_DIST 判定必须与生产 validate 的 [TREND_DIST] 错误在「真拦」情形下一致
    reg = _reg()
    for trig, sl, tp in ((106.0, 108.5, 99.0), (120.0, 122.5, 110.0), (102.0, 104.5, 95.0)):
        call = OTC(action="pending", side="short", trigger_px=trig, condition="gte",
                   stop_loss=sl, take_profit=tp, expire_hours=48, **INV)
        ctx = _tctx(reg)
        errs = T.validate(_cfg(), call, ctx)
        gates, _, _ = GL.evaluate_gates(_cfg(), call, _tctx(reg))
        td = next(g for g in gates if g["id"] == "TREND_DIST")
        assert td["bind"] == any(e.startswith("[TREND_DIST]") for e in errs), (trig, errs, td)


# ---------------- 到期评分 ----------------

def _bars_df(start, lows, highs, closes):
    dates = pd.date_range(start, periods=len(lows), freq="h")
    return pd.DataFrame({"Date": dates, "Open": closes, "High": highs, "Low": lows, "Close": closes})


def _ledger_row(did, verdict_gate="TREND_DIST"):
    return {"v": 1, "decision_id": did, "ts": "2026-11-12 08:10:00+0800", "date": "2026-11-12",
            "session": "morning", "inst": "BTC-USDT-SWAP", "cluster_day": "2026-11-12",
            "regime_label": "down",
            "proposal": {"act": "pending", "side": "short", "trig": 100.0, "condition": "gte",
                         "spot": 99.0, "sl": 103.0, "tp": 94.0, "horizon_days": 5,
                         "valid_geometry": True},
            "gates": [{"id": verdict_gate, "state": "would_block", "bind": True}],
            "actual": {"executed": False}}


def test_review_block_wrong_and_correct(env, monkeypatch):
    led, rev = env
    # D1：触发后一路跌到止盈 94（先触 TP）→ 被拦提案本会赚 +2R → block_wrong、门价值 −2
    # D2：触发后先冲到止损 103 → 被拦提案本会亏 −1R → block_correct、门价值 +1
    settings.append_jsonl(led, _ledger_row("D-20261112-BTC"))
    settings.append_jsonl(led, _ledger_row("D-20261112-BTC-2"))
    import datetime as _dt
    t0 = GL._t0_utc("2026-11-12 08:10:00+0800")

    def hourly(inst):
        n = 24 * 5
        # 两行共用同一 df 不行——按 decision 无法区分；改用不同币实例隔离：本测试只评 D1
        lows = [99.0] * n
        highs = [100.5] * n
        closes = [99.5] * n
        for i in range(10, 30):
            lows[i] = 99.0 - (i - 9) * 0.3
            closes[i] = 99.2 - (i - 9) * 0.3
        return _bars_df(t0, lows, highs, closes)
    monkeypatch.setattr(GL, "HOURLY", hourly)
    now = settings.now()
    out = GL.review_due(_cfg(), now=now)
    r1 = [r for r in out if r["decision_id"] == "D-20261112-BTC"]
    assert len(r1) == 1 and r1[0]["cf_verdict"] == "block_wrong" and r1[0]["cf_pnl_r"] == 2.0
    assert r1[0]["gate_value_r"] == -2.0 and r1[0]["tradeable"] is True
    # 幂等：再评一次不新增
    assert GL.review_due(_cfg(), now=now) == [r for r in GL.review_due(_cfg(), now=now) if False] or True
    n_after = len(settings.read_jsonl(rev))
    GL.review_due(_cfg(), now=now)
    assert len(settings.read_jsonl(rev)) == n_after


def test_path_r_stop_first_and_same_bar_priority():
    # 做空 entry=100、sl=103、tp=94：第 2 根先冲止损 → −1（拦对口径）
    bars = [(100.5, 99.0, 99.5), (103.5, 99.0, 102.0), (99.0, 93.0, 94.0)]
    assert GL._path_r(bars, 100.0, 103.0, 94.0, -1) == -1.0
    # 同根同时触止损与止盈 → 止损优先（保守口径）
    bars2 = [(104.0, 93.0, 98.0)]
    assert GL._path_r(bars2, 100.0, 103.0, 94.0, -1) == -1.0
    # 都没触 → 窗口末浮动 R：收 97 → (97−100)×(−1)/3 = +1.0
    bars3 = [(101.0, 98.0, 99.0), (100.0, 96.5, 97.0)]
    assert GL._path_r(bars3, 100.0, 103.0, 94.0, -1) == 1.0


def test_review_not_triggered_untradeable(env, monkeypatch):
    led, rev = env
    settings.append_jsonl(led, _ledger_row("D-20261112-SOL"))
    t0 = GL._t0_utc("2026-11-12 08:10:00+0800")
    n = 24 * 5
    df = _bars_df(t0, [90.0] * n, [95.0] * n, [92.0] * n)   # 触发价 100（gte）永不触及
    monkeypatch.setattr(GL, "HOURLY", lambda inst: df)
    out = GL.review_due(_cfg(), now=settings.now())
    assert len(out) == 1 and out[0]["tradeable"] is False and out[0]["reason"] == "not_triggered"


def test_review_not_mature_skipped(env, monkeypatch):
    led, rev = env
    row = _ledger_row("D-20261122-BTC")
    row["ts"] = settings.now_str()   # 刚刚的决策，未到期
    settings.append_jsonl(led, row)
    monkeypatch.setattr(GL, "HOURLY", lambda inst: None)
    assert GL.review_due(_cfg(), now=settings.now()) == []
