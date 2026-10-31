"""2026-10-31 晚场崩溃回归：交易经理下单工具路径产出的 sizing（无 risk_usd/stop_loss_usd）生成日报不得抛 KeyError。全部离线。"""
import pytest

from desk import report

AGG = {"side": "short", "entry_px": 99.5, "stop_loss": 104.5, "take_profit": 89.5, "condition": "lte", "size_pct": 8.26, "leverage": 1.0}
TOOL_RISK = {"ok": True, "how": "risk_sizing", "risk_scale": 0.5, "notional": 8495.61, "leverage": 1.0, "sz": "85.38", "size_pct": 8.2606}
TOOL_MARGIN = {"ok": True, "how": "tm_margin_pct", "margin_pct": 5.0, "notional": 5000.0, "leverage": 1.0, "sz": "50", "size_pct": 5.0}
OLD = {"ok": True, "risk_usd": 986.62, "notional": 20000.0, "leverage": 1.0, "stop_loss_usd": 900.0,
       "clipped": [{"limit": "max_margin_pct", "before": 1, "after": 0.5}]}


def test_tool_risk_sizing_line_computes_loss():
    s = report._sizing_line(TOOL_RISK, AGG)
    loss = 8495.61 * 5.0 / 99.5
    assert "按风险定仓×risk_scale 0.50" in s and "风险预算 按交易经理指定" in s and "张数 85.38" in s
    assert f"止损亏损 {loss:.2f}（按入场价与止损现算）" in s and "名义 8495.61" in s


def test_tool_margin_pct_line():
    s = report._sizing_line(TOOL_MARGIN, AGG)
    assert "交易经理 margin_pct 5.00%" in s and "止损亏损" in s


def test_old_structure_unchanged():
    s = report._sizing_line(OLD, AGG)
    assert "风险预算 986.62 USDT" in s and "止损亏损 900.00" in s and "现算" not in s and "被上限削减：max_margin_pct" in s


@pytest.mark.parametrize("drop", ["risk_usd", "notional", "leverage", "stop_loss_usd", "clipped", "sz", "how"])
def test_missing_each_field_never_raises(drop):
    for base in (OLD, TOOL_RISK, TOOL_MARGIN):
        z = {k: v for k, v in base.items() if k != drop}
        assert report._sizing_line(z, AGG).startswith("；")
        assert report._sizing_line(z, {}).startswith("；")
        assert report._sizing_line(z, None).startswith("；")


def test_no_levels_shows_tm_specified():
    z = {"ok": True, "how": "risk_sizing"}
    s = report._sizing_line(z, {})
    assert "止损亏损 按交易经理指定" in s and "名义 —" in s and "杠杆 —" in s


def test_rejected_and_empty():
    assert report._sizing_line(None) == "" and report._sizing_line({}) == ""
    assert "定仓拒绝 x" in report._sizing_line({"ok": False, "reason": "x"})
    assert "定仓拒绝 —" in report._sizing_line({"ok": False})
    assert report._sizing_line({"ok": True, "notional": "bad", "leverage": None, "clipped": ["R4"]}, AGG).startswith("；")


def test_write_with_real_evening_decisions(monkeypatch, tmp_path):
    """用 2026-10-31 晚场三条真实决策记录（工具路径定仓）端到端生成日报，原先在 _sizing_line 抛 KeyError('risk_usd')。"""
    import json, os
    from desk import demo_exec as DX, settings
    decs = json.load(open(os.path.join(os.path.dirname(__file__), "data", "decisions_20261031_evening.json"), encoding="utf-8"))
    bk = {"equity": 100000.0, "live_equity": 100000.0, "initial": 100000.0, "cash": 100000.0, "exposure": 0.0,
          "equity_basis": "initial", "positions": [], "triggers": []}
    monkeypatch.setattr(DX, "is_demo", lambda cfg: True)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)
    monkeypatch.setattr(DX, "reg", lambda: {"triggers": {}})
    cfg = settings.load_config()
    usage = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}
    p = report.write(cfg, "2099-01-02", decs, [], [], [], [], usage, {}, [], name="t-report-tool-sizing")
    txt = open(p, encoding="utf-8").read()
    assert "风险预算 按交易经理指定" in txt and "张数 85.38" in txt and "（按入场价与止损现算）" in txt
