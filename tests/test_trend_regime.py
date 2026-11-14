"""趋势市执行规则（2026-11-11 改版，LLM 行情判定）：趋势特征计算、判定解析/回退、提示词/工具说明注入、
validate 按判定执行近价护栏与末段衰竭降级、判定日志。全部离线：特征用合成 K 线，
validate 的组合层护栏（guard_errors/keep_errors）打桩放行，冷却现配置不触发。"""
import pandas as pd
import pytest

from desk import manager, okx_data, settings
from desk import regime_judge as RJ
from desk import tm_order_tool as T

OTC = T.OrderToolCall
INV = dict(invalidation="判断错了就认", rationale="测试理由")

# 现价 100、ATR14=10 → 近价上限 0.3×ATR=3，允许触发范围 97~103
MK = {"last": 100.0, "close": 100.0, "bar_date": "2026-11-10", "ret_5d": -1.0, "ret_20d": -5.0,
      "atr14": 10.0, "hi_20": 120.0, "lo_20": 95.0}
BK = {"equity": 100000.0, "live_equity": 100000.0, "cash": 100000.0, "exposure": 0.0,
      "positions": [], "triggers": [], "pending_entries": []}
FEAT = {"bars": 260, "ma20": 97.0, "ma20_slope_pct": -0.8, "dev_ma20_pct": -2.9,
        "ma50": 104.0, "ma50_slope_pct": -1.1, "dev_ma50_pct": -3.8,
        "ma200": 130.0, "ma200_slope_pct": -0.4, "dev_ma200_pct": -23.1,
        "rsi14": 38.5, "pos_in_range": 0.2}


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


def _judge(**over):
    j = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA50 斜率 -1.20%", "RSI14 38.5"], "summary": "空头趋势进行中",
         "exhaustion_signals": []}
    j.update(over)
    if j["stage"] == "late_exhaustion" and "exhaustion_signals" not in over:
        # v2 代码门：无正面衰竭证据的 late 会被强制降为 mid；测「真 late」语义时默认带一项合法信号，
        # 测代码门本身则显式传 exhaustion_signals=[] 或闭集外值。
        j["exhaustion_signals"] = ["structure_break"]
    return j


def _reg(judge="default", cfg=None, feat=None):
    cfg = cfg or _cfg()
    j = _judge() if judge == "default" else judge
    return RJ.build_regime(cfg, j, dict(FEAT) if feat is None else feat)


# ---------------- 趋势特征计算 ----------------

def _df(closes):
    return pd.DataFrame({"Date": pd.date_range("2026-01-01", periods=len(closes), freq="D"),
                         "Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
                         "Close": closes, "Volume": [1.0] * len(closes), "VolumeUSDT": [1.0] * len(closes),
                         "ts": list(range(len(closes)))})


def test_features_downtrend(monkeypatch):
    closes = [200.0 - 0.4 * i for i in range(260)]          # 单边下跌 200 → 96.4
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df(closes))
    monkeypatch.setattr(RJ, "flow_features", lambda inst: {"flow_missing": True})   # 资金面离线打桩
    f = RJ.features("BTC-USDT-SWAP")
    assert f["bars"] == 260 and f["ma200"] is not None
    assert f["ma20"] < f["ma50"] < f["ma200"]
    assert f["ma50_slope_pct"] < 0 and f["dev_ma20_pct"] < 0 and f["dev_ma200_pct"] < 0
    assert f["rsi14"] == 0.0                                 # 全程下跌：涨幅恒为 0
    assert f["pos_in_range"] is not None and f["pos_in_range"] < 0.2


def test_features_insufficient_history(monkeypatch):
    closes = [100.0 + 0.1 * i for i in range(120)]           # 只有 120 根：MA200 不可得，其余照常
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df(closes))
    monkeypatch.setattr(RJ, "flow_features", lambda inst: {"flow_missing": True})   # 资金面离线打桩
    f = RJ.features("SOL-USDT-SWAP")
    assert f["ma200"] is None and f["ma200_slope_pct"] is None and f["dev_ma200_pct"] is None
    assert f["ma50"] is not None and f["ma50_slope_pct"] > 0 and f["rsi14"] == 100.0


def test_feature_block_renders_values_and_gaps():
    feat = {"bars": 120, "ma20": 100.0, "ma20_slope_pct": -1.2, "dev_ma20_pct": -3.4,
            "ma50": None, "ma50_slope_pct": None, "dev_ma50_pct": None,
            "ma200": None, "ma200_slope_pct": None, "dev_ma200_pct": None,
            "rsi14": 38.5, "pos_in_range": 0.12}
    s = RJ.feature_block(feat, dict(MK))
    assert "MA20 100" in s and "-1.20%" in s and "RSI14 38.5" in s and "数据不足" in s and "12%" in s


