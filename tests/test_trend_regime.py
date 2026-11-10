"""A+ 趋势市执行规则（2026-11-11 用户批准）：代码判定 trend_regime、提示词/工具说明注入、validate 近价护栏与超卖降级。
全部离线：validate 的组合层护栏（guard_errors）打桩放行，冷却现配置为 0 小时不触发。"""
import pytest

from desk import manager, settings
from desk import tm_order_tool as T

OTC = T.OrderToolCall
INV = dict(invalidation="判断错了就认", rationale="测试理由")

# 现价 100、ATR14=10 → 近价上限 0.3×ATR=3，允许触发范围 97~103
MK = {"last": 100.0, "close": 100.0, "bar_date": "2026-11-10", "ret_5d": -1.0, "ret_20d": -5.0,
      "atr14": 10.0, "hi_20": 120.0, "lo_20": 95.0}
BK = {"equity": 100000.0, "live_equity": 100000.0, "cash": 100000.0, "exposure": 0.0,
      "positions": [], "triggers": [], "pending_entries": []}


def _cfg(**over):
    cfg = settings.load_config()
    if over:
        cfg["manager"] = {**cfg["manager"], **over}
    return cfg


def _st(bear=0, bull=0, cfg=None):
    from desk.roles import ROLE_KEYS
    cfg = cfg or _cfg()
    roles = manager.voters(cfg)
    assert len(roles) == 11
    st = {r: {"direction": "neutral", "confidence": 0.8, "stop": None, "target": None,
              "entry_px": None, "entry_condition": None, "horizon_days": 5} for r in ROLE_KEYS}
    for i, r in enumerate(roles):
        st[r] = {**st[r], "direction": "bearish" if i < bear else ("bullish" if i < bear + bull else "neutral")}
    return st


def _reg(mk=None, S=-0.5, bear=8, bull=0, cfg=None):
    cfg = cfg or _cfg()
    return manager.trend_regime(mk or dict(MK), S, _st(bear, bull, cfg), manager.weights(cfg), cfg)


# ---------------- trend_regime 判定边界 ----------------

def test_short_trend_all_conditions():
    r = _reg()
    assert r["enabled"] and r["trend"] and r["side"] == "short" and not r["oversold"]


def test_boundary_values_pass():
    # S 恰好 -0.30、票数恰好 7、ret_20d 恰好 -3%、ret_5d 恰好 0 → 成立
    r = _reg(mk={**MK, "ret_5d": 0.0, "ret_20d": -3.0}, S=-0.30, bear=7)
    assert r["trend"] and r["side"] == "short"


@pytest.mark.parametrize("mk,S,bear", [
    ({**MK}, -0.29, 8),                 # 条件①：共识差一点
    ({**MK}, -0.5, 6),                  # 条件②：同向票不足
    ({**MK, "ret_20d": -2.9}, -0.5, 8),  # 条件③a：20 日趋势不足
    ({**MK, "ret_5d": 0.1}, -0.5, 8),    # 条件③b：5 日已反转向上
    ({**MK, "last": 110.0}, -0.5, 8),    # 条件④：现价在区间上半部（pos=0.6）
])
def test_each_condition_failing_individually(mk, S, bear):
    r = _reg(mk=mk, S=S, bear=bear)
    assert r["enabled"] and not r["trend"] and r["side"] is None


def test_long_mirror():
    mk = {**MK, "last": 115.0, "ret_5d": 1.0, "ret_20d": 5.0}
    r = _reg(mk=mk, S=0.5, bear=0, bull=8)
    assert r["trend"] and r["side"] == "long" and not r["oversold"]
    # 做多镜像的位置条件：现价在下半部不成立
    r2 = _reg(mk={**mk, "last": 100.0}, S=0.5, bear=0, bull=8)
    assert not r2["trend"]


def test_oversold_by_lo_distance():
    # 现价 95.2 距 20 日低 95 只有 0.21% → 降级（其余条件仍成立）
    r = _reg(mk={**MK, "last": 95.2})
    assert r["trend"] and r["oversold"]


def test_oversold_by_ret5d():
    r = _reg(mk={**MK, "ret_5d": -8.5})
    assert r["trend"] and r["oversold"]


def test_disabled_switch():
    cfg = _cfg(trend_rule_enabled=False)
    r = _reg(cfg=cfg)
    assert not r["enabled"] and not r["trend"]


# ---------------- 提示词与工具说明注入 ----------------

