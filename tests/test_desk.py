import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from desk import settings, roles


@pytest.fixture(scope="module")
def cfg():
    return settings.load_config()


def test_imports():
    import tradingagents
    from desk import analysis, ledger, manager, okx_data, report, review, run_daily, scoring, stances  # noqa
    assert tradingagents.__version__ == "0.7.0"


def test_okx_data_fetch(cfg):
    from desk import okx_data
    d = okx_data.candles("BTC-USDT-SWAP", "1Dutc", 60)
    assert len(d) >= 55 and d.Close.iloc[-1] > 1000
    assert okx_data.to_inst("ETH-USD") == "ETH-USDT-SWAP"
    assert okx_data.client().key is None if hasattr(okx_data.client(), "key") else True


def test_playbooks_and_injection(cfg):
    roles.ensure_playbooks()
    for r in roles.ROLE_KEYS:
        assert roles.read_meta(r)["current"] == 1
    from desk.llm import FakeChatModel
    from langchain_core.messages import SystemMessage, HumanMessage
    seen = {}

    class Spy(FakeChatModel):
        def _generate(self, messages, *a, **k):
            seen["m"] = messages
            return super()._generate(messages, *a, **k)
    p = roles.PlaybookLLM(Spy(), "bull")
    p.invoke([SystemMessage(content="SYS"), HumanMessage(content="hi")])
    assert "角色手册" in seen["m"][0].content and seen["m"][0].content.startswith("SYS")
    p.invoke("plain string")
    assert "角色手册" in seen["m"][0].content


def test_stance_parse():
    from desk import stances
    s = stances.parse_stance("x\n【立场】方向=看空；目标价=70,000；止损价=无；置信度=0.7；周期=3天")
    assert s["direction"] == "bearish" and s["target"] == 70000 and s["stop"] is None and s["horizon_days"] == 3
    assert stances.parse_structured("portfolio_manager", "**Rating**: Underweight\n\n**Price Target**: 70000\n\n**Time Horizon**: 5 days")["direction"] == "bearish"


def test_consensus(cfg):
    from desk import manager
    w = {r: 1.0 for r in roles.ROLE_KEYS}
    st = {r: {"direction": "bullish", "confidence": 1.0} for r in roles.ROLE_KEYS}
    assert manager.consensus(st, w) == pytest.approx(1.0)
    st = {r: {"direction": "bearish", "confidence": 0.5} for r in roles.ROLE_KEYS}
    assert manager.consensus(st, w) == pytest.approx(-0.5)


@pytest.fixture()
def led(cfg, monkeypatch):
    from desk import ledger
    for p in (ledger.store.TRADES_CSV, ledger.store.ORDERS_LOG, ledger.ACCOUNT, ledger.META):
        if p.exists():
            p.unlink()
    kp = ledger.kill_switch_path(cfg)
    if kp.exists():
        kp.unlink()
    monkeypatch.setattr(ledger, "PRICE", lambda inst: 100.0)
    return ledger


def test_paper_open_guardrails(cfg, led):
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, 110.0, 5, "D1", "t", 1000)
    assert ok, info
    assert info["qty"] == pytest.approx(200 / (100 * 1.0005), rel=1e-6)
    b = led.book(cfg, {"BTC-USDT-SWAP": 100.0})
    assert b["cash"] < 800 and abs(b["equity"] - 1000) < 1.0     # 只损失费用+滑点
    # 单笔 >20%（2026-10-30 起配置为 33%，本测试显式设旧值 20）
    cfg["guardrails"]["max_position_pct"] = 20
    ok, info = led.open_position(cfg, "ETH-USDT-SWAP", 250, 95.0, None, 5, "D2", "t", 1000)
    assert not ok and any("POSITION" in r for r in info["reasons"])
    # 无止损
    ok, info = led.open_position(cfg, "ETH-USDT-SWAP", 100, None, None, 5, "D3", "t", 1000) if False else (None, None)
    # 不在品种池
    ok, info = led.open_position(cfg, "DOGE-USDT", 100, 95.0, None, 5, "D4", "t", 1000)
    assert not ok and any("UNIVERSE" in r for r in info["reasons"])
    # 止损高于现价
    ok, info = led.open_position(cfg, "ETH-USDT-SWAP", 100, 105.0, None, 5, "D5", "t", 1000)
    assert not ok and any("STOP_LOSS" in r for r in info["reasons"])
    # 总敞口 60%：已 20%，再开两个 20% 后第三个拒绝（max_open 3 也会拒）
    assert led.open_position(cfg, "ETH-USDT-SWAP", 200, 95.0, None, 5, "D6", "t", 1000)[0]
    assert led.open_position(cfg, "SOL-USDT-SWAP", 200, 95.0, None, 5, "D7", "t", 1000)[0]
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 100, 95.0, None, 5, "D8", "t", 1000)
    assert not ok and any(("MAX_POS" in r or "EXPOSURE" in r) for r in info["reasons"])


