"""4H 入场结构（2026-11-15 用户批准）：已收盘 4H K 线波段枢轴计算（左右各 2 根严格枢轴、未确认
枢轴不算）、特征表/提示词/工具说明呈现、build_regime 4H 状态与存档、validate §3.8 [H4_STRUCT]
拦截与作用域。全部离线：合成 4H K 线，validate 的组合层护栏打桩放行（同 test_weekly_filter 口径）。"""
import pandas as pd
import pytest

from desk import okx_data, settings
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

# 合成 4H 序列：确认波段高在 idx2(103) 与 idx7(106)，确认波段低在 idx3(93) 与 idx8(92)；
# idx11 的更高高点 108 / 更低低点 90 右侧只有 1 根 K 线（未确认），必须被忽略。
_HIGHS = [100, 101, 103, 102, 100, 99, 104, 106, 105, 103, 102, 108, 107]
_LOWS = [95, 94, 96, 93, 94.5, 97, 98, 96, 92, 93, 95, 90, 91]


def _cfg(**over):
    cfg = settings.load_config()
    if over:
        cfg["manager"] = {**cfg["manager"], **over}
    return cfg


def _df4(highs=_HIGHS, lows=_LOWS, start="2026-11-01"):
    n = len(highs)
    return pd.DataFrame({"Date": pd.date_range(start, periods=n, freq="4h"),
                         "Open": [(h + l) / 2 for h, l in zip(highs, lows)], "High": highs, "Low": lows,
                         "Close": [(h + l) / 2 for h, l in zip(highs, lows)], "Volume": [1.0] * n,
                         "VolumeUSDT": [1.0] * n, "ts": list(range(n))})


def _judge(**over):
    j = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA50 斜率 -1.20%"], "summary": "空头趋势进行中", "exhaustion_signals": []}
    j.update(over)
    return j


def _reg(feat=None, judge=None, cfg=None):
    cfg = cfg or _cfg()
    f = {"bars": 260}
    if feat:
        f.update(feat)
    return RJ.build_regime(cfg, judge, f)


# ---------------- 波段枢轴计算 ----------------

def test_swing_points_latest_confirmed():
    s = RJ.swing_points(_df4())
    assert s["h4_swing_high"] == 106.0 and s["h4_swing_high_time"] == "2026-11-02 04:00"
    assert s["h4_swing_low"] == 92.0 and s["h4_swing_low_time"] == "2026-11-02 08:00"
    assert s["h4_bars"] == 13


def test_swing_points_unconfirmed_tail_pivot_ignored():
    # idx11 的 108/90 右侧只有 1 根，未确认 → 取不到它（上一测试已断言取的是 106/92）；
    # 把序列截到 idx10 为止，确认值不变，进一步证明末端枢轴不参与。
    s = RJ.swing_points(_df4(_HIGHS[:11], _LOWS[:11]))
    assert s["h4_swing_high"] == 106.0 and s["h4_swing_low"] == 92.0


def test_swing_points_tie_is_not_a_pivot():
    s = RJ.swing_points(_df4([100, 105, 105, 100, 99], [90, 95, 95, 99, 100]))
    assert s["h4_swing_high"] is None and s["h4_swing_low"] is None


def test_swing_points_insufficient_bars():
    s = RJ.swing_points(_df4([100, 103, 102, 101], [95, 93, 94, 96]))
    assert s["h4_swing_high"] is None and s["h4_swing_low"] is None and s["h4_bars"] == 4


def test_swing_points_never_raises():
    assert RJ.swing_points(None)["h4_swing_high"] is None
    assert RJ.swing_points(pd.DataFrame())["h4_bars"] == 0


