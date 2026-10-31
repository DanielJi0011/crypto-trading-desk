"""2026-10-30 用户决定：交易经理定现有挂单有效期（自原挂单时刻起算）；代码只做硬护栏。离线。"""
import datetime as _d

import pytest

from desk import demo_exec as DX, manager, settings


def _tm(**k):
    base = dict(action="pending", confidence=0.6, rationale="", entry_side="short", entry_trigger_px=79300,
                entry_condition="gte", entry_stop_loss=81950, entry_take_profit=73200, entry_size_pct=5,
                entry_expire_hours=96, risk_scale=0.5)
    base.update(k)
    return manager.ManagerDecision.model_construct(**{**{f: None for f in manager.ManagerDecision.model_fields}, **base})


@pytest.fixture
def cfg():
    return settings.load_config()




def _eth(**k):
    return _tm(**{**dict(entry_side="short", entry_trigger_px=2334, entry_stop_loss=2432, entry_take_profit=2160,
                         entry_expire_hours=144), **k})


# ---------- 有效期 ----------
def _reg(monkeypatch, tmp_path, created):
    import json
    p = tmp_path / "reg.json"
    p.write_text(json.dumps({"positions": {}, "triggers": {"OLD": {"created": created, "expires": "2026-11-04 06:09:42+0800"}}}))
    monkeypatch.setattr(DX, "REG", p)
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "log_event", lambda *a, **k: None)


def test_eth_expiry_from_created(monkeypatch, tmp_path, cfg):
    _reg(monkeypatch, tmp_path, "2026-10-30 06:09:43+0800")
    monkeypatch.setattr(settings, "now", lambda: _d.datetime(2026, 10, 30, 21, 0, tzinfo=_d.timezone(_d.timedelta(hours=8))))
    n = []
    r = manager.apply_tm_expiry(cfg, "ETH-USDT-SWAP", "OLD", _eth(entry_expire_hours=118), n)
    assert r["new"] == "2026-11-04 04:09:43+0800"
    assert DX.reg()["triggers"]["OLD"]["expires"] == "2026-11-04 04:09:43+0800"


def test_expiry_already_passed_kept(monkeypatch, tmp_path, cfg):
    _reg(monkeypatch, tmp_path, "2026-10-20 06:09:43+0800")
    monkeypatch.setattr(settings, "now", lambda: _d.datetime(2026, 10, 30, 21, 0, tzinfo=_d.timezone(_d.timedelta(hours=8))))
    n = []
    assert manager.apply_tm_expiry(cfg, "ETH-USDT-SWAP", "OLD", _eth(entry_expire_hours=118), n) is None
    assert DX.reg()["triggers"]["OLD"]["expires"] == "2026-11-04 06:09:42+0800" and any("已过" in x for x in n)