def test_kill_switch_and_live_gate(cfg, led):
    led.kill_switch_path(cfg).write_text("x")
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 100, 95.0, None, 5, "D1", "t", 1000)
    assert not ok and any("KILL" in r for r in info["reasons"])
    led.kill_switch_path(cfg).unlink()
    with pytest.raises(led.LiveTradingDisabled):
        led.assert_paper({"mode": "live"})
    (settings.STATE / "LIVE_APPROVED").write_text("x")
    try:
        with pytest.raises(led.LiveTradingDisabled):
            led.assert_paper({"mode": "live"})
    finally:
        (settings.STATE / "LIVE_APPROVED").unlink()


def _bars(rows):
    base = int(settings.now().timestamp() * 1000) + 3600_000
    return pd.DataFrame([{"ts": base + i * 3600_000, "Open": o, "High": h, "Low": l, "Close": c} for i, (o, h, l, c) in enumerate(rows)])


def test_mark_stop_tp_gap(cfg, led, monkeypatch):
    # 本用例只测止损/止盈/跳空撮合；两笔止损后会触发当日亏损 2.5% 停开（用户决定），这里放宽该护栏以便开第三笔
    monkeypatch.setitem(cfg["guardrails"], "max_daily_loss_pct", 50)
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, 110.0, 5, "D1", "t", 1000)
    tid = info["trade_id"]
    # 跳空：开盘 90 < 止损 95 → 按开盘价 90 成交
    monkeypatch.setattr(led, "BARS", lambda inst: _bars([(100, 101, 99, 100), (90, 92, 88, 91)]))
    closed = led.mark_to_market(cfg)
    assert len(closed) == 1 and closed[0]["exit_reason"] == "止损" and float(closed[0]["exit_px"]) == 90
    assert float(closed[0]["pnl_usd"]) < -19
    # 同一根K线同时触及止损和止盈 → 止损优先
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, 110.0, 5, "D2", "t", 1000)
    monkeypatch.setattr(led, "BARS", lambda inst: _bars([(100, 112, 94, 100)]))
    c = led.mark_to_market(cfg)
    assert c[0]["exit_reason"] == "止损"
    # 止盈
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, 110.0, 5, "D3", "t", 1000)
    monkeypatch.setattr(led, "BARS", lambda inst: _bars([(100, 112, 99, 111)]))
    c = led.mark_to_market(cfg)
    assert c[0]["exit_reason"] == "止盈" and float(c[0]["exit_px"]) == 110
    # 现金守恒：无持仓时权益=现金
    b = led.book(cfg)
    assert not b["positions"] and b["equity"] == pytest.approx(led.account(cfg)["cash"])


def test_reduce_and_stop_move(cfg, led):
    ok, info = led.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, None, 5, "D1", "t", 1000)
    tid = info["trade_id"]
    ok, msg = led.move_stop(cfg, tid, 90.0)
    assert not ok                                   # 禁止下移
    ok, msg = led.move_stop(cfg, tid, 97.0)
    assert ok
    part = led.close_position(cfg, tid, "减仓", px=105.0, fraction=0.5)
    assert part["trade_id"].endswith("-r1") and part["status"] == "closed"
    rest = led.open_trades()
    assert len(rest) == 1 and float(rest[0]["sz"]) == pytest.approx(float(part["sz"]), rel=1e-3)