def test_h4_swing_features_fetch_and_failsoft(monkeypatch):
    seen = {}

    def fake_candles(inst, bar="1Dutc", days=420):
        seen["bar"] = bar
        return _df4()
    monkeypatch.setattr(okx_data, "candles", fake_candles)
    f = RJ.h4_swing_features("BTC-USDT-SWAP")
    assert seen["bar"] == "4H"
    assert f["h4_swing_high"] == 106.0 and f["h4_swing_low"] == 92.0 and f["h4_missing"] is False

    def boom(inst, bar="1Dutc", days=420):
        raise RuntimeError("网络失败")
    monkeypatch.setattr(okx_data, "candles", boom)
    f2 = RJ.h4_swing_features("BTC-USDT-SWAP")
    assert f2["h4_swing_high"] is None and f2["h4_swing_low"] is None and f2["h4_missing"] is True


def test_features_includes_h4(monkeypatch):
    closes = [100.0 + 0.5 * i for i in range(280)]
    daily = pd.DataFrame({"Date": pd.date_range("2024-06-03", periods=len(closes), freq="D"),
                          "Open": closes, "High": [c * 1.01 for c in closes],
                          "Low": [c * 0.99 for c in closes], "Close": closes,
                          "Volume": [1.0] * len(closes), "VolumeUSDT": [1.0] * len(closes),
                          "ts": list(range(len(closes)))})
    monkeypatch.setattr(okx_data, "candles",
                        lambda inst, bar="1Dutc", days=420: _df4() if bar == "4H" else daily)
    monkeypatch.setattr(RJ, "flow_features", lambda inst: {"flow_missing": True})   # 资金面离线打桩
    f = RJ.features("BTC-USDT-SWAP")
    assert f["h4_swing_high"] == 106.0 and f["h4_swing_low"] == 92.0 and f["h4_missing"] is False
    block = RJ.feature_block(f, dict(MK))
    assert "4H 波段结构" in block and "波段高点 106" in block and "波段低点 92" in block


def test_feature_block_h4_missing_renders_gap():
    s = RJ.feature_block({"bars": 100}, dict(MK))
    assert "4H 波段结构" in s and "数据不足" in s


def test_judge_prompt_marks_h4_as_execution_data():
    p = RJ.judge_prompt("比特币", "BTC-USDT-SWAP", {"bars": 260, **H4F}, dict(MK), -0.5,
                        {"valid": 9, "long": 1, "short": 7, "neutral": 1}, "")
    assert "4H 波段高低点" in p and "不是你的判定对象" in p


# ---------------- 配置与 regime 状态 ----------------

def test_h4_cfg_default_and_switch():
    assert RJ.h4_cfg({})["enabled"] is True
    assert RJ.h4_cfg({"manager": {"h4_entry_structure": {"enabled": False}}})["enabled"] is False
    assert RJ.h4_cfg(_cfg())["enabled"] is True          # dev config.yaml 显式开启


def test_build_regime_h4_state():
    reg = _reg(feat=H4F)
    h4 = reg["h4"]
    assert h4["swing_high"] == 106.0 and h4["swing_low"] == 92.0 and h4["missing"] is False
    assert h4["short_bound"] == 106.0 and h4["long_bound"] == 92.0
    reg_missing = _reg(feat={})
    assert reg_missing["h4"]["missing"] is True
    assert reg_missing["h4"]["short_bound"] is None and reg_missing["h4"]["long_bound"] is None
    cfg = _cfg(h4_entry_structure={"enabled": False})
    reg_off = RJ.build_regime(cfg, None, {"bars": 260, **H4F})
    assert reg_off["h4"]["enabled"] is False
    assert reg_off["h4"]["short_bound"] is None and reg_off["h4"]["long_bound"] is None
    assert reg_off["h4"]["swing_high"] == 106.0          # 波段值保留展示，只是不再约束


def test_build_regime_h4_bad_values_are_missing():
    reg = _reg(feat={"h4_swing_high": float("nan"), "h4_swing_low": -3.0})
    assert reg["h4"]["swing_high"] is None and reg["h4"]["swing_low"] is None
    assert reg["h4"]["missing"] is True