# ---------------- 判定解析与回退 ----------------

def test_normalize_valid_dict_and_clamp():
    j = RJ.normalize_judgment({"direction": "long", "strength": "moderate", "stage": "early",
                               "confidence": 1.7, "evidence": "单条证据", "summary": "多头初起"})
    assert j["direction"] == "long" and j["confidence"] == 1.0 and j["evidence"] == ["单条证据"]


def test_normalize_instance():
    j = RJ.normalize_judgment(RJ.RegimeJudgment(direction="none", strength="none", stage="mid",
                                                confidence=0.3, evidence=[], summary="震荡"))
    assert j["direction"] == "none" and j["strength"] == "none"


@pytest.mark.parametrize("bad", [
    None, "x", 42, [], {"direction": "up", "strength": "strong", "stage": "mid", "confidence": 0.5, "evidence": [], "summary": "s"},
    {"direction": "short"},                                            # 缺字段
    {"direction": "short", "strength": "strong", "stage": "mid", "confidence": float("nan"), "evidence": [], "summary": "s"},
])
def test_normalize_rejects_garbage(bad):
    assert RJ.normalize_judgment(bad) is None


def test_call_judge_mock_llm():
    # conftest 置 TA_DESK_MOCK_LLM=1：FakeChatModel 按 schema 造实例（Literal 取首值）
    j = RJ.call_judge(_cfg(), None, "提示")
    assert j and j["direction"] == "short" and j["strength"] == "strong" and 0 <= j["confidence"] <= 1


def test_call_judge_failure_retries_once_then_none(monkeypatch):
    calls = {"n": 0}

    class Boom:
        def invoke(self, prompt):
            calls["n"] += 1
            raise RuntimeError("relay 503")
    monkeypatch.setattr(RJ, "_judge_model", lambda cfg, tracker: Boom())
    assert RJ.call_judge(_cfg(), None, "x") is None and calls["n"] == 2


def test_call_judge_unparseable_returns_none(monkeypatch):
    class Junk:
        def invoke(self, prompt):
            return {"direction": "up"}
    monkeypatch.setattr(RJ, "_judge_model", lambda cfg, tracker: Junk())
    assert RJ.call_judge(_cfg(), None, "x") is None


def test_call_judge_model_build_failure(monkeypatch):
    def boom(cfg, tracker):
        raise RuntimeError("no key")
    monkeypatch.setattr(RJ, "_judge_model", boom)
    assert RJ.call_judge(_cfg(), None, "x") is None


def test_judge_prompt_contents():
    p = RJ.judge_prompt("比特币", "BTC-USDT-SWAP", dict(FEAT), dict(MK), -0.5,
                        {"valid": 11, "long": 1, "short": 8, "neutral": 2}, "市场结论摘录X")
    assert "趋势特征" in p and "S=-0.500" in p and "看空 8" in p and "市场结论摘录X" in p and "late_exhaustion" in p


# ---------------- build_regime：生效/降级口径 ----------------

def test_build_regime_active_and_downgrade():
    r = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion"), None)
    assert r["enabled"] and r["active"] and r["side"] == "short" and r["downgrade"] and not r["failed"]


def test_build_regime_moderate_active():
    r = RJ.build_regime(_cfg(), _judge(strength="moderate"), None)
    assert r["active"] and not r["downgrade"]


@pytest.mark.parametrize("over", [{"strength": "none"}, {"direction": "none", "strength": "none"},
                                  {"direction": "none", "strength": "none", "stage": "late_exhaustion"}])
def test_build_regime_inactive(over):
    r = RJ.build_regime(_cfg(), _judge(**over), None)
    assert not r["active"] and r["side"] is None and not r["downgrade"] and not r["failed"]


def test_build_regime_judge_failed():
    r = RJ.build_regime(_cfg(), None, None)
    assert r["failed"] and not r["active"] and r["side"] is None and not r["downgrade"]


def test_trend_cfg_keys():
    tc = RJ.trend_cfg(_cfg())
    assert tc == {"enabled": True, "near_atr": 0.3, "oversold_risk_scale": 0.5}
    assert not RJ.trend_cfg(_cfg(trend_rule_enabled=False))["enabled"]


# ---------------- decide 侧判定步（manager._run_regime_judge） ----------------

def test_run_regime_judge_mock(monkeypatch):
    monkeypatch.setattr(RJ, "features", lambda inst: {"bars": 260})
    cfg = _cfg()
    reg = manager._run_regime_judge(cfg, cfg["coins"][0], dict(MK), _st(8, 0, cfg), manager.weights(cfg),
                                    -0.5, {"market": "结论"}, None)
    assert not reg["failed"] and reg["judge"]["direction"] == "short" and reg["active"]


