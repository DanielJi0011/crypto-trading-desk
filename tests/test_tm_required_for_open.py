"""2026-10-30 用户决定：最后由交易经理拍板（manager.tm_required_for_open，默认 true）。
交易经理无结论（调用失败、空输出、解析失败）或没给同向现价单/完整条件单 → 不开仓、不加仓、不挂条件单，日报写明原因。全部离线打桩。"""
import pytest

from desk import settings
from tests.test_tm_no_vote import MK, SOURCES, _dec, _st, desk_env  # noqa: F401


class _Boom:
    def __init__(self, exc):
        self.exc = exc


def _install(monkeypatch, box, exc):
    from desk import llm as dllm

    class Fake:
        def with_structured_output(self, *_):
            return self

        def invoke(self, *_a, **_k):
            raise exc
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: Fake())


class OutputParserException(Exception):
    pass


def _usage():
    return {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}


def test_config_default_true():
    from desk import manager
    cfg = settings.load_config()
    assert cfg["manager"]["tm_required_for_open"] is True and manager.tm_required_for_open(cfg)
    assert manager.tm_required_for_open({"manager": {}})


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", ["call_fail", "parse_fail", "empty_none", "empty_dict"])
def test_no_conclusion_no_open_all_paths(desk_env, monkeypatch, source, kind):
    manager, calls, box = desk_env
    cfg = settings.load_config()
    if kind == "call_fail":
        _install(monkeypatch, box, RuntimeError("timeout after 3 retries"))
    elif kind == "parse_fail":
        _install(monkeypatch, box, OutputParserException("bad json"))
    else:
        box["dec"] = None if kind == "empty_none" else {}
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None, source=source)
    assert rec["tm_status"] == "absent"
    assert not calls["open_now"] and not calls["place_trigger"]
    assert rec["final_action"] == "hold(交易经理缺席)" and rec["pending"] is None
    assert rec["notes"][0].startswith("交易经理无结论，不开仓")
    want = {"call_fail": "调用失败", "parse_fail": "解析失败", "empty_none": "输出为空", "empty_dict": "输出为空"}[kind]
    assert want in rec["tm_absent_reason"] and want in rec["notes"][0]


def test_report_shows_reason(desk_env, monkeypatch):
    manager, calls, box = desk_env
    from desk import report
    cfg = settings.load_config()
    _install(monkeypatch, box, OutputParserException("bad"))
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    from desk import demo_exec as DX
    monkeypatch.setattr(DX, "is_demo", lambda cfg: False)
    txt = open(report.write(cfg, "2099-01-01", [rec], [], [], [], [], _usage(), {}, [], name="t-tmreq"), encoding="utf-8").read()
    assert "交易经理无结论，不开仓" in txt and "解析失败" in txt


def test_aggregate_no_fallback_open_by_default():
    from desk import manager
    cfg = settings.load_config()
    cfg["manager"]["tm_require_explicit_levels"] = False  # 旧行为：交易经理缺入场价/止盈时用基准补（2026-10-31 后默认必须给具体点位，见 test_tm_explicit_levels.py）
    st = _st("bearish", entry_px=95.0, cond="lte")
    for tm in (None, _dec(), _dec(action="pending", entry_side="short", entry_trigger_px=103.0), _dec(action="buy", stop_loss=96.0)):
        agg = manager.aggregate(cfg, st, manager.weights(cfg), {"last": 100.0, "atr14": 2.0}, tm=tm)
        assert agg["buy"] is False and agg["plan_group"] is None and agg["tm_no_conclusion"]
        assert any("交易经理无同向有效入场结论" in n for n in agg["notes"])
    # 同向市价单、完整同向条件单不受影响；非法止损仍按基准替代（保持原行为）
    agg = manager.aggregate(cfg, st, manager.weights(cfg), {"last": 100.0, "atr14": 2.0},
                            tm=_dec(action="short", stop_loss=95.0, take_profit=90.0, size_pct=5))
    assert agg["buy"] and agg["plan_group"] == "tm" and agg["stop_loss"] > 100.0
    agg = manager.aggregate(cfg, st, manager.weights(cfg), {"last": 100.0, "atr14": 2.0},
                            tm=_dec(action="pending", entry_side="short", entry_trigger_px=103.0, entry_condition="gte",
                                    entry_stop_loss=106.0, entry_size_pct=5))
    assert agg["buy"] and agg["plan_group"] == "tm_pending" and agg["condition"] == "gte"


def test_switch_off_restores_fallback():
    from desk import manager
    cfg = settings.load_config()
    cfg["manager"]["tm_required_for_open"] = False
    agg = manager.aggregate(cfg, _st("bearish"), manager.weights(cfg), {"last": 100.0, "atr14": 2.0}, tm=None)
    assert agg["buy"] and agg["plan_group"] == "tm_fallback_now" and agg["condition"] == "now"


def test_vote_mode_opposite_tm_no_fallback_open(desk_env):
    """tm_no_vote_weight=false 时方向闸门不拦交易经理反向单（旧逻辑），此前会回退现价 + ATR 基准开仓；现在默认不开仓。"""
    manager, calls, box = desk_env
    cfg = settings.load_config()
    cfg["manager"]["tm_no_vote_weight"] = False
    box["dec"] = _dec(action="buy", size_pct=5, stop_loss=96.0, take_profit=110.0, confidence=0.3)
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert rec["aggregate"]["side"] == "short"
    assert not calls["open_now"] and not calls["place_trigger"] and rec["aggregate"]["tm_no_conclusion"]
    cfg["manager"]["tm_required_for_open"] = False
    calls["open_now"].clear()
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert rec["aggregate"]["side"] == "short"
    assert calls["open_now"] and rec["aggregate"]["plan_group"] == "tm_fallback_now"


def test_switch_off_absent_still_no_open(desk_env, monkeypatch):
    """开关关闭时：缺席本来就不开新仓（该分支原有逻辑），不因开关改变。"""
    manager, calls, box = desk_env
    cfg = settings.load_config()
    cfg["manager"]["tm_required_for_open"] = False
    _install(monkeypatch, box, RuntimeError("x"))
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert rec["tm_status"] == "absent" and not calls["open_now"] and not calls["place_trigger"]