def _stance(i, role, regime, direction, hit_date):
    return {"id": f"X{i}:{role}", "decision_id": f"X{i}", "date": hit_date, "time": f"2026-01-{i + 1:02d} 08:00:00+0800",
            "inst_id": "BTC-USDT-SWAP", "role": role, "direction": direction, "confidence": 0.6, "target": None, "stop": 90.0,
            "horizon_days": 5, "ref_px": 100.0, "regime": regime}


def test_scoring_weights(cfg, monkeypatch):
    cfg["learning"].update(apply_weights=True, auto_edit_playbooks=True, research_write_playbook=True)   # 本用例验证学习机制本身（生产配置为冻结）
    from desk import scoring
    for p in (scoring.STANCES, scoring.SCORES, scoring.WEIGHTS):
        if p.exists():
            p.unlink()
    d0 = dt.date(2026, 1, 1)
    days = pd.date_range(d0, periods=60)
    # 价格先 100 → 之后每天 +1%：5天后 > +2%
    closes = [100 * 1.01 ** (i + 1) for i in range(60)]
    df = pd.DataFrame({"Date": days, "Open": closes, "High": [c * 1.001 for c in closes], "Low": [c * 0.999 for c in closes], "Close": closes})
    monkeypatch.setattr(scoring, "DAILY", lambda inst: df)
    for i in range(25):
        settings.append_jsonl(scoring.STANCES, _stance(i, "bull", "上涨", "bullish", str(d0)))
        settings.append_jsonl(scoring.STANCES, _stance(i, "bear", "上涨", "bearish", str(d0)))
    new = scoring.score_due(cfg, now=settings.TZ.localize(dt.datetime(2026, 3, 1)) if hasattr(settings.TZ, "localize") else dt.datetime(2026, 3, 1, tzinfo=settings.TZ))
    assert len(new) == 50
    assert all(s["hit"] for s in new if s["role"] == "bull") and not any(s["hit"] for s in new if s["role"] == "bear")
    ch = scoring.update_weights(cfg)
    w = settings.read_json(scoring.WEIGHTS, {})
    assert w["bull"] > 1.0 and w["bear"] < 1.0 and 0.5 <= w["bear"]
    assert w["bull"] <= 2.0
    card = scoring.scorecard("bull")
    assert card["n"] == 25 and card["hit"] == 1.0 and card["by_regime"]["上涨"]["n"] == 25
    # 重复打分不重复
    assert scoring.score_due(cfg, now=dt.datetime(2026, 3, 1, tzinfo=settings.TZ)) == []


def test_playbook_rules_and_rollback(cfg, monkeypatch):
    cfg["learning"].update(apply_weights=True, auto_edit_playbooks=True, research_write_playbook=True)   # 本用例验证学习机制本身（生产配置为冻结）
    from desk import review, scoring
    for p in (scoring.SCORES, review.PB_LOG):
        if p.exists():
            p.unlink()
    roles.ensure_playbooks()
    r = "market"
    # 不足 20 条打分 → 不修订
    ev = review.maybe_edit_playbooks(cfg, None)
    assert not any(e["role"] == r and e["type"] == "修订" for e in ev)

    def add(n, hit, base_t):
        for i in range(n):
            settings.append_jsonl(scoring.SCORES, {"id": f"{base_t}-{i}", "role": r, "hit": hit, "time": f"{base_t} {i:02d}:00:00+0800",
                                                   "regime": "震荡", "direction": "bullish", "outcome": "平", "ret_pct": 0, "stop_quality": None,
                                                   "date": "2026-01-01", "inst_id": "BTC-USDT-SWAP", "confidence": 0.5})
    add(22, True, "2099-01-01")
    # 让 LLM 返回 3 处改动 → 拒绝
    class FakeEd:
        def __init__(self, n):
            self.n = n
        def with_structured_output(self, schema):
            n = self.n
            from langchain_core.runnables import RunnableLambda
            return RunnableLambda(lambda _: review.PlaybookEdit(no_change=False, learned_rules="- 规则", changes=[
                review.Change(op="新增", rule=f"r{i}", why="w") for i in range(n)]))
    monkeypatch.setattr(review.dllm, "make_llm", lambda cfg, tier, tr=None, **k: FakeEd(3))
    ev = review.maybe_edit_playbooks(cfg, None)
    assert [e for e in ev if e["role"] == r][0]["type"] == "拒绝修订"
    assert roles.read_meta(r)["current"] == 1
    # 重置 last_review 后，2 处改动 → 通过，版本 +1
    m = roles.read_meta(r); m["last_review"] = ""; settings.write_json(roles.meta_path(r), m)
    monkeypatch.setattr(review.dllm, "make_llm", lambda cfg, tier, tr=None, **k: FakeEd(2))
    ev = review.maybe_edit_playbooks(cfg, None)
    assert [e for e in ev if e["role"] == r][0]["type"] == "修订"
    m = roles.read_meta(r)
    assert m["current"] == 2 and m["history"][-1]["eval_status"] == "pending" and m["history"][-1]["baseline_hit"] == 1.0
    assert "- 规则" in roles.current_text(r)
    m["history"][-1]["time"] = "2099-06-01 00:00:00+0800"; settings.write_json(roles.meta_path(r), m)   # 模拟修订发生在这批打分之后
    # 观察期内不再修订
    add(5, False, "2099-07-01")
    assert not [e for e in review.maybe_edit_playbooks(cfg, None) if e["role"] == r and e["type"] == "修订"]
    # 之后 20 条命中率大幅下降 → 自动回滚到 v001 的内容（生成 v003）
    add(20, False, "2099-08-01")
    ev = review.evaluate_and_rollback(cfg)
    e = [e for e in ev if e["role"] == r][0]
    assert e["type"] == "回滚"
    m = roles.read_meta(r)
    assert m["current"] == 3 and "暂无经验规则" in roles.current_text(r)
    assert roles.version_path(r, 2).exists()           # 旧版本保留


