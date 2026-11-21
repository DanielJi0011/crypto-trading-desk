"""周线方向过滤（决策 2，2026-11-15 用户批准）：完整周聚合、周线方向计算（收盘 vs 周 MA20 +
五周斜率）、特征表/提示词/工具说明呈现、build_regime 周线状态与存档、validate §3.7 [WEEKLY_DIR]
拦截与作用域。全部离线：合成日线（2024-06-03 周一起始），validate 的组合层护栏打桩放行
（同 test_trend_regime 口径）。"""
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
W_SHORT = {"weekly_direction": "short", "weekly_missing": False, "weekly_weeks": 36}


def _cfg(**over):
    cfg = settings.load_config()
    if over:
        cfg["manager"] = {**cfg["manager"], **over}
    return cfg


def _df(closes, start="2024-06-03"):   # 2024-06-03 是周一，周界干净
    return pd.DataFrame({"Date": pd.date_range(start, periods=len(closes), freq="D"),
                         "Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
                         "Close": closes, "Volume": [1.0] * len(closes), "VolumeUSDT": [1.0] * len(closes),
                         "ts": list(range(len(closes)))})


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


# ---------------- 完整周聚合与周线方向计算 ----------------

def test_weekly_aggregation_drops_partial_weeks():
    closes = [100.0 + 0.5 * i for i in range(283)]          # 40 个完整周 + 3 天残周（当前周未走完）
    w = RJ.weekly_features(_df(closes))
    assert w["weekly_weeks"] == 40
    assert w["weekly_close"] == closes[279]                  # 残周的更高收盘不算数


def test_weekly_gap_week_dropped():
    df = _df([100.0 + 0.5 * i for i in range(280)]).drop(index=10).reset_index(drop=True)
    assert RJ.weekly_features(df)["weekly_weeks"] == 39      # 缺 1 天的那一周整周剔除


def test_weekly_direction_long_short_none():
    up = RJ.weekly_features(_df([100.0 + 0.5 * i for i in range(280)]))
    dn = RJ.weekly_features(_df([500.0 - 0.5 * i for i in range(280)]))
    flat = RJ.weekly_features(_df([100.0] * 280))
    assert up["weekly_direction"] == "long" and up["weekly_ma20_slope_pct"] > 0 and up["weekly_missing"] is False
    assert dn["weekly_direction"] == "short" and dn["weekly_ma20_slope_pct"] < 0 and dn["weekly_missing"] is False
    assert flat["weekly_direction"] == "none" and flat["weekly_missing"] is False   # 贴均线+斜率 0 → 中性


def test_weekly_direction_conflict_is_none():
    # 先陡跌后强反弹：现价已站上周 MA20，但 MA20 五周斜率仍向下 → 位置与斜率打架 → 中性
    closes = [900.0 - 2.0 * i for i in range(210)] + [480.0 + 3.0 * i for i in range(1, 71)]
    w = RJ.weekly_features(_df(closes))
    assert w["weekly_close"] > w["weekly_ma20"] and w["weekly_ma20_slope_pct"] < 0
    assert w["weekly_direction"] == "none"


def test_weekly_insufficient_history_is_missing():
    w = RJ.weekly_features(_df([100.0 + 0.5 * i for i in range(100)]))   # 14 个完整周 < 25
    assert w["weekly_direction"] is None and w["weekly_missing"] is True and w["weekly_weeks"] == 14


def test_weekly_features_never_raises():
    assert RJ.weekly_features(None)["weekly_missing"] is True
    assert RJ.weekly_features(pd.DataFrame())["weekly_missing"] is True


def test_features_includes_weekly(monkeypatch):
    closes = [100.0 + 0.5 * i for i in range(280)]
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df(closes))
    monkeypatch.setattr(RJ, "flow_features", lambda inst: {"flow_missing": True})   # 资金面离线打桩
    f = RJ.features("BTC-USDT-SWAP")
    assert f["weekly_direction"] == "long" and f["weekly_missing"] is False and f["weekly_weeks"] == 40
    block = RJ.feature_block(f, dict(MK))
    assert "weekly direction" in block and "long" in block and "weekly MA20" in block


def test_feature_block_weekly_missing_renders_gap():
    s = RJ.feature_block({"bars": 100}, dict(MK))
    assert "weekly direction" in s and "n/a" in s