def _prompt(regime, cfg=None):
    cfg = cfg or _cfg()
    coin = cfg["coins"][0]
    st = _st(8, 0, cfg)
    return manager._prompt(cfg, coin, dict(MK), None, dict(BK), st, manager.weights(cfg), -0.5,
                           {"portfolio_manager": "pm", "trader": "t"}, None, regime)


def test_prompt_injects_judgement_and_rule():
    cfg = _cfg()
    p = _prompt(_reg(cfg=cfg), cfg)
    assert "趋势市判定（代码计算，以本行为准，不得自行推翻）" in p
    assert "结论：空头趋势市成立" in p and "超卖降级=否" in p
    assert "## 趋势市执行规则（只改趋势市的入场方式，不改否决门槛）" in p
    assert "没有足够把握就 hold（趋势市按上一段执行）" in p


def test_prompt_non_trend_still_injects_judgement():
    cfg = _cfg()
    p = _prompt(_reg(S=-0.1, cfg=cfg), cfg)
    assert "结论：趋势市不成立" in p


def test_prompt_disabled_no_injection():
    cfg = _cfg(trend_rule_enabled=False)
    p = _prompt(_reg(cfg=cfg), cfg)
    assert "趋势市判定" not in p and "趋势市执行规则" not in p
    assert "没有足够把握就 hold。止损请依据结构位/ATR" in p


def test_instructions_trend_lines():
    cfg = _cfg()
    ctx = {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(cfg=cfg)}
    s = T.instructions(ctx, cfg)
    assert "近价指触发价距现价≤0.3×ATR14" in s
    assert "会被校验退回并注明允许范围，请改近后重试，不要改成 hold 绕过" in s
    assert "空头趋势市成立" in s and "近价触发允许范围 97~103" in s


# ---------------- validate() 趋势护栏 ----------------

@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    cfg = _cfg()

    def make(regime, existing=None):
        return cfg, {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None,
                     "existing": existing, "S": -0.5, "consensus_side": "short", "trend": regime}
    return make


def _pend(trig, cond, sl, tp, **kw):
    return OTC(action="pending", side="short", trigger_px=trig, condition=cond, stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV, **kw)


def test_validate_far_trigger_rejected_only_in_trend(vctx):
    cfg, ctx = vctx(_reg())
    errs = T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx)
    assert any("[TREND_DIST]" in e and "97" in e and "103" in e for e in errs), errs


def test_validate_far_trigger_ok_when_not_trend(vctx):
    cfg, ctx = vctx(_reg(S=-0.1))
    assert T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx) == []


def test_validate_near_gte_and_lte_pass(vctx):
    cfg, ctx = vctx(_reg())
    assert T.validate(cfg, _pend(102.0, "gte", 104.5, 98.0), ctx) == []     # 小反弹近价
    assert T.validate(cfg, _pend(98.0, "lte", 100.5, 94.0), ctx) == []      # 跌破近价


def test_validate_replace_pending_also_gated(vctx):
    ex = {"algoId": "OLD", "side": "short", "trigger_px": 105.0, "condition": "gte",
          "stop_loss": 108.0, "take_profit": 96.0, "sz": "7", "expires": "2026-12-03 20:00:00+0800"}
    cfg, ctx = vctx(_reg(), existing=ex)
    call = OTC(action="replace_pending", side="short", trigger_px=110.0, condition="gte",
               stop_loss=113.0, take_profit=103.5, expire_hours=48, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[TREND_DIST]" in e for e in errs), errs


def test_validate_oversold_open_rejected(vctx):
    reg = _reg(mk={**MK, "last": 95.2})
    assert reg["oversold"]
    cfg, ctx = vctx(reg)
    call = OTC(action="open", side="short", entry=95.2, stop_loss=97.7, take_profit=91.2, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[TREND_OVERSOLD]" in e for e in errs), errs


def test_validate_oversold_risk_scale_cap(vctx):
    reg = _reg(mk={**MK, "last": 95.2})
    cfg, ctx = vctx(reg)
    errs = T.validate(cfg, _pend(97.0, "gte", 99.5, 93.0, risk_scale=0.8), ctx)
    assert any("[TREND_OVERSOLD]" in e and "risk_scale" in e for e in errs), errs
    # 降到上限内 + 近价 → 通过
    assert T.validate(cfg, _pend(97.0, "gte", 99.5, 93.0, risk_scale=0.5), ctx) == []


def test_validate_oversold_flag_off_no_cap(vctx):
    # 非降级趋势市：risk_scale=0.8 的近价单不受降级上限影响
    cfg, ctx = vctx(_reg())
    assert T.validate(cfg, _pend(102.0, "gte", 104.5, 98.0, risk_scale=0.8), ctx) == []