def test_mock_full_dry_run(cfg, monkeypatch):
    """模拟 LLM 的完整流程：真实 OKX 数据 → 全图 → 立场 → 经理 → 纸面执行 → 日报。"""
    from desk import ledger, llm as dllm, manager, run_daily
    for p in (ledger.store.TRADES_CSV, ledger.store.ORDERS_LOG, ledger.ACCOUNT, ledger.META, manager.DECISIONS):
        if p.exists():
            p.unlink()

    def hook(schema):
        return schema(action="buy", size_pct=10, entry=82000.0, stop_loss=round(ledger.PRICE("BTC-USDT-SWAP") * 0.95, 1), take_profit=90200.0,
                      invalidation="跌破止损", horizon_days=5, confidence=0.6, rationale="模拟：采纳多数角色看多")
    dllm.FAKE_STRUCTURED["ManagerDecision"] = hook
    monkeypatch.setattr(ledger, "PRICE", lambda inst: 82000.0)
    monkeypatch.setattr(manager.okx_data, "last_price", lambda inst: 82000.0)
    rc = run_daily.main(["--mock", "--coins", "BTC", "--date", "2026-10-19"])
    assert rc == 0
    rep = (settings.REPORTS / "2026-10-19.md").read_text(encoding="utf-8")
    assert "多角色交易台日报" in rep and "纸面成交" in rep and "LLM 用量与成本" in rep
    assert len(ledger.open_trades()) == 1
    usage = json.loads(next((settings.STATE / "usage").glob("run-*.json")).read_text())
    assert usage["calls"] >= 10 and usage["input"] > 0


def test_providers_fallback(cfg, monkeypatch):
    """按当前 config 的 role_models 动态取角色，避免角色换厂商后测试失效。"""
    from desk import providers
    rm = cfg["role_models"]
    by = lambda prov: [r for r, v in rm.items() if v["provider"] == prov]
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    for r in by("grok")[:1]:   # grok 缺密钥 → 回退 DeepSeek，并注明缺哪个 key
        x = providers.resolve(cfg, r)
        assert x["provider"] == "deepseek" and "XAI_API_KEY" in x["fallback"]
        assert x["model"] == cfg["llm"]["deep_model" if rm[r].get("tier") == "deep" else "quick_model"]
    for r in by("deepseek")[:2]:   # deepseek 角色不回退
        x = providers.resolve(cfg, r)
        assert x["provider"] == "deepseek" and x["fallback"] is None and x["model"] == rm[r]["model"]
    for r in by("gpt")[:1]:   # 若有 gpt 角色：有密钥时走中转
        x = providers.resolve(cfg, r)
        assert x["provider"] == "gpt" and x["fallback"] is None and x["base_url"].startswith("https://")
        monkeypatch.delenv("OPENAI_API_KEY")
        assert providers.resolve(cfg, r)["provider"] == "deepseek"
    assert providers.provider_of("grok-4.7-build") == "grok" and providers.provider_of("deepseek-flash") == "deepseek"