def test_run_regime_judge_features_failure(monkeypatch):
    def boom(inst):
        raise RuntimeError("okx down")
    monkeypatch.setattr(RJ, "features", boom)
    cfg = _cfg()
    reg = manager._run_regime_judge(cfg, cfg["coins"][0], dict(MK), _st(8, 0, cfg), manager.weights(cfg),
                                    -0.5, {"market": "m"}, None)
    assert reg["failed"] and not reg["active"] and reg["features"] is None


def test_log_call_appends_jsonl():
    reg = _reg()
    RJ.log_call(date="2026-11-11", session="morning", source="daily", inst="BTC-USDT-SWAP",
                decision_id="D-20261111-BTC", regime=reg)
    rows = settings.read_jsonl(RJ.REGIME_CALLS)
    assert rows and rows[-1]["decision_id"] == "D-20261111-BTC" and rows[-1]["judge"]["direction"] == "short"
    assert rows[-1]["features"]["ma200"] == 130.0 and rows[-1]["active"] is True and rows[-1]["failed"] is False


# ---------------- 提示词与工具说明注入 ----------------

def _prompt(regime, cfg=None):
    cfg = cfg or _cfg()
    coin = cfg["coins"][0]
    st = _st(8, 0, cfg)
    return manager._prompt(cfg, coin, dict(MK), None, dict(BK), st, manager.weights(cfg), -0.5,
                           {"portfolio_manager": "pm", "trader": "t"}, None, regime)


def test_prompt_injects_llm_regime_and_features():
    cfg = _cfg()
    p = _prompt(_reg(cfg=cfg), cfg)
    assert "## 本场行情判定（LLM" in p and "空头趋势进行中" in p and "MA50 斜率 -1.20%" in p
    assert "## 趋势特征（代码计算，仅数据，不含结论）" in p and "MA200 130" in p
    assert "趋势市执行规则（依本场判定生效" in p
    assert "趋势市判定（代码计算" not in p and "不得自行推翻" not in p
    assert "没有足够把握就 hold（本场判定为趋势市时按上方判定段的执行规则）" in p


def test_prompt_downgrade_line():
    cfg = _cfg()
    p = _prompt(_reg(judge=_judge(stage="late_exhaustion"), cfg=cfg), cfg)
    assert "末段衰竭（late_exhaustion），降级生效" in p and "risk_scale≤0.5" in p


def test_prompt_inactive_regime():
    cfg = _cfg()
    p = _prompt(_reg(judge=_judge(direction="none", strength="none"), cfg=cfg), cfg)
    assert "本场趋势市执行规则不生效（判定无趋势或强度不足）" in p


def test_prompt_judge_failed():
    cfg = _cfg()
    p = _prompt(_reg(judge=None, cfg=cfg), cfg)
    assert "判定失败（regime_judge_failed）：本场趋势市执行规则不生效" in p


def test_prompt_no_regime_arg():
    cfg = _cfg()
    p = _prompt(None, cfg)
    assert "本场未取得判定结果，趋势市执行规则不生效" in p


def test_prompt_disabled_no_injection():
    cfg = _cfg(trend_rule_enabled=False)
    p = _prompt(_reg(cfg=cfg), cfg)
    assert "本场行情判定" not in p and "趋势特征" not in p
    assert "没有足够把握就 hold。止损请依据结构位/ATR" in p


def _ictx(regime):
    return {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
            "S": -0.5, "consensus_side": "short", "trend": regime}


def test_instructions_active():
    cfg = _cfg()
    s = T.instructions(_ictx(_reg(cfg=cfg)), cfg)
    assert "近价指触发价距现价≤0.3×ATR14" in s
    assert "会被校验退回并注明允许范围，请改近后重试，不要改成 hold 绕过" in s
    assert "本币本场行情判定（LLM）：空头趋势生效" in s and "近价触发允许范围 97~103" in s and "末段衰竭降级=否" in s


def test_instructions_judge_failed():
    cfg = _cfg()
    s = T.instructions(_ictx(_reg(judge=None, cfg=cfg)), cfg)
    assert "本币本场行情判定失败：趋势市规则不生效" in s


def test_instructions_disabled():
    cfg = _cfg(trend_rule_enabled=False)
    s = T.instructions(_ictx(_reg(cfg=cfg)), cfg)
    assert "行情判定" not in s and "趋势市" not in s


# ---------------- validate() 趋势护栏（按 LLM 判定驱动） ----------------

