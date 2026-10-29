"""R7：KILL / HALT 统一路径与语义。"""
import datetime as dt

import pytest

from desk import settings, okx_demo as X, demo_exec as DX, ledger


@pytest.fixture
def flags(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "KILL_PATH", tmp_path / "KILL")
    monkeypatch.setattr(settings, "LEGACY_KILL_PATH", tmp_path / "ledger_KILL")
    monkeypatch.setattr(settings, "HALT_PATH", tmp_path / "HALT")
    monkeypatch.setattr(settings, "_legacy_warned", False)
    return tmp_path


def test_kill_path_constant():
    assert settings.KILL_PATH == settings.STATE / "KILL"
    assert settings.LEGACY_KILL_PATH == settings.STATE / "ledger" / "KILL"
    assert settings.HALT_PATH == settings.STATE / "HALT"
    assert ledger.kill_switch_path({}) == settings.KILL_PATH


def test_kill_new_and_legacy(flags, capsys):
    assert not settings.kill_active() and not settings.no_new_positions()
    (flags / "ledger_KILL").write_text("x")
    assert settings.kill_active()
    assert "迁移" in capsys.readouterr().err
    (flags / "ledger_KILL").unlink(); (flags / "KILL").write_text("x")
    assert settings.kill_active() and not settings.halt_active()
    (flags / "KILL").unlink(); (flags / "HALT").write_text("x")
    assert settings.halt_active() and not settings.kill_active() and settings.no_new_positions()


def _bk():
    return {"equity": 100000, "live_equity": 100000, "positions": [], "triggers": [], "exposure": 0, "pending_entries": []}


@pytest.mark.parametrize("flag,tag", [("KILL", "[KILL]"), ("HALT", "[HALT]")])
def test_check_open_blocked(flags, monkeypatch, flag, tag):
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    cfg = settings.load_config()
    r, _ = DX.check_open(cfg, _bk(), "BTC-USDT-SWAP", 0.1, 100000, 95000, "long", 1)
    assert not r
    (flags / flag).write_text("x")
    r, _ = DX.check_open(cfg, _bk(), "BTC-USDT-SWAP", 0.1, 100000, 95000, "long", 1)
    assert any(tag in x for x in r)


def test_halt_blocks_post_layer(flags, monkeypatch):
    calls = []
    monkeypatch.setattr(X, "_request", lambda m, p, params=None, body=None: calls.append(p) or {"code": "0", "data": []})
    monkeypatch.setattr(X, "EVENTS", flags / "ev.jsonl")
    X.post("/api/v5/trade/order", {"ordType": "market"})
    (flags / "HALT").write_text("x")
    with pytest.raises(X.HaltError):
        X.post("/api/v5/trade/close-position", {})
    with pytest.raises(X.HaltError):
        X.post("/api/v5/trade/amend-algos", {})
    X.post("/api/v5/trade/cancel-algos", [], allow_halt=True)   # 仅 R10 停机撤开仓条件单可放行
    assert calls == ["/api/v5/trade/order", "/api/v5/trade/cancel-algos"]
    X.get  # 读取不受影响（get 不经 post）


def test_kill_does_not_block_post_layer(flags, monkeypatch):
    calls = []
    monkeypatch.setattr(X, "_request", lambda m, p, params=None, body=None: calls.append(p) or {"code": "0", "data": []})
    (flags / "KILL").write_text("x")
    X.post("/api/v5/trade/close-position", {})   # KILL 允许平仓/减仓/收紧止损
    assert calls == ["/api/v5/trade/close-position"]