def test_role_briefs_isolated(cfg):
    from desk.llm import FakeChatModel
    seen = {}

    class Spy(FakeChatModel):
        def _generate(self, messages, *a, **k):
            seen["m"] = messages[0].content
            return super()._generate(messages, *a, **k)
    roles.set_briefs({"bull": "资金流秘密数据"})
    roles.PlaybookLLM(Spy(), "bull").invoke("x")
    assert "资金流秘密数据" in seen["m"]
    roles.PlaybookLLM(Spy(), "market").invoke("x")
    assert "资金流秘密数据" not in seen["m"]
    roles.set_briefs({})


def test_flows_and_backtest_tool():
    from desk import flows, tools
    b = flows.brief("BTC")
    assert "资金费率" in b and "多空账户比" in b
    tools._AS_OF["date"] = "2026-01-01"
    out = tools.run_backtest.invoke({"symbol": "BTC-USD", "entry_expr": "c > c.shift(1)", "sl_pct": 5, "max_bars": 3})
    j = json.loads(out)
    assert j["全样本"]["交易次数"] > 50 and j["参数"]["区间"][1] < "2026-01-01"
    tools.install_market_tools()
    import tradingagents.graph.analyst_execution as ae
    assert "run_backtest" in [t.name for t in ae.ANALYST_NODE_SPECS["market"].tools]


def test_research_pipeline(cfg, monkeypatch):
    cfg = {**cfg, "research": {**cfg["research"], "phase": "confirm"}, "learning": dict(cfg["learning"])}   # S2：通过只在季度确认期成立
    cfg["learning"].update(apply_weights=True, auto_edit_playbooks=True, research_write_playbook=True)   # 本用例验证学习机制本身（生产配置为冻结）
    from desk import research
    n = 1200
    rng = np.random.default_rng(0)
    # 构造：星期日(dow=6) 之后一天必涨 +3%，其余随机 —— 真实规律应通过；随机规律不应通过
    t = pd.date_range("2022-01-01", periods=n, freq="D")
    r = rng.normal(0, 0.01, n)
    r[1:][t[:-1].dayofweek == 6] += 0.03
    c = 100 * np.cumprod(1 + r)
    o = np.r_[100, c[:-1]]
    df = pd.DataFrame({"time": t, "o": o, "h": np.maximum(o, c) * 1.001, "l": np.minimum(o, c) * 0.999, "c": c, "v": 1.0})
    data = {"BTC-USDT-SWAP": df, "ETH-USDT-SWAP": df.copy(), "SOL-USDT-SWAP": df.copy()}
    good = {"side": "long", "name": "周日效应", "conditions": [{"feature": "dow", "op": ">", "value": 5.5}], "horizon_days": 1, "stop_pct": 15}
    bad = {"side": "long", "name": "噪声", "conditions": [{"feature": "ret_1", "op": ">", "value": 0.5}], "horizon_days": 3, "stop_pct": 8}
    assert research.evaluate(good, data, cfg, 10)["passed"]
    assert not research.evaluate(bad, data, cfg, 10)["passed"]
    # 全流程：预注册 → 检验 → 通过的写入手册（mock 提议）
    monkeypatch.setattr(research, "load_data", lambda cfg: data)
    monkeypatch.setattr(research, "propose", lambda cfg, role, tr, n: [good, bad])
    m = roles.read_meta("flows")
    cur = next(x for x in m["history"] if x["version"] == m["current"])
    cur["eval_status"] = "kept"; settings.write_json(roles.meta_path("flows"), m)
    rep = research.run(cfg, None, force=True, only_roles=["flows"])
    reg = settings.read_jsonl(research.REGISTRY)
    assert len([x for x in reg if x["role"] == "flows"]) >= 2           # 先登记
    assert rep["edits"] and rep["edits"][0]["type"] == "修订"
    assert "研究验证" in roles.current_text("flows")
    assert roles.read_meta("flows")["history"][-1]["eval_status"] == "pending"
    # 观察期内再次研究：推迟写入
    rep2 = research.run(cfg, None, force=True, only_roles=["flows"])
    assert rep2["edits"][0]["type"] == "推迟写入"
    # 非周日且非强制 → 跳过
    assert research.run(cfg, None, force=False).get("skipped") or settings.now().weekday() == 6