@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])
    cfg = _cfg()

    def make(regime, existing=None):
        return cfg, {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None,
                     "existing": existing, "S": -0.5, "consensus_side": "short", "trend": regime}
    return make


def _pend(trig, cond, sl, tp, **kw):
    return OTC(action="pending", side="short", trigger_px=trig, condition=cond, stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV, **kw)


def test_validate_far_trigger_rejected_when_active(vctx):
    cfg, ctx = vctx(_reg())
    errs = T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx)
    assert any("[TREND_DIST]" in e and "97" in e and "103" in e for e in errs), errs


def test_validate_far_trigger_rejected_when_moderate(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(strength="moderate")))
    errs = T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx)
    assert any("[TREND_DIST]" in e for e in errs), errs


def test_validate_far_trigger_ok_when_inactive(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(strength="none")))
    assert T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx) == []


def test_validate_far_trigger_ok_on_direction_mismatch(vctx):
    # 判定为多头趋势、订单做空（与共识一致）：判定方向≠下单方向 → 护栏不触发
    cfg, ctx = vctx(_reg(judge=_judge(direction="long")))
    assert T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx) == []


def test_validate_far_trigger_ok_when_judge_failed(vctx):
    cfg, ctx = vctx(_reg(judge=None))
    assert T.validate(cfg, _pend(110.0, "gte", 113.0, 103.5), ctx) == []


def test_validate_far_trigger_ok_when_disabled(vctx):
    cfg = _cfg(trend_rule_enabled=False)
    _, ctx = vctx(_reg(cfg=cfg))
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


def test_validate_keep_pending_not_gated(vctx):
    # keep_pending 沿用原单（触发价 110 本就远）：不在护栏作用域内
    ex = {"algoId": "OLD", "side": "short", "trigger_px": 110.0, "condition": "gte",
          "stop_loss": 113.0, "take_profit": 103.5, "sz": "7", "expires": "2026-12-03 20:00:00+0800"}
    cfg, ctx = vctx(_reg(), existing=ex)
    call = OTC(action="keep_pending", side="short", trigger_px=110.0, stop_loss=113.0,
               take_profit=103.5, expire_hours=48, **INV)
    assert T.validate(cfg, call, ctx) == []


def test_validate_late_exhaustion_open_rejected(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(stage="late_exhaustion")))
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=95.5, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[TREND_OVERSOLD]" in e and "late_exhaustion" in e for e in errs), errs


def test_validate_late_exhaustion_risk_scale_cap(vctx):
    cfg, ctx = vctx(_reg(judge=_judge(stage="late_exhaustion")))
    errs = T.validate(cfg, _pend(102.0, "gte", 104.5, 98.0, risk_scale=0.8), ctx)
    assert any("[TREND_OVERSOLD]" in e and "risk_scale" in e for e in errs), errs
    # 降到上限内 + 近价 → 通过
    assert T.validate(cfg, _pend(102.0, "gte", 104.5, 98.0, risk_scale=0.5), ctx) == []


def test_validate_no_downgrade_no_risk_cap(vctx):
    # 生效但非末段衰竭：risk_scale=0.8 的近价单不受降级上限影响
    cfg, ctx = vctx(_reg())
    assert T.validate(cfg, _pend(102.0, "gte", 104.5, 98.0, risk_scale=0.8), ctx) == []


# ---------------- 判定标准 v2（2026-11-14：衰竭证据门 + 代码门 + 版本号） ----------------

def test_judge_standard_v2_and_signal_set():
    assert RJ.JUDGE_STANDARD == 3
    assert set(RJ.EXHAUSTION_SIGNALS) == {"slope_flattening", "deepening_pullbacks", "structure_break",
                                          "momentum_divergence", "volume_stall", "forced_flow_exhaustion"}


def test_normalize_signals_filtered_raw_kept_and_versioned():
    j = RJ.normalize_judgment({"direction": "short", "strength": "strong", "stage": "late_exhaustion",
                               "confidence": 0.7, "evidence": ["价格创新低而 RSI14 未创新低"], "summary": "衰竭",
                               "exhaustion_signals": ["momentum_divergence", "rsi_oversold", "momentum_divergence"]})
    assert j["exhaustion_signals"] == ["momentum_divergence"]            # 闭集过滤 + 去重
    assert j["exhaustion_signals_raw"] == ["momentum_divergence", "rsi_oversold", "momentum_divergence"]
    assert j["judge_standard"] == 3 and j["stage"] == "late_exhaustion"   # normalize 只解析，覆盖在代码门