def test_block_reason_matrix():
    reg = _reg(feat=H4F)
    r = RJ.h4_block_reason(reg, "short", 105.0)
    assert r and "[H4_STRUCT]" in r and "106" in r
    assert RJ.h4_block_reason(reg, "short", 106.0) is None      # 恰等于波段高点可过
    assert RJ.h4_block_reason(reg, "short", 107.0) is None
    r2 = RJ.h4_block_reason(reg, "long", 93.0)
    assert r2 and "[H4_STRUCT]" in r2 and "92" in r2
    assert RJ.h4_block_reason(reg, "long", 92.0) is None        # 恰等于波段低点可过
    assert RJ.h4_block_reason(reg, "long", 91.0) is None
    assert RJ.h4_block_reason(_reg(feat={}), "short", 50.0) is None     # 缺失不拦
    assert RJ.h4_block_reason(None, "short", 50.0) is None
    assert RJ.h4_block_reason({}, "short", 50.0) is None
    assert RJ.h4_block_reason(reg, "short", None) is None
    cfg = _cfg(h4_entry_structure={"enabled": False})
    assert RJ.h4_block_reason(RJ.build_regime(cfg, None, {"bars": 260, **H4F}), "short", 50.0) is None


# ---------------- 提示词 / 工具说明 / 存档 ----------------

def test_regime_block_h4_line():
    s = RJ.regime_block(_reg(feat=H4F, judge=_judge()), dict(MK))
    assert "4H 入场结构" in s and "[H4_STRUCT]" in s and "106" in s


def test_regime_block_h4_line_when_judge_failed():
    # 判定失败时趋势规则不生效，但 4H 结构是纯代码数据，照常提示约束
    s = RJ.regime_block(_reg(feat=H4F, judge=None), dict(MK))
    assert "判定失败" in s and "4H 入场结构" in s and "[H4_STRUCT]" in s


def test_regime_block_no_h4_line_when_disabled():
    cfg = _cfg(h4_entry_structure={"enabled": False})
    s = RJ.regime_block(_reg(feat=H4F, judge=_judge(), cfg=cfg), dict(MK))
    assert "4H 入场结构" not in s


def test_instructions_h4_line():
    ctx = {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(feat=H4F, judge=None)}
    s = T.instructions(ctx, _cfg())
    assert "4H 入场结构" in s and "[H4_STRUCT]" in s and "106" in s


def test_log_call_h4_fields(tmp_path, monkeypatch):
    p = tmp_path / "regime_calls.jsonl"
    monkeypatch.setattr(RJ, "REGIME_CALLS", p)
    RJ.log_call(date="2026-11-15", session="morning", source="daily", inst="BTC-USDT-SWAP",
                decision_id="D-20261115-BTC", regime=_reg(feat=H4F, judge=_judge()))
    rows = settings.read_jsonl(p)
    r = rows[-1]
    assert r["h4_swing_high"] == 106.0 and r["h4_swing_low"] == 92.0
    assert r["h4_short_bound"] == 106.0 and r["h4_long_bound"] == 92.0
    assert r["h4_filter_enabled"] is True and r["h4_missing"] is False
    assert r["features"]["h4_swing_high"] == 106.0


# ---------------- validate() §3.8 4H 入场结构 ----------------

@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])

    def make(regime, side="short", existing=None, pos=None, cfg=None):
        cfg = cfg or _cfg()
        return cfg, {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": pos,
                     "existing": existing, "S": 0.5 if side == "long" else -0.5,
                     "consensus_side": side, "trend": regime}
    return make


def _pend_short(trigger=102.0, sl=104.5, tp=98.0):
    return OTC(action="pending", side="short", trigger_px=trigger, condition="gte", stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV)


def _pend_long(trigger=98.0, sl=95.5, tp=104.0):
    return OTC(action="pending", side="long", trigger_px=trigger, condition="lte", stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV)


def test_validate_short_trigger_inside_swing_blocked(vctx):
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    errs = T.validate(cfg, _pend_short(), ctx)
    assert any("[H4_STRUCT]" in e and "106" in e for e in errs), errs


