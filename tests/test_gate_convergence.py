"""决策层门收敛（2026-11-21 用户批准）：① 入场门优先级——validate §3.5 近价带与 §3.8 的 4H
波段位冲突时 H4 优先（近价带单侧放宽到波段位，让位放行留痕）；② 突破保底（海龟式 failsafe）——
现价越过 20 日极值且与下单方向一致时 §3.8 不拦截，留痕 [H4_BREAKOUT_PASS]；③ 周线过滤降为咨询
——manager.weekly_filter.mode=advisory（默认）只披露留痕不硬拦，mode=block 恢复硬拦。
全部离线：合成特征，validate 的组合层护栏打桩放行（同 test_weekly_filter 口径）。
现价 100、ATR14=10 → 原近价带 97~103；4H 波段高点 106 / 低点 92（同 test_h4_entry_structure）。"""
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
W_SHORT = {"weekly_direction": "short", "weekly_missing": False, "weekly_weeks": 36}


def _cfg(**over):
    cfg = settings.load_config()
    if over:
        cfg["manager"] = {**cfg["manager"], **over}
    return cfg


def _judge(**over):
    j = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA50 斜率 -1.20%"], "summary": "空头趋势进行中", "exhaustion_signals": []}
    j.update(over)
    return j


def _reg(feat=None, judge="default", cfg=None):
    cfg = cfg or _cfg()
    f = {"bars": 260}
    if feat:
        f.update(feat)
    j = _judge() if judge == "default" else judge
    return RJ.build_regime(cfg, j, f)


@pytest.fixture
def vctx(monkeypatch):
    monkeypatch.setattr(T, "guard_errors", lambda cfg, call, ctx: [])
    monkeypatch.setattr(T, "keep_errors", lambda cfg, call, ctx: [])

    def make(regime, side="short", existing=None, pos=None, cfg=None, mk_over=None):
        cfg = cfg or _cfg()
        mk = dict(MK)
        if mk_over:
            mk.update(mk_over)
        return cfg, {"inst": "BTC-USDT-SWAP", "mk": mk, "bk": dict(BK), "pos": pos,
                     "existing": existing, "S": 0.5 if side == "long" else -0.5,
                     "consensus_side": side, "trend": regime}
    return make


def _pend_short(trigger=106.0, sl=108.5, tp=99.0):
    return OTC(action="pending", side="short", trigger_px=trigger, condition="gte", stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV)


def _pend_long(trigger=94.0, sl=91.5, tp=101.0):
    return OTC(action="pending", side="long", trigger_px=trigger, condition="lte", stop_loss=sl,
               take_profit=tp, expire_hours=48, **INV)


def _notes(ctx):
    return ctx.get("_gate_notes") or []


# ---------------- 配置：weekly mode ----------------

def test_weekly_cfg_mode_default_advisory():
    assert RJ.weekly_cfg({})["mode"] == "advisory"
    assert RJ.weekly_cfg({"manager": {"weekly_filter": {"mode": "block"}}})["mode"] == "block"
    assert RJ.weekly_cfg({"manager": {"weekly_filter": {"mode": "bogus"}}})["mode"] == "advisory"
    assert RJ.weekly_cfg(_cfg())["mode"] == "advisory"          # dev config.yaml 显式 advisory


def test_build_regime_weekly_mode_field():
    assert _reg(feat=W_SHORT)["weekly"]["mode"] == "advisory"
    cfg = _cfg(weekly_filter={"enabled": True, "mode": "block"})
    assert RJ.build_regime(cfg, None, {"bars": 260, **W_SHORT})["weekly"]["mode"] == "block"


def test_log_call_weekly_mode_field(tmp_path, monkeypatch):
    p = tmp_path / "regime_calls.jsonl"
    monkeypatch.setattr(RJ, "REGIME_CALLS", p)
    RJ.log_call(date="2026-11-21", session="morning", source="daily", inst="BTC-USDT-SWAP",
                decision_id="D-20261121-BTC", regime=_reg(feat=W_SHORT))
    rows = settings.read_jsonl(p)
    assert rows[-1]["weekly_filter_mode"] == "advisory"


# ---------------- §3.5 近价带让位（H4 优先） ----------------

def test_trend_dist_yields_to_h4_swing_high(vctx):
    # 做空触发价恰在波段高点 106：原近价带上限 103 本会拦，H4 让位后放行并留痕
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    assert T.validate(cfg, _pend_short(), ctx) == []
    notes = _notes(ctx)
    assert len(notes) == 1 and "TREND_DIST" in notes[0] and "H4_STRUCT" in notes[0] and "106" in notes[0]


def test_trend_dist_still_blocks_beyond_swing(vctx):
    # 放宽只到波段位：触发价 107 超过放宽后的上限 106 → 仍拦，允许范围写放宽后的 97~106
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    errs = T.validate(cfg, _pend_short(trigger=107.0, sl=109.5, tp=100.0), ctx)
    assert any("[TREND_DIST]" in e and "97" in e and "106" in e for e in errs), errs