# ---------------- 配置与 regime 状态 ----------------

def test_weekly_cfg_default_and_switch():
    assert RJ.weekly_cfg({})["enabled"] is True
    assert RJ.weekly_cfg({"manager": {"weekly_filter": {"enabled": False}}})["enabled"] is False
    assert RJ.weekly_cfg(_cfg())["enabled"] is True          # dev config.yaml 显式开启


def test_build_regime_weekly_state():
    reg = _reg(feat=W_SHORT)
    assert reg["weekly"]["direction"] == "short" and reg["weekly"]["blocked_side"] == "long"
    assert reg["weekly_direction"] == "short" and reg["weekly"]["missing"] is False
    assert _reg(feat={"weekly_direction": "none"})["weekly"]["blocked_side"] is None
    reg_missing = _reg(feat={})
    assert reg_missing["weekly"]["missing"] is True and reg_missing["weekly"]["blocked_side"] is None
    cfg = _cfg(weekly_filter={"enabled": False})
    reg_off = RJ.build_regime(cfg, None, {"weekly_direction": "short"})
    assert reg_off["weekly"]["enabled"] is False and reg_off["weekly"]["blocked_side"] is None


def test_block_reason_matrix():
    reg = _reg(feat=W_SHORT)
    r = RJ.weekly_block_reason(reg, "long")
    assert r and "[WEEKLY_DIR]" in r and "空头" in r
    assert RJ.weekly_block_reason(reg, "short") is None                       # 同向不拦
    assert RJ.weekly_block_reason(_reg(feat={"weekly_direction": "none"}), "long") is None
    assert RJ.weekly_block_reason(_reg(feat={}), "long") is None              # 缺失不拦
    assert RJ.weekly_block_reason(None, "long") is None
    assert RJ.weekly_block_reason({}, "long") is None
    cfg = _cfg(weekly_filter={"enabled": False})
    assert RJ.weekly_block_reason(RJ.build_regime(cfg, None, dict(W_SHORT)), "long") is None


# ---------------- 提示词 / 工具说明 / 存档 ----------------

def test_regime_block_weekly_line():
    s = RJ.regime_block(_reg(feat=W_SHORT, judge=_judge()), dict(MK))
    assert "Weekly direction filter" in s and "[WEEKLY_DIR]" in s and "short" in s


def test_regime_block_weekly_line_when_judge_failed():
    # 判定失败时趋势规则不生效，但周线过滤是纯代码数据，照常提示拦截
    s = RJ.regime_block(_reg(feat=W_SHORT, judge=None), dict(MK))
    assert "Judgment failed" in s and "Weekly direction filter" in s and "[WEEKLY_DIR]" in s


def test_regime_block_no_weekly_line_when_disabled():
    cfg = _cfg(weekly_filter={"enabled": False})
    s = RJ.regime_block(_reg(feat=W_SHORT, judge=_judge(), cfg=cfg), dict(MK))
    assert "Weekly direction filter" not in s


def test_instructions_weekly_line():
    ctx = {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(feat=W_SHORT, judge=None)}
    s = T.instructions(ctx, _cfg())
    assert "Weekly direction filter" in s and "[WEEKLY_DIR]" in s


def test_log_call_weekly_fields(tmp_path, monkeypatch):
    p = tmp_path / "regime_calls.jsonl"
    monkeypatch.setattr(RJ, "REGIME_CALLS", p)
    RJ.log_call(date="2026-11-15", session="morning", source="daily", inst="BTC-USDT-SWAP",
                decision_id="D-20261115-BTC", regime=_reg(feat=W_SHORT, judge=_judge()))
    rows = settings.read_jsonl(p)
    assert rows[-1]["weekly_direction"] == "short" and rows[-1]["weekly_blocked_side"] == "long"
    assert rows[-1]["weekly_filter_enabled"] is True and rows[-1]["weekly_missing"] is False
    assert rows[-1]["features"]["weekly_direction"] == "short"


# ---------------- validate() §3.7 周线过滤 ----------------

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


def _open_long():
    return OTC(action="open", side="long", entry=100.0, stop_loss=97.5, take_profit=105.0, **INV)