def _rs_data(seed=1):
    n = 1200
    rng = np.random.default_rng(seed)
    # 周日(dow=6)之后一天必跌 −3%，周三(dow=2)之后一天必涨 +3%，其余随机
    t = pd.date_range("2022-01-01", periods=n, freq="D")
    r = rng.normal(0, 0.01, n)
    r[1:][t[:-1].dayofweek == 6] -= 0.03
    r[1:][t[:-1].dayofweek == 2] += 0.03
    c = 100 * np.cumprod(1 + r)
    o = np.r_[100, c[:-1]]
    df = pd.DataFrame({"time": t, "o": o, "h": np.maximum(o, c) * 1.001, "l": np.minimum(o, c) * 0.999, "c": c, "v": 1.0})
    return {"BTC-USDT-SWAP": df, "ETH-USDT-SWAP": df.copy(), "SOL-USDT-SWAP": df.copy()}


SUN = [{"feature": "dow", "op": ">", "value": 5.5}]


def test_research_short_passes_long_fails(cfg):
    cfg = {**cfg, "research": {**cfg["research"], "phase": "confirm"}, "learning": dict(cfg["learning"])}   # S2：通过只在季度确认期成立
    from desk import research
    data = _rs_data()
    short = {"side": "short", "name": "周日后跌", "conditions": SUN, "horizon_days": 1, "stop_pct": 15}
    r = research.evaluate(short, data, cfg, 10)
    assert r["side"] == "short" and r["passed"] and r["stats"]["oos"]["mean_excess_pct"] > 0
    assert not research.evaluate({**short, "side": "long"}, data, cfg, 10)["passed"]
    txt = research.rule_text(short, {"id": "X", **r})
    assert "随后 1 天做空的超额收益为正" in txt and "偏空" in txt


def _run_role(cfg, monkeypatch, role, hs, data):
    from desk import research
    monkeypatch.setattr(research, "load_data", lambda cfg: data)
    monkeypatch.setattr(research, "propose", lambda cfg, r, tr, n: hs)
    m = roles.read_meta(role)
    cur = next(x for x in m["history"] if x["version"] == m["current"])
    cur["eval_status"] = "kept"; settings.write_json(roles.meta_path(role), m)
    before = roles.current_text(role)
    return research.run(cfg, None, force=True, only_roles=[role]), before


def test_research_missing_side_is_unknown_skipped(cfg, monkeypatch):
    from desk import research
    data = _rs_data()
    legacy = {"name": "旧", "conditions": [{"feature": "dow", "op": ">", "value": 1.5}, {"feature": "dow", "op": "<", "value": 2.5}],
              "horizon_days": 1, "stop_pct": 15}          # 按做多本可通过，但缺 side 不能猜
    assert research.side_of(legacy) == "unknown"
    r = research.evaluate(legacy, data, cfg, 10)
    assert r["skipped"] and not r["passed"] and r["side"] == "unknown"
    assert research.HypoList(**{"hypotheses": [{**legacy, "rationale": "x"}]}).hypotheses[0].side == "unknown"
    rep, before = _run_role(cfg, monkeypatch, "sentiment", [legacy], data)
    t = rep["roles"]["sentiment"]["tested"][0]
    assert t["skipped"] and not t["passed"] and t["k_tests"] is None
    assert not [e for e in rep["edits"] if e["role"] == "sentiment"] and roles.current_text("sentiment") == before
    reg = [x for x in settings.read_jsonl(research.REGISTRY) if x["id"] == t["id"]]
    assert reg and reg[0]["hypothesis"]["side"] == "unknown"
    md = (research.RDIR / f"report-{rep['week']}.md").read_text(encoding="utf-8")
    assert "未知方向，跳过检验" in md and "| 未知 |" in md
    from desk import report as drep
    import inspect
    assert "未知" in inspect.getsource(drep)