def test_trend_dist_no_yield_within_band_then_h4_blocks(vctx):
    # 触发价 102 在原近价带内：§3.5 无让位（无留痕），但低于波段高点 → §3.8 照常拦
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    errs = T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx)
    assert any("[H4_STRUCT]" in e for e in errs), errs
    assert _notes(ctx) == []


def test_trend_dist_no_conflict_no_note(vctx):
    # 波段高点 102 已在近价带内：无冲突，触发价 102 两门齐过，无留痕
    feat = {**H4F, "h4_swing_high": 102.0}
    cfg, ctx = vctx(_reg(feat=feat), side="short")
    assert T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx) == []
    assert _notes(ctx) == []


def test_trend_dist_no_yield_when_h4_disabled(vctx):
    # 结构门关闭 → 不让位：触发价 106 距现价 6 > 近价上限 3 → [TREND_DIST] 拦
    cfg = _cfg(h4_entry_structure={"enabled": False})
    cfg, ctx = vctx(RJ.build_regime(cfg, _judge(), {"bars": 260, **H4F}), side="short", cfg=cfg)
    errs = T.validate(cfg, _pend_short(), ctx)
    assert any("[TREND_DIST]" in e for e in errs), errs


def test_trend_dist_yield_long_mirror(vctx):
    # 做多镜像：波段低点 92 低于近价带下限 97 → 下限放宽到 92，触发价恰在波段低点 92 放行并留痕
    cfg, ctx = vctx(_reg(feat=H4F, judge=_judge(direction="long")), side="long")
    assert T.validate(cfg, _pend_long(trigger=92.0, sl=89.5, tp=99.0), ctx) == []
    notes = _notes(ctx)
    assert len(notes) == 1 and "TREND_DIST" in notes[0] and "92" in notes[0]


def test_gate_notes_reach_decision(vctx):
    # 留痕经 to_decision 透传到决策对象（manager 据此记入决策记录）
    cfg, ctx = vctx(_reg(feat=H4F), side="short")
    call = _pend_short()
    assert T.validate(cfg, call, ctx) == []
    dec = T.to_decision(call, ctx)
    assert dec._gate_notes and "H4_STRUCT" in dec._gate_notes[0]


# ---------------- §3.8 突破保底 ----------------
# 注意（2026-11-21 修复）：生产特征表 features() 从不写 hi_20/lo_20（只在内部算 pos_in_range），
# 20 日极值只在 mk 里。以下用例一律用生产形状：特征表只给 H4 波段，极值走 mk_over。

def test_breakout_pass_short_pending(vctx):
    # 现价 100 ≤ 20 日最低 100（已破位下行）：触发价 102 本会被 §3.8 拦，保底放行并留痕
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="short", mk_over={"lo_20": 100.0})
    assert T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx) == []
    notes = _notes(ctx)
    assert len(notes) == 1 and "[H4_BREAKOUT_PASS]" in notes[0]


def test_breakout_pass_uses_mk_extremes_production_shape(vctx):
    # 回归（2026-11-21 早场实测）：生产特征表不写 hi_20/lo_20，极值只在 mk 里；旧代码只读
    # 特征表 → _ext 为 None、保底永不触发——本用例在旧代码下必失败。
    # 现价 100 严格低于 20 日最低 105（做空破位，形如当早 BTC 现价 58957.4 < 20 日低 61388）：
    # 触发价 102 本会被 §3.8 拦，保底放行并留痕。
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="short", mk_over={"lo_20": 105.0})
    assert "lo_20" not in (ctx["trend"] or {}).get("features", {})
    assert T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx) == []
    assert any("[H4_BREAKOUT_PASS]" in n for n in _notes(ctx))


def test_breakout_pass_short_open(vctx):
    # open 以现价比对同样适用保底：现价 100 < 波段高点 106 本会被拦，破位后放行
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="short", mk_over={"lo_20": 100.0})
    call = OTC(action="open", side="short", entry=100.0, stop_loss=102.5, take_profit=96.0, **INV)
    assert T.validate(cfg, call, ctx) == []
    assert any("[H4_BREAKOUT_PASS]" in n for n in _notes(ctx))


def test_breakout_not_triggered_above_20d_low(vctx):
    # 现价 100 > 20 日最低 99：未破位 → §3.8 照常拦，无留痕
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="short", mk_over={"lo_20": 99.0})
    errs = T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx)
    assert any("[H4_STRUCT]" in e for e in errs), errs
    assert _notes(ctx) == []


