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
         "evidence": ["MA50 斜率 -1.20%", "RSI14 38.5"], "summary": "空头趋势进行中"}
    j.update(over)
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
    f = RJ.features("BTC-USDT-SWAP")
    assert f["bars"] == 260 and f["ma200"] is not None
    assert f["ma20"] < f["ma50"] < f["ma200"]
    assert f["ma50_slope_pct"] < 0 and f["dev_ma20_pct"] < 0 and f["dev_ma200_pct"] < 0
    assert f["rsi14"] == 0.0                                 # 全程下跌：涨幅恒为 0
    assert f["pos_in_range"] is not None and f["pos_in_range"] < 0.2


def test_features_insufficient_history(monkeypatch):
    closes = [100.0 + 0.1 * i for i in range(120)]           # 只有 120 根：MA200 不可得，其余照常
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: _df(closes))
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
