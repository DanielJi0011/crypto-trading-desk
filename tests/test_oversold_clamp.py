"""[TREND_OVERSOLD] reject 改 clamp（2026-11-22 共识包 C 项，用户批准）：
验证失败**仅**由末段降级 risk_scale 上限引起时，代码把 risk_scale 夹到上限（0.5）放行，
回执与 _gate_notes 记 clamp（from/to），不再打回重试；同时有任何其他失败 → 照旧拒绝。
上限数值本身不变（manager.trend_oversold_risk_scale）。夹具与 test_gate_convergence 同源（生产形状）。
现价 100、ATR14=10、近价带 97~103；降级判定 = short/strong/late_exhaustion + 有效衰竭信号。"""
import pytest

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


def _late_regime(cfg=None):
    cfg = cfg or _cfg()
    j = {"direction": "short", "strength": "strong", "stage": "late_exhaustion", "confidence": 0.7,
         "evidence": ["结构破坏"], "summary": "末段衰竭", "exhaustion_signals": ["structure_break"]}
    reg = RJ.build_regime(cfg, j, {"bars": 260, **H4F})
    assert reg["downgrade"] is True and reg["active"] is True
    return reg


@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])

    def make(regime):
        return _cfg(), {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None,
                        "existing": None, "S": -0.5, "consensus_side": "short", "trend": regime}
    return make


def _pend(rs=1.0, trigger=102.0, sl=104.5, tp=95.0):
    # 触发价 102 在近价带（97~103）内、≥ 波段高点？否（102<106）→ 会另有 [H4_STRUCT]；
    # 纯 clamp 用例用 trigger=106 恰在波段高点（H4 让位放行），见下方各用例的说明。
    return OTC(action="pending", side="short", trigger_px=trigger, condition="gte", stop_loss=sl,
               take_profit=tp, expire_hours=48, risk_scale=rs, **INV)


class _Model:
    """假模型：每次 invoke 返回同一个预设 call（dict 形态，与 run() 的解析路径一致）。"""
    def __init__(self, call):
        self._call = call
        self.n = 0

    def invoke(self, prompt):
        self.n += 1
        return dict(self._call)


def _run(cfg, ctx, call):
    return T.run(cfg, _Model(call.model_dump()), "prompt", ctx)


# ---------------- try_clamp_oversold 单元 ----------------

def test_validate_still_rejects_rs_over_cap(vctx):
    # validate 本身不变：rs=1.0 在降级下仍被 [TREND_OVERSOLD] 拒（clamp 发生在 run 的放行环节）
    cfg, ctx = vctx(_late_regime())
    errs = T.validate(cfg, _pend(rs=1.0, trigger=106.0, sl=108.5, tp=99.0), ctx)
    assert len(errs) == 1 and T._is_oversold_rs_err(errs[0]), errs


def test_try_clamp_only_for_pure_rs_failure(vctx):
    cfg, ctx = vctx(_late_regime())
    call = _pend(rs=0.9, trigger=106.0, sl=108.5, tp=99.0)
    errs = T.validate(cfg, call, ctx)
    clamped = T.try_clamp_oversold(call, errs, ctx)
    assert clamped is not None and clamped.risk_scale == 0.5
    # 混合失败（rs 超限 + 触发价远超近价带与波段位）→ 不夹
    call2 = _pend(rs=0.9, trigger=120.0, sl=122.5, tp=110.0)
    errs2 = T.validate(cfg, call2, ctx)
    assert len(errs2) >= 1 and T.try_clamp_oversold(call2, errs2, ctx) is None


def test_try_clamp_none_when_rs_within_cap(vctx):
    cfg, ctx = vctx(_late_regime())
    call = _pend(rs=0.5, trigger=106.0, sl=108.5, tp=99.0)
    assert T.validate(cfg, call, ctx) == []
    assert T.try_clamp_oversold(call, [], ctx) is None


# ---------------- run() 放行路径 ----------------

def test_run_clamps_and_passes(vctx):
    cfg, ctx = vctx(_late_regime())
    call = _pend(rs=1.0, trigger=106.0, sl=108.5, tp=99.0)
    res = _run(cfg, ctx, call)
    assert res["dec"] is not None and res["attempts"] == 1, res.get("absent_why")
    assert res["clamp"] == {"gate": "TREND_OVERSOLD", "field": "risk_scale", "from": 1.0, "to": 0.5}
    assert res["call"]["risk_scale"] == 0.5
    notes = getattr(res["dec"], "_gate_notes", []) or []
    assert any("CLAMP" in n and "1" in n and "0.5" in n for n in notes), notes


def test_run_mixed_failure_still_rejected(vctx):
    # rs 超限 + RR 不足（止盈太近）→ 混合失败：3 次都拒、无结论、clamp 不出现
    cfg, ctx = vctx(_late_regime())
    call = _pend(rs=1.0, trigger=106.0, sl=108.5, tp=104.0)   # RR = 2/2.5 = 0.8 < 1.5
    res = _run(cfg, ctx, call)
    assert res["dec"] is None and res["attempts"] == T.TOOL_MAX_ATTEMPTS
    assert "clamp" not in res or res.get("clamp") is None


def test_run_open_ban_not_clamped(vctx):
    # 降级禁用现价 open：那条 [TREND_OVERSOLD] 不是 rs 条，rs 再小也救不了 → 照旧拒绝
    cfg, ctx = vctx(_late_regime())
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=96.0,
               risk_scale=0.4, **INV)
    res = _run(cfg, ctx, call)
    assert res["dec"] is None


def test_clamped_sizing_uses_cap():
    # 夹后仓位计算（真实数字验算，与 instructions 的心算口径一致）：
    # 权益 108,061.73（2026-11-22 早场实际权益）、SOL 2026-11-21 复议实况：入场 67.15、止损 72.3
    # → 止损距离 7.67%；共识强度缩放与回撤缩放此处以 demo_size 的生产函数为准——只断言
    # rs=0.4（TM 实际取值，≤0.5 上限）与 rs=0.5（夹后上限）的名义比值 ≈ 0.4/0.5（同口径线性）。
    from types import SimpleNamespace
    from desk import manager
    cfg = _cfg()
    mk = {"last": 67.15, "atr14": 5.15}
    bk = {"equity": 108061.73, "live_equity": 108061.73, "cash": 108061.73, "exposure": 0.0,
          "positions": [], "triggers": [], "pending_entries": []}
    agg = {"side": "short", "entry_px": 67.15, "stop_loss": 72.3, "S": -0.281}
    r04 = manager.demo_size(cfg, agg, mk, bk, SimpleNamespace(risk_scale=0.4, leverage=1.0), "SOL-USDT-SWAP")
    r05 = manager.demo_size(cfg, agg, mk, bk, SimpleNamespace(risk_scale=0.5, leverage=1.0), "SOL-USDT-SWAP")
    assert r04.get("ok") and r05.get("ok"), (r04, r05)
    ratio = float(r04["notional"]) / float(r05["notional"])
    assert abs(ratio - 0.8) < 0.02, ratio   # 名义随 risk_scale 线性：夹到 0.5 即按上限全额定仓，不多不少