def test_normalize_signals_string_wrapped_and_missing_default():
    j = RJ.normalize_judgment({"direction": "short", "strength": "strong", "stage": "late_exhaustion",
                               "confidence": 0.7, "evidence": [], "summary": "s",
                               "exhaustion_signals": "volume_stall"})
    assert j["exhaustion_signals"] == ["volume_stall"]
    j2 = RJ.normalize_judgment({"direction": "short", "strength": "strong", "stage": "mid",
                                "confidence": 0.7, "evidence": [], "summary": "s"})
    assert j2["exhaustion_signals"] == [] and j2["exhaustion_signals_raw"] == []


def test_gate_overrides_late_without_signals():
    r = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion", exhaustion_signals=[]), None)
    j = r["judge"]
    assert j["stage"] == "mid" and j["stage_raw"] == "late_exhaustion" and j["stage_overridden"] is True
    assert j["stage_override_reason"] == "late_exhaustion_without_exhaustion_signals"
    assert r["active"] and not r["downgrade"]                             # 生效口径用被覆盖后的 stage


def test_gate_overrides_late_with_only_invalid_signals():
    raw = RJ.normalize_judgment({"direction": "short", "strength": "strong", "stage": "late_exhaustion",
                                 "confidence": 0.9, "evidence": ["RSI14 22", "贴 20 日区间底部"], "summary": "跌深了",
                                 "exhaustion_signals": ["rsi_oversold", "range_bottom"]})
    assert raw["exhaustion_signals"] == []                                # 闭集外值在 normalize 即被过滤
    r = RJ.build_regime(_cfg(), raw, None)
    assert r["judge"]["stage"] == "mid" and r["judge"]["stage_overridden"] is True and not r["downgrade"]


def test_gate_keeps_late_with_valid_signals():
    r = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion",
                                       exhaustion_signals=["momentum_divergence", "slope_flattening"]), None)
    j = r["judge"]
    assert j["stage"] == "late_exhaustion" and j["stage_raw"] == "late_exhaustion"
    assert j["stage_overridden"] is False and r["downgrade"]             # 合法 late：降级照旧生效
    assert j["exhaustion_signals"] == ["momentum_divergence", "slope_flattening"]


def test_gate_mid_early_untouched():
    for st in ("early", "mid"):
        j = RJ.build_regime(_cfg(), _judge(stage=st), None)["judge"]
        assert j["stage"] == st and j["stage_raw"] == st and j["stage_overridden"] is False


def test_gate_defensive_without_normalize():
    # 未经 normalize 的直构 dict（缺 exhaustion_signals 字段）同样受代码门约束
    j = RJ.apply_stage_gate({"direction": "short", "strength": "strong", "stage": "late_exhaustion",
                             "confidence": 0.5, "evidence": [], "summary": "s"})
    assert j["stage"] == "mid" and j["stage_overridden"] is True and j["exhaustion_signals"] == []
    assert RJ.apply_stage_gate(None) is None


def test_judge_prompt_v2_wording():
    p = RJ.judge_prompt("比特币", "BTC-USDT-SWAP", dict(FEAT), dict(MK), -0.5,
                        {"valid": 11, "long": 1, "short": 8, "neutral": 2}, "")
    assert "伸展变量" in p and "只许用来支撑 direction 与 strength" in p
    assert "都不许推出 late_exhaustion" in p and "强趋势中 RSI 长期钉在极值区" in p
    for key in ("slope_flattening", "deepening_pullbacks", "structure_break",
                "momentum_divergence", "volume_stall", "exhaustion_signals"):
        assert key in p
    assert "结构未破" in p and "强制降为 mid" in p                        # mid 正面定义 + 代码门明示
    assert "现价贴近 20 日低点/区间底部" not in p                          # v1 的拉伸即衰竭定义已删除


def test_prompt_override_note_and_no_downgrade(vctx):
    cfg = _cfg()
    p = _prompt(_reg(judge=_judge(stage="late_exhaustion", exhaustion_signals=[]), cfg=cfg), cfg)
    assert "原始阶段为末段衰竭" in p and "强制降为中段（mid）生效" in p and "降级生效" not in p
    # 被覆盖后 validate 也不再按衰竭降级：现价 open 不被 [TREND_OVERSOLD] 拦
    _, ctx = vctx(_reg(judge=_judge(stage="late_exhaustion", exhaustion_signals=[]), cfg=cfg))
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=95.5, **INV)
    assert not any("[TREND_OVERSOLD]" in e for e in T.validate(cfg, call, ctx))


def test_prompt_late_with_signals_lists_them():
    cfg = _cfg()
    p = _prompt(_reg(judge=_judge(stage="late_exhaustion",
                                  exhaustion_signals=["momentum_divergence"]), cfg=cfg), cfg)
    assert "衰竭证据（正面信号）" in p and "动量背离（momentum_divergence）" in p and "降级生效" in p