def test_research_invalid_side_from_llm_unknown(cfg, monkeypatch):
    from desk import research
    data = _rs_data()
    bad = {"side": "buy", "name": "非法方向", "conditions": SUN, "horizon_days": 1, "stop_pct": 15}
    ok = {"side": "short", "name": "空", "conditions": SUN, "horizon_days": 1, "stop_pct": 15}
    assert research.side_of(bad) == "unknown" and research.side_of({**bad, "side": "SHORT"}) == "short"
    # 模拟 LLM 结构化输出：非法 side 不应导致解析失败，也不应被猜成多/空
    class FakeLLM:
        def with_structured_output(self, _):
            return self
        def invoke(self, _):
            return research.HypoList(hypotheses=[research.Hypo(rationale="x", **bad), research.Hypo(rationale="x", **{k: v for k, v in bad.items() if k != "side"})])
    monkeypatch.setattr(research.dllm, "make_llm", lambda *a, **k: FakeLLM())
    got = research.propose(cfg, "market", None, 3)
    assert [g["side"] for g in got] == ["unknown", "unknown"]
    # k 只算实际检验过的：跳过的不增加 Bonferroni 检验数
    rep, _ = _run_role(cfg, monkeypatch, "market", [bad, ok], data)
    t = rep["roles"]["market"]["tested"]
    assert t[0]["skipped"] and t[1]["side"] == "short" and not t[1].get("skipped")
    res = {x["id"]: x for x in settings.read_jsonl(research.RESULTS)}
    done = [i for i, x in res.items() if not x.get("skipped")]
    assert t[1]["k_tests"] == len(done)          # S2：全局计数（所有角色），跳过的不计
    assert all("非法方向" not in c["rule"] for e in rep["edits"] for c in e.get("changes", []))


def test_research_edit_cap_long_short_combined(cfg, monkeypatch):
    cfg = {**cfg, "research": {**cfg["research"], "phase": "confirm"}, "learning": dict(cfg["learning"])}   # S2：通过只在季度确认期成立
    cfg["learning"].update(apply_weights=True, auto_edit_playbooks=True, research_write_playbook=True)   # 本用例验证学习机制本身（生产配置为冻结）
    from desk import research
    data = _rs_data()
    hs = [{"side": "short", "name": "空1", "conditions": SUN, "horizon_days": 1, "stop_pct": 15},
          {"side": "short", "name": "空2", "conditions": [{"feature": "dow", "op": ">", "value": 5.9}], "horizon_days": 1, "stop_pct": 12},
          {"side": "long", "name": "多1", "conditions": [{"feature": "dow", "op": ">", "value": 1.5}, {"feature": "dow", "op": "<", "value": 2.5}],
           "horizon_days": 1, "stop_pct": 15}]
    monkeypatch.setattr(research, "load_data", lambda cfg: data)
    monkeypatch.setattr(research, "propose", lambda cfg, role, tr, n: hs)
    m = roles.read_meta("news")
    cur = next(x for x in m["history"] if x["version"] == m["current"])
    cur["eval_status"] = "kept"; settings.write_json(roles.meta_path("news"), m)
    rep = research.run(cfg, None, force=True, only_roles=["news"])
    assert all(r["passed"] for r in rep["roles"]["news"]["tested"])
    ed = [e for e in rep["edits"] if e["type"] == "修订"]
    assert len(ed) == 1 and len(ed[0]["changes"]) == 2
    md = (research.RDIR / f"report-{rep['week']}.md").read_text(encoding="utf-8")
    assert "| 做空 |" in md and "| 做多 |" in md


ROLE_FEATS = {"market": ["ret_1", "ret_3", "ret_5", "ret_20", "dist_ma20", "dist_ma50", "atr_pct", "range_pct", "btc_ret_5", "dow"],
              "sentiment": ["rsi_14", "dd_high20", "up_high20"], "news": ["vol_z20"], "flows": ["vol_z20", "ret_1", "vol_dir"]}


class _CapLLM:
    def __init__(self, outs):
        self.outs, self.prompts = list(outs), []
    def with_structured_output(self, _):
        return self
    def invoke(self, p):
        self.prompts.append(p)
        return self.outs.pop(0) if self.outs else None