def test_breakout_pass_long_mirror(vctx):
    # 做多镜像：现价 100 ≥ 20 日最高 100（已破位上行）：触发价 98 本会被 §3.8 拦，保底放行
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="long", mk_over={"hi_20": 100.0})
    assert T.validate(cfg, _pend_long(trigger=98.0, sl=95.5, tp=105.0), ctx) == []
    assert any("[H4_BREAKOUT_PASS]" in n for n in _notes(ctx))


def test_breakout_wrong_direction_no_pass(vctx):
    # 上行破位（现价 ≥ hi_20）救不了做空单：做空只看 lo_20，现价 100 > 95 → 照常拦
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="short", mk_over={"lo_20": 95.0, "hi_20": 100.0})
    errs = T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx)
    assert any("[H4_STRUCT]" in e for e in errs), errs
    assert _notes(ctx) == []


def test_breakout_missing_extremes_no_pass(vctx):
    # 两处都缺 20 日极值（mk 与特征表均无）→ 保底不触发（fail-soft），§3.8 照常拦
    cfg, ctx = vctx(_reg(feat=H4F, judge=None), side="short")
    ctx["mk"].pop("hi_20", None)
    ctx["mk"].pop("lo_20", None)
    errs = T.validate(cfg, _pend_short(trigger=102.0, sl=104.5, tp=95.0), ctx)
    assert any("[H4_STRUCT]" in e for e in errs), errs


# ---------------- §3.7 周线 advisory ----------------

def test_weekly_advisory_counter_open_passes_with_note(vctx):
    # 默认 advisory：逆周线（周线空头、开多仓）不拦，放行并留痕
    cfg, ctx = vctx(_reg(feat=W_SHORT, judge=None), side="long")
    call = OTC(action="open", side="long", entry=100.0, stop_loss=97.5, take_profit=105.0, **INV)
    assert T.validate(cfg, call, ctx) == []
    notes = _notes(ctx)
    assert len(notes) == 1 and "[WEEKLY_DIR]" in notes[0] and "advisory" in notes[0]


def test_weekly_advisory_counter_pending_passes_with_note(vctx):
    cfg, ctx = vctx(_reg(feat=W_SHORT, judge=None), side="long")
    call = OTC(action="pending", side="long", trigger_px=98.0, condition="lte", stop_loss=95.5,
               take_profit=104.0, expire_hours=48, **INV)
    assert T.validate(cfg, call, ctx) == []
    assert any("[WEEKLY_DIR]" in n for n in _notes(ctx))


def test_weekly_advisory_aligned_no_note(vctx):
    # 与周线同向：无咨询留痕
    cfg, ctx = vctx(_reg(feat=W_SHORT, judge=None), side="short")
    call = OTC(action="pending", side="short", trigger_px=102.0, condition="gte", stop_loss=104.5,
               take_profit=95.0, expire_hours=48, **INV)
    assert T.validate(cfg, call, ctx) == []
    assert _notes(ctx) == []


def test_weekly_block_mode_still_blocks(vctx):
    # mode=block 恢复原硬拦（与 test_weekly_filter 的三条同口径，此处再钉一条 open）
    cfg = _cfg(weekly_filter={"enabled": True, "mode": "block"})
    cfg, ctx = vctx(RJ.build_regime(cfg, None, {"bars": 260, **W_SHORT}), side="long", cfg=cfg)
    call = OTC(action="open", side="long", entry=100.0, stop_loss=97.5, take_profit=105.0, **INV)
    errs = T.validate(cfg, call, ctx)
    assert any("[WEEKLY_DIR]" in e for e in errs), errs
    assert _notes(ctx) == []


# ---------------- 提示词口径 ----------------

def test_regime_block_weekly_line_advisory_wording():
    s = RJ.regime_block(_reg(feat=W_SHORT, judge=_judge()), dict(MK))
    assert "Weekly direction filter" in s and "ADVISORY" in s and "will NOT be rejected" in s
    assert "will be rejected ([WEEKLY_DIR])" not in s


def test_regime_block_weekly_line_block_wording():
    cfg = _cfg(weekly_filter={"enabled": True, "mode": "block"})
    reg = RJ.build_regime(cfg, _judge(), {"bars": 260, **W_SHORT})
    s = RJ.regime_block(reg, dict(MK))
    assert "will be rejected ([WEEKLY_DIR])" in s


def test_instructions_weekly_line_advisory_wording():
    ctx = {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(feat=W_SHORT, judge=None)}
    s = T.instructions(ctx, _cfg())
    assert "Weekly direction filter" in s and "ADVISORY" in s and "will NOT be rejected" in s


def test_instructions_disclose_yield_and_breakout():
    ctx = {"inst": "BTC-USDT-SWAP", "mk": dict(MK), "bk": dict(BK), "pos": None, "existing": None,
           "S": -0.5, "consensus_side": "short", "trend": _reg(feat=H4F, judge=None)}
    s = T.instructions(ctx, _cfg())
    assert "H4 takes precedence" in s and "[H4_BREAKOUT_PASS]" in s