def test_log_call_v2_fields():
    reg = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion", exhaustion_signals=[]), dict(FEAT))
    RJ.log_call(date="2026-11-14", session="evening", source="daily", inst="BTC-USDT-SWAP",
                decision_id="D-20261114-BTC", regime=reg)
    row = settings.read_jsonl(RJ.REGIME_CALLS)[-1]
    assert row["judge_standard"] == 3                                     # 记录级版本位：v1 旧记录无此字段
    assert row["judge"]["judge_standard"] == 3
    assert row["judge"]["stage"] == "mid" and row["judge"]["stage_raw"] == "late_exhaustion"
    assert row["judge"]["stage_overridden"] is True and row["downgrade"] is False


# ---------------- 判定标准 v3（2026-11-14：特征扩建 + 第六项资金面衰竭） ----------------

def _df2(closes, vols):
    return pd.DataFrame({"Date": pd.date_range("2026-01-01", periods=len(closes), freq="D"),
                         "Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
                         "Close": closes, "Volume": vols, "VolumeUSDT": vols,
                         "ts": list(range(len(closes)))})


def test_features_slope_history_and_volume_z(monkeypatch):
    # 前 240 根陡峭下跌（-3/根）、后 20 根近乎走平（-0.2/根）：MA20 当前斜率应明显比 5 日前收窄
    closes = [1000.0 - 3.0 * i for i in range(240)] + [283.0 - 0.2 * j for j in range(1, 21)]
    vols = [90.0 if i % 2 == 0 else 110.0 for i in range(259)] + [500.0]   # 末根量能爆量
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df2(closes, vols))
    monkeypatch.setattr(RJ, "flow_features", lambda inst: {"flow_missing": True})
    f = RJ.features("BTC-USDT-SWAP")
    assert f["ma20_slope_pct"] is not None and f["ma20_slope_pct_prev5"] is not None
    assert f["ma20_slope_pct_prev5"] < f["ma20_slope_pct"] < 0              # 5 日前更陡、当前收窄（走平中）
    assert abs(f["ma20_slope_pct"]) <= 0.7 * abs(f["ma20_slope_pct_prev5"])  # 收窄 ≥30%：slope_flattening 可举证
    assert f["ma50_slope_pct_prev5"] is not None
    assert f["vol_z_last"] is not None and f["vol_z_last"] > 2.0           # 爆量 climax
    assert f["vol_z_max5"] is not None and f["vol_z_max5"] >= f["vol_z_last"]
    assert f["last_chg_atr"] is not None and -0.5 < f["last_chg_atr"] < 0  # 末日小跌（滞行侧）


def test_features_volume_z_none_when_flat_volume(monkeypatch):
    closes = [200.0 - 0.4 * i for i in range(260)]
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df(closes))  # Volume 恒为 1.0
    monkeypatch.setattr(RJ, "flow_features", lambda inst: {"flow_missing": True})
    f = RJ.features("BTC-USDT-SWAP")
    assert f["vol_z_last"] is None and f["vol_z_max5"] is None             # 基准窗口 std=0 → 不编造 z 值


def _stub_flow(monkeypatch, funding=None, oi=None):
    def _f(inst):
        if isinstance(funding, Exception):
            raise funding
        return funding

    def _o(inst):
        if isinstance(oi, Exception):
            raise oi
        return oi
    monkeypatch.setattr(RJ, "_fetch_funding", _f)
    monkeypatch.setattr(RJ, "_fetch_oi", _o)
    monkeypatch.setattr(RJ, "_fetch_liq", lambda inst: None)


def test_flow_features_percentile_and_oi_change(monkeypatch):
    hist = [0.0001] * 265 + [-0.001, -0.003, 0.0002, -0.0025, 0.0]         # 270 期，绝大多数高于当前费率
    series = [100.0] * 175 + [100.0, 99.0, 97.0, 94.0, 92.0, 90.0]          # 5 日前 100 → 最新 90 = -10%
    _stub_flow(monkeypatch, funding=(-0.002, hist), oi=(2.0e9, series))
    f = RJ.flow_features("BTC-USDT-SWAP")
    assert f["flow_missing"] is False
    assert f["funding_now"] == -0.002 and f["funding_hist_n"] == 270
    assert f["funding_pctile_90d"] is not None and f["funding_pctile_90d"] <= 10.0   # 第六项门槛 (a) 可达
    assert f["oi_usd"] == 2.0e9 and f["oi_chg_5d_pct"] == -10.0                      # 第六项门槛 (b) 恰达


