"""交易经理无结论（调用失败/解析失败/空输出）：独立即时提醒（按决策号去重）、日报开头写明原因；
有持仓时调用失败/空输出不加仓、不挂新单、原止损止盈不变、持仓不被误平。全部离线，通知走测试捕获通道。"""
import pytest

from desk import settings
from tests.test_tm_no_vote import _st, desk_env  # noqa: F401
from tests.test_tm_required_for_open import OutputParserException, _install, _usage

MUTATORS = ["open_now", "place_trigger", "add_now", "place_add_trigger", "close", "reduce", "_reduce", "move_stop", "move_tp",
            "amend_trigger_sltp", "place_leg_protection", "close_algos"]


@pytest.fixture
def capture(monkeypatch, tmp_path):
    from desk import demo_ws
    sent = []
    monkeypatch.setattr(demo_ws, "notify", lambda ev: sent.append(ev) or True)   # 测试捕获通道，不外发
    monkeypatch.setattr(demo_ws, "TM_NC_SEEN", tmp_path / "tm_nc.json")
    return sent


@pytest.mark.parametrize("kind", ["call_fail", "parse_fail", "empty"])
def test_alert_sent_once_per_discussion(desk_env, monkeypatch, capture, kind):
    manager, calls, box = desk_env
    cfg = settings.load_config()
    if kind == "call_fail":
        _install(monkeypatch, box, RuntimeError("timeout"))
    elif kind == "parse_fail":
        _install(monkeypatch, box, OutputParserException("bad"))
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert len(capture) == 1
    ev = capture[0]
    coin = cfg["coins"][0]["inst_id"].split("-")[0]
    assert ev["kind"] == "tm_no_conclusion" and "tm_no_conclusion" in __import__("desk.demo_ws").demo_ws.NOTIFY_KINDS
    assert "交易经理无结论，不开仓" in ev["message"] and coin in ev["message"] and rec["tm_absent_reason"] in ev["message"]
    # 同一币同一次讨论（同一决策号）重复触发：不再提醒
    from desk import demo_ws
    assert demo_ws.notify_tm_no_conclusion(rec["inst_id"], rec["tm_absent_reason"], rec["decision_id"]) is False
    assert len(capture) == 1


def test_alert_failure_releases_claim(monkeypatch, tmp_path):
    from desk import demo_ws
    p = tmp_path / "x.json"
    assert demo_ws.notify_tm_no_conclusion("BTC-USDT-SWAP", "r", "D-1", notifier=lambda ev: False, path=p) is False
    assert demo_ws.notify_tm_no_conclusion("BTC-USDT-SWAP", "r", "D-1", notifier=lambda ev: True, path=p) is True


def test_no_alert_when_tm_concludes(desk_env, capture):
    manager, calls, box = desk_env
    from tests.test_tm_no_vote import _dec
    cfg = settings.load_config()
    box["dec"] = _dec()
    manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert capture == []


def test_report_top_section(desk_env, monkeypatch, capture):
    manager, calls, box = desk_env
    from desk import demo_exec as DX, report
    cfg = settings.load_config()
    _install(monkeypatch, box, RuntimeError("down"))
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    monkeypatch.setattr(DX, "is_demo", lambda cfg: False)
    txt = open(report.write(cfg, "2099-01-01", [rec], [], [], [], [], _usage(), {}, [], name="t-tmnc"), encoding="utf-8").read()
    head = txt.split("## 一、")[0]
    assert "今日未开仓说明" in head and "交易经理无结论，不开仓" in head and "未通过校验" in head


@pytest.mark.parametrize("kind", ["call_fail", "empty"])
def test_with_position_tm_absent_no_change(desk_env, monkeypatch, capture, kind):
    manager, calls, box = desk_env
    from desk import demo_exec as DX
    cfg = settings.load_config()
    inst = cfg["coins"][0]["inst_id"]
    pos = {"trade_id": "T1", "inst_id": inst, "side": "short", "avg_px": 101.0, "price": 100.0, "qty": 1.0, "contracts": 1,
           "notional": 100.0, "margin": 100.0, "leverage": 1, "sl_px": 104.0, "tp_px": 92.0, "liq_px": None, "funding": 0.0,
           "upnl": 1.0, "deadline": "2026-11-05", "opened": "2026-10-29 08:00:00"}
    snap = dict(pos)
    bk = DX.book(cfg)
    bk["positions"] = [pos]
    hits = []
    for name in MUTATORS:
        if hasattr(DX, name):
            monkeypatch.setattr(DX, name, (lambda n: lambda *a, **k: hits.append(n) or (True, {}))(name))
    if kind == "call_fail":
        _install(monkeypatch, box, RuntimeError("timeout"))
    else:
        box["dec"] = None
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert rec["tm_status"] == "absent"
    assert hits == [], hits                       # 不加仓、不挂新单、不改止损止盈、不平仓
    assert pos == snap                            # 原持仓（含止损止盈）不变
    assert not (rec["aggregate"].get("add") or {}).get("executed")
    assert rec["pending"] is None
    assert "平" not in str(rec["final_action"]) and "加仓" not in str(rec["final_action"])
    assert len(capture) == 1
