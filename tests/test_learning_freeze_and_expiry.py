"""学习冻结（L1 开关部分，用户决定）与 Q5 过期只通知。离线。"""
import datetime as dt
import json

from desk import settings, scoring, manager, review


def test_frozen_weights_go_to_shadow(monkeypatch, tmp_path):
    monkeypatch.setattr(scoring, "WEIGHTS", tmp_path / "w.json"); monkeypatch.setattr(scoring, "WEIGHTS_SHADOW", tmp_path / "ws.json")
    monkeypatch.setattr(scoring, "WEIGHT_HIST", tmp_path / "wh.jsonl"); monkeypatch.setattr(manager, "WEIGHTS", tmp_path / "w.json")
    cfg = settings.load_config()
    assert cfg["learning"]["apply_weights"] is False and cfg["learning"]["auto_edit_playbooks"] is False
    assert cfg["learning"]["research_write_playbook"] is False
    (tmp_path / "w.json").write_text(json.dumps({"trade_manager": 2.0}))
    monkeypatch.setattr(scoring, "scores", lambda r: [{"hit": True}] * 40)
    scoring.update_weights(cfg)
    assert json.loads((tmp_path / "w.json").read_text()) == {"trade_manager": 2.0}   # 实用权重文件未被改
    assert (tmp_path / "ws.json").exists()
    assert manager.weights(cfg)["trade_manager"] == manager.TM_INIT_WEIGHT          # 决策用初始权重


def test_frozen_playbook_edit_no_llm(monkeypatch):
    from desk import llm as dllm
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("冻结时不得调用 LLM")))
    cfg = settings.load_config()
    assert review.maybe_edit_playbooks(cfg, None) == [] and review.evaluate_and_rollback(cfg) == []


def test_protected_id_removed():
    assert "4026779985963827200" not in [str(x) for x in settings.load_config()["execution"]["protected_algo_ids"]]


def test_expiry_only_notifies(monkeypatch, tmp_path):
    from desk import demo_watch, demo_exec as DX, okx_demo as X, entry_limit, demo_ws, trigger_review, monitor
    past = (settings.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S%z")
    reg = {"triggers": {"A9": {"inst_id": "BTC-USDT-SWAP", "expires": past}},
           "positions": {"ETH-USDT-SWAP": {"deadline": past, "opened": past}}, "watch": {"last_algo_uts": 1}}
    sent, posts, redis = [], [], []
    monkeypatch.setattr(X, "verify", lambda: True); monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(trigger_review, "poll", lambda cfg: {"reasons": []})
    monkeypatch.setattr(entry_limit, "check_timeouts", lambda cfg: [])
    monkeypatch.setattr(DX, "reg", lambda: reg); monkeypatch.setattr(DX, "save_reg", lambda r: None)
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [{"algoId": "A9", "instId": "BTC-USDT-SWAP"}])
    monkeypatch.setattr(DX, "demo_positions", lambda: [])
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: {"positions": [{"inst_id": "ETH-USDT-SWAP"}], "equity": 1, "live_equity": 1, "triggers": []})
    monkeypatch.setattr(DX, "close", lambda cfg, p, why: None)
    monkeypatch.setattr(X, "post", lambda path, body, **k: posts.append(path) or {"data": [{}]})
    monkeypatch.setattr(X, "get", lambda path, **k: {"data": []})
    monkeypatch.setattr(demo_ws, "sync_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(demo_ws, "notify", lambda ev: sent.append(ev["kind"]))
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda *a: redis.append(1))
    cfg = settings.load_config(); cfg["execution"]["protected_algo_ids"] = []
    cfg["expiry_extend"]["mode"] = "off"   # 本用例测旧行为（到期直接平仓只通知）；全员讨论见 test_expiry_full_discussion.py
    demo_watch.run_once(cfg)
    assert sent == ["trigger_expired", "position_expired"] and redis == []