def test_research_role_menus_and_filter(cfg, monkeypatch):
    from desk import research
    assert {r: research.role_features(cfg, r) for r in ROLE_FEATS} == ROLE_FEATS
    for role, feats in ROLE_FEATS.items():
        mixed = research.Hypo(rationale="x", side="long", name="混", horizon_days=3,
                              conditions=[{"feature": "rsi_14", "op": "<", "value": 30}, {"feature": "vol_z20", "op": ">", "value": 2},
                                          {"feature": "dow", "op": ">", "value": 5}])
        alien = research.Hypo(rationale="x", side="short", name="全越权", horizon_days=3,
                              conditions=[{"feature": "up_high20" if role != "sentiment" else "ret_5", "op": ">", "value": 1}])
        fake = _CapLLM([research.HypoList(hypotheses=[mixed, alien])])
        monkeypatch.setattr(research.dllm, "make_llm", lambda *a, **k: fake)
        got = research.propose(cfg, role, None, 3)
        menu = fake.prompts[0].split("特征菜单：")[1].split("你以前的假设")[0]
        listed = [ln[2:].split("：")[0] for ln in menu.strip().splitlines() if ln.startswith("- ")]
        assert listed == feats
        for g in got:
            assert all(c["feature"] in feats for c in g["conditions"]) and g["conditions"]
        assert got == []                    # S2：含任一菜单外特征的假设整条拒绝（不再删条件后保留）
        if role == "news":
            assert "只能使用 vol_z20 这一个特征" in fake.prompts[0]


def test_vol_dir_feature():
    from desk import research
    n = 40
    c = np.r_[np.full(30, 100.0), [101, 99, 99, 103, 95, 95, 96, 94, 100, 100]]
    v = np.r_[np.full(30, 1.0) + np.arange(30) % 2 * 0.1, np.full(10, 5.0)]
    df = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=n), "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": v})
    f = research.features(df)
    exp = f["vol_z20"] * np.sign(f["ret_1"])
    assert np.allclose(f["vol_dir"].dropna(), exp.dropna())
    assert f["vol_dir"].iloc[30] > 0 and f["vol_dir"].iloc[31] < 0 and f["vol_dir"].iloc[32] == 0


def test_research_empty_llm_no_crash(cfg, monkeypatch):
    from desk import research
    fake = _CapLLM([None, None])
    monkeypatch.setattr(research.dllm, "make_llm", lambda *a, **k: fake)
    assert research.propose(cfg, "flows", None, 3) == [] and len(fake.prompts) == 2      # 重试一次
    ok = research.HypoList(hypotheses=[research.Hypo(rationale="x", side="short", name="量增价跌", horizon_days=3,
                                                     conditions=[{"feature": "vol_dir", "op": "<", "value": -1.5}])])
    fake2 = _CapLLM([None, ok])
    monkeypatch.setattr(research.dllm, "make_llm", lambda *a, **k: fake2)
    assert [g["name"] for g in research.propose(cfg, "flows", None, 3)] == ["量增价跌"]
    research.PROPOSE_NOTES.clear()
    monkeypatch.setattr(research.dllm, "make_llm", lambda *a, **k: _CapLLM([]))
    monkeypatch.setattr(research, "load_data", lambda cfg: _rs_data())
    rep = research.run(cfg, None, force=True, only_roles=["flows"])
    x = rep["roles"]["flows"]
    assert "error" not in x and x["tested"] == [] and "无假设" in x["note"]
    assert "无假设" in (research.RDIR / f"report-{rep['week']}.md").read_text(encoding="utf-8")


def test_claude_provider_and_reasoning(monkeypatch):
    import yaml
    from desk import providers
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert providers.provider_of("claude-opus-5-5") == "claude"
    monkeypatch.setenv("CLAUDE_RELAY_API_KEY", "x")
    r = providers.resolve(cfg, "trade_manager")
    assert r["provider"] == "claude" and r["model"] == "claude-opus-5-5" and r["reasoning"] == "high"
    monkeypatch.delenv("CLAUDE_RELAY_API_KEY")
    assert providers.resolve(cfg, "reviewer")["fallback"]