def test_flow_features_failure_marks_missing(monkeypatch):
    series = [100.0] * 175 + [100.0, 99.0, 97.0, 94.0, 92.0, 90.0]
    _stub_flow(monkeypatch, funding=RuntimeError("okx 50011"), oi=(2.0e9, series))
    f = RJ.flow_features("BTC-USDT-SWAP")
    assert f["flow_missing"] is True                                       # 核心项失败 → missing
    assert f["funding_now"] is None and f["funding_pctile_90d"] is None and f["funding_hist_n"] == 0
    assert f["oi_usd"] == 2.0e9 and f["oi_chg_5d_pct"] == -10.0            # 已取到的部分保留渲染
    _stub_flow(monkeypatch, funding=(-0.002, [0.0001] * 270), oi=RuntimeError("timeout"))
    f2 = RJ.flow_features("BTC-USDT-SWAP")
    assert f2["flow_missing"] is True and f2["oi_usd"] is None and f2["oi_chg_5d_pct"] is None
    assert f2["funding_now"] == -0.002


def test_features_flow_failure_does_not_break_features(monkeypatch):
    closes = [200.0 - 0.4 * i for i in range(260)]
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df(closes))
    _stub_flow(monkeypatch, funding=RuntimeError("down"), oi=RuntimeError("down"))
    f = RJ.features("BTC-USDT-SWAP")                                       # 不抛异常：判定照常进行
    assert f["flow_missing"] is True and f["ma20"] is not None and f["rsi14"] == 0.0


def test_flow_missing_derived_from_final_fields(monkeypatch):
    """回归（2026-11-14 服务器实测：费率字段缺失但 flow_missing=False）：flow_missing 从终态推导——
    费率/OI 任一核心字段最终缺失即 missing，哪怕抓取分支没有抛异常，也不许出现「字段 None 而
    flow_missing=False」（代码门靠这个标记作废 forced_flow_exhaustion，标错=第六项被误放行）。"""
    series = [100.0] * 175 + [100.0, 99.0, 97.0, 94.0, 92.0, 90.0]
    # 费率分支不抛异常地返回现值 None → 终态必须兜住
    _stub_flow(monkeypatch, funding=(None, [0.0001] * 270), oi=(2.0e9, series))
    f = RJ.flow_features("BTC-USDT-SWAP")
    assert f["funding_now"] is None and f["flow_missing"] is True
    # 费率现值在、历史为空 → 分位 None → missing
    _stub_flow(monkeypatch, funding=(0.0001, []), oi=(2.0e9, series))
    f = RJ.flow_features("BTC-USDT-SWAP")
    assert f["funding_now"] == 0.0001 and f["funding_pctile_90d"] is None and f["flow_missing"] is True
    # OI 现值在、序列不足 6 根 → 5 日变化 None → missing
    _stub_flow(monkeypatch, funding=(-0.002, [0.0001] * 270), oi=(2.0e9, [100.0] * 5))
    f = RJ.flow_features("BTC-USDT-SWAP")
    assert f["oi_usd"] == 2.0e9 and f["oi_chg_5d_pct"] is None and f["flow_missing"] is True


def test_fetch_funding_rejects_nonfinite_rate(monkeypatch):
    """回归：OKX 返回 "NaN" 字面量费率时 float() 会静默解析成 nan（旧行为：分位算出 0.0、
    funding_now=nan、flow_missing=False，坏值冒充好数据进特征表）——必须响亮失败并标 missing。"""
    from desk import flows as _flows

    def fake_get(url, ttl=600):
        if "funding-rate-history" in url:
            return {"code": "0", "data": [{"fundingTime": str(1794700800000 - i * 28800000),
                                           "fundingRate": "0.0001", "realizedRate": "0.0001"}
                                          for i in range(100)]}
        if "funding-rate?" in url:
            return {"code": "0", "data": [{"fundingRate": "NaN"}]}
        raise RuntimeError(f"unexpected url {url}")

    monkeypatch.setattr(_flows, "_get", fake_get)
    monkeypatch.setattr(RJ, "_fetch_oi", lambda inst: (2.0e9, [100.0] * 175 + [90.0] * 6))
    monkeypatch.setattr(RJ, "_fetch_liq", lambda inst: None)
    with pytest.raises(ValueError):
        RJ._fetch_funding("BTC-USDT-SWAP")
    f = RJ.flow_features("BTC-USDT-SWAP")
    assert f["funding_now"] is None and f["funding_pctile_90d"] is None and f["flow_missing"] is True
    assert f["oi_usd"] == 2.0e9 and f["oi_chg_5d_pct"] is not None   # OI 分支独立成功、数值保留