def _pend_long():
    return OTC(action="pending", side="long", trigger_px=98.0, condition="lte", stop_loss=95.5,
               take_profit=104.0, expire_hours=48, **INV)


def _pend_short():
    return OTC(action="pending", side="short", trigger_px=102.0, condition="gte", stop_loss=104.5,
               take_profit=98.0, expire_hours=48, **INV)


def _block_cfg():
    # 2026-11-21 门收敛后默认 advisory 不硬拦；本组三条测的是 mode=block 的原硬拦行为，显式钉住
    return _cfg(weekly_filter={"enabled": True, "mode": "block"})


def test_validate_counter_weekly_open_blocked(vctx):
    # 判定失败（judge=None）也拦：周线过滤不依赖 LLM 判定生效（mode=block 口径）
    cfg = _block_cfg()
    cfg, ctx = vctx(_reg(feat=W_SHORT, cfg=cfg), side="long", cfg=cfg)
    errs = T.validate(cfg, _open_long(), ctx)
    assert any("[WEEKLY_DIR]" in e for e in errs), errs


def test_validate_counter_weekly_pending_blocked(vctx):
    cfg = _block_cfg()
    cfg, ctx = vctx(_reg(feat=W_SHORT, cfg=cfg), side="long", cfg=cfg)
    errs = T.validate(cfg, _pend_long(), ctx)
    assert any("[WEEKLY_DIR]" in e for e in errs), errs


def test_validate_counter_weekly_replace_pending_blocked(vctx):
    ex = {"algoId": "OLD", "side": "long", "trigger_px": 105.0, "condition": "gte",
          "stop_loss": 102.0, "take_profit": 112.0, "sz": "7", "expires": "2026-12-03 20:00:00+0800"}
    cfg = _block_cfg()
    cfg, ctx = vctx(_reg(feat=W_SHORT, cfg=cfg), side="long", existing=ex, cfg=cfg)
    call = OTC(action="replace_pending", side="long", trigger_px=98.0, condition="lte",
               stop_loss=95.5, take_profit=104.0, expire_hours=48, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[WEEKLY_DIR]" in e for e in errs), errs


def test_validate_aligned_with_weekly_passes(vctx):
    cfg, ctx = vctx(_reg(feat=W_SHORT), side="short")
    assert T.validate(cfg, _pend_short(), ctx) == []


def test_validate_weekly_neutral_passes(vctx):
    cfg, ctx = vctx(_reg(feat={"weekly_direction": "none", "weekly_missing": False}), side="long")
    assert T.validate(cfg, _open_long(), ctx) == []


def test_validate_weekly_missing_passes(vctx):
    cfg, ctx = vctx(_reg(feat={}), side="long")
    assert T.validate(cfg, _open_long(), ctx) == []


def test_validate_weekly_disabled_passes(vctx):
    cfg = _cfg(weekly_filter={"enabled": False})
    cfg, ctx = vctx(RJ.build_regime(cfg, None, {"bars": 260, **W_SHORT}), side="long", cfg=cfg)
    assert T.validate(cfg, _open_long(), ctx) == []


def test_validate_keep_pending_not_blocked(vctx):
    # keep_pending 沿用原单：不在过滤作用域内（同 §3.5 口径）
    ex = {"algoId": "OLD", "side": "long", "trigger_px": 110.0, "condition": "gte",
          "stop_loss": 107.0, "take_profit": 116.0, "sz": "7", "expires": "2026-12-03 20:00:00+0800"}
    cfg, ctx = vctx(_reg(feat=W_SHORT), side="long", existing=ex)
    call = OTC(action="keep_pending", side="long", trigger_px=110.0, stop_loss=107.0,
               take_profit=116.0, expire_hours=48, **INV)
    assert T.validate(cfg, call, ctx) == []


def test_validate_add_not_blocked(vctx):
    # add 是已有持仓的加仓腿，不算「开新仓」，不在过滤作用域内（同移动止损作用域口径）
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "long", "avg_px": 100.0, "sz": "5",
           "sl_px": 95.0, "tp_px": 110.0}
    cfg, ctx = vctx(_reg(feat=W_SHORT), side="long", pos=pos)
    call = OTC(action="add", side="long", entry=100.0, stop_loss=97.5, take_profit=105.0, **INV)
    assert T.validate(cfg, call, ctx) == []