# ---------------- demo_watch 打桩：KILL 允许到期平仓、HALT 全冻结
def _stub_watch(monkeypatch, tmp_path):
    from desk import demo_watch, entry_limit, demo_ws, monitor
    past = (settings.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S%z")
    opened = (settings.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S%z")
    reg = {"triggers": {}, "positions": {"ETH-USDT-SWAP": {"deadline": past, "opened": opened}}, "watch": {"last_algo_uts": 1}}
    calls = {"close": 0, "el": 0, "rediscuss": 0}
    monkeypatch.setattr(X, "verify", lambda: True)
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(entry_limit, "check_timeouts", lambda cfg: calls.__setitem__("el", calls["el"] + 1) or [])
    monkeypatch.setattr(DX, "reg", lambda: {k: dict(v) for k, v in reg.items()})
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "demo_positions", lambda: [])
    pos = {"inst_id": "ETH-USDT-SWAP", "trade_id": "DEMO-ETH", "side": "short"}
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: {"positions": [pos], "equity": 1, "live_equity": 1, "triggers": []})
    monkeypatch.setattr(DX, "close", lambda cfg, p, why: calls.__setitem__("close", calls["close"] + 1))
    monkeypatch.setattr(demo_ws, "sync_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(X, "get", lambda path, **kw: ({"data": [{"instId": "SOL-USDT-SWAP", "algoId": "9", "uTime": "5", "actualSide": "sl"}]}
                                                      if kw.get("ordType") == "oco" else {"data": []}))
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda cfg, t: calls.__setitem__("rediscuss", calls["rediscuss"] + 1) or "ok")
    try:
        from desk import trigger_review
        monkeypatch.setattr(trigger_review, "poll", lambda *a, **k: {})
    except ImportError:
        pass
    return demo_watch, calls


def test_demo_watch_kill_still_closes_expired(flags, monkeypatch, tmp_path):
    w, calls = _stub_watch(monkeypatch, tmp_path)
    (flags / "KILL").write_text("x")
    cfg = settings.load_config(); cfg["expiry_extend"]["mode"] = "off"   # off：KILL 下到期照常平仓
    assert w.run_once(cfg)["ok"]
    assert calls == {"close": 1, "el": 1, "rediscuss": 1}


def test_demo_watch_kill_full_discussion_still_runs(flags, monkeypatch, tmp_path):
    """KILL 与早晚报同一口径：到期照常全员讨论（平仓/减仓/收紧止损允许，开仓由 check_open 的 [KILL] 护栏拒绝）。"""
    from desk import expiry_discuss as ED
    monkeypatch.setattr(ED, "DIR", tmp_path / "ed")
    sp = []
    monkeypatch.setattr(ED, "SPAWN", lambda args, logfile: sp.append(args))
    w, calls = _stub_watch(monkeypatch, tmp_path)
    (flags / "KILL").write_text("x")
    assert w.run_once(settings.load_config())["ok"]
    assert len(sp) == 1 and calls["close"] == 0


def test_demo_watch_halt_freezes_writes(flags, monkeypatch, tmp_path):
    w, calls = _stub_watch(monkeypatch, tmp_path)
    (flags / "HALT").write_text("x")
    assert w.run_once(settings.load_config())["ok"]
    assert calls == {"close": 0, "el": 0, "rediscuss": 0}


# ---------------- run_daily --mock：KILL 下跳过全部 LLM
@pytest.mark.parametrize("flag", ["KILL", "HALT"])
def test_run_daily_mock_skips_llm(flags, monkeypatch, flag):
    from desk import analysis, review, research, run_daily, report
    (flags / flag).write_text("x")
    def boom(*a, **k):
        raise AssertionError("KILL/HALT 下不得调用 LLM 流程")
    monkeypatch.setattr(analysis, "analyze", boom)
    monkeypatch.setattr(review, "review_trade", boom)
    monkeypatch.setattr(review, "maybe_edit_playbooks", boom)
    monkeypatch.setattr(research, "run", boom)
    seen = {}
    real = report.write
    def spy(*a, **k):
        seen["banner"] = k.get("banner"); seen["decisions"] = a[2]
        return real(*a, **k)
    monkeypatch.setattr(report, "write", spy)
    assert run_daily.main(["--mock", "--report-name", f"test-{flag}"]) == 0
    assert flag in seen["banner"] and seen["decisions"] == []
    txt = (settings.REPORTS / f"test-{flag}.md").read_text()
    assert ("KILL 生效" if flag == "KILL" else "HALT") in txt and "color:red" in txt