def test_validate_short_trigger_at_swing_high_passes(vctx):
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    assert T.validate(cfg, _pend_short(trigger=106.0, sl=108.5, tp=98.0), ctx) == []


def test_validate_short_trigger_above_swing_high_passes(vctx):
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    assert T.validate(cfg, _pend_short(trigger=107.0, sl=109.5, tp=99.0), ctx) == []


def test_validate_long_trigger_inside_swing_blocked(vctx):
    cfg, ctx = vctx(_reg(feat=H4F), side="long")
    errs = T.validate(cfg, _pend_long(), ctx)
    assert any("[H4_STRUCT]" in e and "92" in e for e in errs), errs


def test_validate_long_trigger_at_swing_low_passes(vctx):
    cfg, ctx = vctx(_reg(feat=H4F), side="long")
    assert T.validate(cfg, _pend_long(trigger=92.0, sl=89.5, tp=98.0), ctx) == []


def test_validate_long_trigger_below_swing_low_passes(vctx):
    cfg, ctx = vctx(_reg(feat=H4F), side="long")
    assert T.validate(cfg, _pend_long(trigger=91.0, sl=88.5, tp=97.0), ctx) == []


def test_validate_open_short_below_swing_high_blocked(vctx):
    # open 以现价（mk last=100）为入场价比对：100 < 波段高点 106 → 拦
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=96.0, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[H4_STRUCT]" in e for e in errs), errs


def test_validate_open_long_above_swing_low_blocked(vctx):
    # 镜像：open 现价 100 > 波段低点 92 → 拦
    cfg, ctx = vctx(_reg(feat=H4F), side="long")
    call = OTC(action="open", side="long", entry=100.0, stop_loss=97.5, take_profit=105.0, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[H4_STRUCT]" in e for e in errs), errs


def test_validate_open_long_passes_when_last_at_or_below_swing_low(vctx):
    cfg, ctx = vctx(_reg(feat={"h4_swing_high": None, "h4_swing_low": 101.0, "h4_missing": False}),
                    side="long")
    call = OTC(action="open", side="long", entry=100.0, stop_loss=97.5, take_profit=105.0, **INV)
    assert T.validate(cfg, call, ctx) == []


def test_validate_h4_missing_passes(vctx):
    cfg, ctx = vctx(_reg(feat={}), side="short")
    assert T.validate(cfg, _pend_short(), ctx) == []


def test_validate_h4_disabled_passes(vctx):
    cfg = _cfg(h4_entry_structure={"enabled": False})
    cfg, ctx = vctx(RJ.build_regime(cfg, None, {"bars": 260, **H4F}), side="short", cfg=cfg)
    assert T.validate(cfg, _pend_short(), ctx) == []


def test_validate_keep_pending_not_blocked(vctx):
    # keep_pending 沿用原单：不在结构门作用域内（同 §3.5/§3.7 口径）
    ex = {"algoId": "OLD", "side": "short", "trigger_px": 102.0, "condition": "gte",
          "stop_loss": 104.5, "take_profit": 98.0, "sz": "7", "expires": "2026-12-03 20:00:00+0800"}
    cfg, ctx = vctx(_reg(feat=H4F), side="short", existing=ex)
    call = OTC(action="keep_pending", side="short", trigger_px=102.0, stop_loss=104.5,
               take_profit=98.0, expire_hours=48, **INV)
    assert T.validate(cfg, call, ctx) == []


def test_validate_add_not_blocked(vctx):
    # add 是已有持仓的加仓腿，不在结构门作用域内（同 §3.7 口径）
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "short", "avg_px": 100.0, "sz": "5",
           "sl_px": 102.5, "tp_px": 96.0}
    cfg, ctx = vctx(_reg(feat=H4F), side="short", pos=pos)
    call = OTC(action="add", side="short", entry=100.0, stop_loss=102.5, take_profit=96.0, **INV)
    assert T.validate(cfg, call, ctx) == []