FLOW_OK = {"flow_missing": False, "funding_now": -0.002, "funding_pctile_90d": 3.0,
           "funding_hist_n": 270, "oi_usd": 2.0e9, "oi_chg_5d_pct": -12.5}
FLOW_MISSING = {"flow_missing": True, "funding_now": None, "funding_pctile_90d": None,
                "funding_hist_n": 0, "oi_usd": None, "oi_chg_5d_pct": None}


def test_gate_strips_forced_flow_when_flow_missing():
    r = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion",
                                       exhaustion_signals=["forced_flow_exhaustion"]), dict(FLOW_MISSING))
    j = r["judge"]
    assert j["stage"] == "mid" and j["stage_raw"] == "late_exhaustion" and j["stage_overridden"] is True
    assert j["stage_override_reason"] == "late_exhaustion_only_forced_flow_but_flow_missing"
    assert j["exhaustion_signals"] == [] and j["exhaustion_signals_dropped"] == ["forced_flow_exhaustion"]
    assert j["exhaustion_signals_drop_reason"] == "flow_data_missing"
    assert r["active"] and not r["downgrade"]


def test_gate_keeps_forced_flow_late_with_flow_data():
    r = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion",
                                       exhaustion_signals=["forced_flow_exhaustion"]), dict(FLOW_OK))
    j = r["judge"]
    assert j["stage"] == "late_exhaustion" and j["stage_overridden"] is False and r["downgrade"]
    assert j["exhaustion_signals"] == ["forced_flow_exhaustion"] and "exhaustion_signals_dropped" not in j


def test_gate_forced_flow_dropped_but_other_signal_keeps_late():
    r = RJ.build_regime(_cfg(), _judge(stage="late_exhaustion",
                                       exhaustion_signals=["forced_flow_exhaustion", "structure_break"]),
                        dict(FLOW_MISSING))
    j = r["judge"]
    assert j["stage"] == "late_exhaustion" and j["stage_overridden"] is False and r["downgrade"]
    assert j["exhaustion_signals"] == ["structure_break"]                  # 第六项被剔除，其余信号照常生效
    assert j["exhaustion_signals_dropped"] == ["forced_flow_exhaustion"]


def test_gate_forced_flow_kept_without_feat_info():
    # feat 无 flow_missing 标记（旧口径/直构判定）→ 按资金面可用处理，行为与 v2 一致
    j = RJ.apply_stage_gate(_judge(stage="late_exhaustion", exhaustion_signals=["forced_flow_exhaustion"]))
    assert j["stage"] == "late_exhaustion" and "exhaustion_signals_dropped" not in j


def test_prompt_dropped_note():
    cfg = _cfg()
    p = _prompt(_reg(judge=_judge(stage="late_exhaustion",
                                  exhaustion_signals=["forced_flow_exhaustion", "structure_break"]),
                     cfg=cfg, feat=dict(FLOW_MISSING)), cfg)
    assert "该项证据已被代码校验剔除" in p and "降级生效" in p              # 剔除有注记，late 靠其余信号仍生效


def test_judge_prompt_v3_wording():
    p = RJ.judge_prompt("比特币", "BTC-USDT-SWAP", dict(FEAT), dict(MK), -0.5,
                        {"valid": 11, "long": 1, "short": 8, "neutral": 2}, "")
    assert "forced_flow_exhaustion" in p and "资金面衰竭" in p
    assert "分位 ≤10%" in p and "分位 ≥90%" in p and "5 日变化 ≤−10%" in p  # 第六项双硬门槛
    assert "收窄 ≥30%" in p and "5 日前同口径斜率" in p                     # slope_flattening 实测定义
    assert "z 值 ≥2.0" in p and "0.3×ATR14" in p and "0.8×ATR14" in p       # volume_stall climax+推进双条件
    assert "也会被代码剔除" in p                                            # 缺数据引用第六项的后果明示
    assert "以单时点近似" not in p                                          # v2 单时点近似定义已删除


def test_feature_block_v3_segments():
    feat = dict(FEAT, ma20_slope_pct_prev5=-2.04, vol_z_last=2.6, vol_z_max5=3.1, last_chg_atr=-0.12,
                **FLOW_OK)
    s = RJ.feature_block(feat, dict(MK))
    assert "5 日前同口径斜率 -2.04%" in s and "量能：最新日 z 值 2.6" in s
    assert "资金费率（8h）-0.2000%" in s and "分位 3" in s and "持仓量 $2.00B" in s and "5 日变化 -12.50%" in s
    s2 = RJ.feature_block(dict(FEAT), dict(MK))
    assert "本场数据缺失（抓取失败）" in s2 and "forced_flow_exhaustion（资金面衰竭）本场不可引用" in s2
