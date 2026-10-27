"""三币并行分析（parallel.enabled）：简报隔离、并行/串行等价、决策顺序固定、单币失败隔离、jsonl 不交错、开关关闭不变。
全部在 TA_DESK_MOCK_LLM=1 + 临时 TA_DESK_HOME 下运行，不调真实模型、不碰模拟盘。"""
from __future__ import annotations

import json
import re
import threading
import time

import pytest

from desk import settings

COINS3 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


# ---------------------------------------------------------------- 简报隔离（单元）
def test_briefs_contextvar_isolated_across_threads():
    import contextvars
    from desk import roles
    bar = threading.Barrier(3)
    got, errs = {}, []

    def work(c):
        roles.set_briefs({"*": f"共享-{c}", "bull": f"资金流-{c}"})
        bar.wait()                       # 三线程都写完后再读，原模块级 dict 会互相覆盖
        time.sleep(0.05)
        got[c] = (roles.BRIEFS.get("*"), roles.BRIEFS.get("bull"), "bull" in roles.BRIEFS)
        roles.set_briefs({})
        if roles.BRIEFS.get("*"):
            errs.append(c)
    ts = [threading.Thread(target=contextvars.copy_context().run, args=(work, c)) for c in ("BTC", "ETH", "SOL")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    for c in ("BTC", "ETH", "SOL"):
        assert got[c] == (f"共享-{c}", f"资金流-{c}", True)
    assert roles.BRIEFS.get("*") is None   # 主线程上下文不受影响


def test_set_briefs_serial_semantics_unchanged():
    from desk import roles
    roles.set_briefs({"bull": "x", "bear": ""})
    assert roles.BRIEFS.get("bull") == "x" and "bear" not in roles.BRIEFS and len(roles.BRIEFS) == 1
    roles.BRIEFS.clear()
    assert roles.BRIEFS.get("bull") is None and len(roles.BRIEFS) == 0


# ---------------------------------------------------------------- 真实图（模拟 LLM）三币同时分析：每个角色只看到本币简报
def _book_all():
    pos = []
    for inst, px in (("BTC-USDT-SWAP", 80000), ("ETH-USDT-SWAP", 3000), ("SOL-USDT-SWAP", 150)):
        pos.append({"inst_id": inst, "ticker": inst.split("-")[0] + "-USD", "qty": 1, "side": "long", "pos_side": "long",
                    "avg_px": px, "contracts": 1, "sl_px": px * 0.9, "tp_px": px * 1.2, "upnl": 1.0})
    return {"positions": pos, "cash": 10000.0, "triggers": []}


def test_parallel_analyze_real_graph_briefs_isolated(monkeypatch):
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    from desk import analysis, llm as dllm
    cfg = settings.load_config()
    seen, lock = [], threading.Lock()
    orig = dllm.FakeChatModel._generate

    def spy(self, messages, *a, **k):
        sys0 = str(messages[0].content) if messages else ""
        with lock:
            seen.append((dllm.STAGE.get(), sys0))
        return orig(self, messages, *a, **k)
    monkeypatch.setattr(dllm.FakeChatModel, "_generate", spy)
    tracker = dllm.UsageTracker(cfg)
    analysis.build_graph(cfg, tracker)
    coins = [c for c in cfg["coins"] if c["inst_id"] in COINS3]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(contextvars.copy_context().run, analysis.analyze, cfg, c, "2026-10-19", _book_all(), tracker) for c in coins]
        res = [f.result() for f in futs]
    assert all(r["final_state"] for r in res)
    mine = re.compile(r"- (\S+-USDT-SWAP)：[^\n]*（本币）")
    checked = 0
    for stage, sys0 in seen:
        if "[交易台共享信息]" not in sys0:
            continue
        ccy = stage.split("/")[0].split("-")[0]
        marks = set(mine.findall(sys0))
        assert ccy in ("BTC", "ETH", "SOL") and marks == {f"{ccy}-USDT-SWAP"}, (stage, marks)
        checked += 1
    assert checked >= 3 * 10   # 每币图内大部分角色都带共享简报


# ---------------------------------------------------------------- jsonl / 用量记录并发写不交错
def test_append_jsonl_threads_no_interleave(tmp_path):
    p = tmp_path / "x.jsonl"
    big = "数" * 20000

    def w(i):
        for j in range(100):
            settings.append_jsonl(p, {"t": i, "j": j, "pad": big})
    ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 600 and all(r["pad"] == big for r in rows)


def test_usage_tracker_concurrent(monkeypatch, tmp_path):
    from desk import llm as dllm
    cfg = settings.load_config()
    tr = dllm.UsageTracker(cfg)
    tr.path = tmp_path / "u.jsonl"
    m = dllm.FakeChatModel(tier="quick", callbacks=[tr])

    def w():
        for _ in range(30):
            m.invoke("hi")
    ts = [threading.Thread(target=w) for _ in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    rows = [json.loads(l) for l in tr.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 150 and tr.summary()["calls"] == 150 and not tr._node


def test_x_feed_missing_alert_once_under_threads(monkeypatch):
    from desk import analysis
    import desk.demo_ws as dw
    sent = []
    monkeypatch.setattr(dw, "notify", lambda ev: sent.append(ev))
    res = []
    bar = threading.Barrier(3)

    def w(c):
        bar.wait()
        res.append(analysis.x_feed_missing_alert("2031-01-02", c))
    ts = [threading.Thread(target=w, args=(c,)) for c in ("BTC", "ETH", "SOL")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(res) == [False, False, True] and len(sent) == 1


# ---------------------------------------------------------------- run_daily 编排（替身分析/决策）
@pytest.fixture
def harness(monkeypatch):
    """替换 run_daily 的重活：记录调用顺序、线程；其余步骤（打分/复盘/研究/日报）打桩。"""
    from desk import (analysis, baselines, demo_trades, ledger, macro, manager, okx_data, report, research, review,
                      scoring, stances)
    from desk import run_daily
    H = {"events": [], "lock": threading.Lock(), "parallel": False, "delays": {}, "fail": set(), "report": None,
         "acct": []}
    real_load = settings.load_config

    def load():
        c = real_load()
        c["parallel"] = {"enabled": H["parallel"]}
        return c
    monkeypatch.setattr(settings, "load_config", load)

    def ev(*x):
        with H["lock"]:
            H["events"].append(x)
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 100.0)
    monkeypatch.setattr(ledger, "book", lambda cfg, prices=None: ev("book", tuple(H["acct"])) or {"positions": [], "cash": 1.0, "acct": tuple(H["acct"])})
    monkeypatch.setattr(analysis, "build_graph", lambda cfg, tr: None)

    def analyze(cfg, coin, date, bk, tracker, extra_brief=""):
        inst = coin["inst_id"]
        ev("analyze_start", inst, threading.current_thread() is threading.main_thread())
        time.sleep(H["delays"].get(inst, 0.2))
        if inst in H["fail"]:
            raise RuntimeError(f"模拟 {inst} 分析失败")
        ev("analyze_end", inst)
        return {"final_state": {"inst": inst, "seen_acct": bk["acct"]}, "signal": "Hold", "report_dir": f"/r/{inst}"}
    monkeypatch.setattr(analysis, "analyze", analyze)

    def extract(fs, cfg, tracker):
        from desk import llm as dllm
        return {"stage": dllm.STAGE.get(), "inst": fs["inst"]}
    monkeypatch.setattr(stances, "extract", extract)

    def decide(cfg, coin, date, fs, st, tracker):
        inst = coin["inst_id"]
        ev("decide", inst, threading.current_thread() is threading.main_thread(), tuple(H["acct"]))
        H["acct"].append(inst)   # 模拟下单改变账户：后面的币决策时必须看到
        return {"inst_id": inst, "decision_id": f"D-x-{inst[:3]}", "consensus": 0.1, "final_action": "hold",
                "stances": st, "seen_acct": fs["seen_acct"], "ap": fs.get("_analysis_px")}
    monkeypatch.setattr(manager, "decide_and_execute", decide)
    monkeypatch.setattr(scoring, "record_stances", lambda cfg, rec: ev("record", rec["inst_id"]))
    monkeypatch.setattr(scoring, "score_due", lambda cfg: None)
    monkeypatch.setattr(scoring, "update_weights", lambda cfg: [])
    monkeypatch.setattr(macro, "safe_get", lambda cfg, d: None)
    monkeypatch.setattr(demo_trades, "review_candidates", lambda *a, **k: [])
    monkeypatch.setattr(review, "evaluate_and_rollback", lambda cfg: [])
    monkeypatch.setattr(review, "maybe_edit_playbooks", lambda cfg, tr: [])
    monkeypatch.setattr(research, "run", lambda *a, **k: {"skipped": True})
    monkeypatch.setattr(baselines, "update", lambda *a, **k: None)

    def rep(cfg, date, decisions, closed, reviews, wc, pb, usage, prices, errors, **k):
        H["report"] = {"decisions": [dict(d) for d in decisions], "errors": list(errors)}
        return "/dev/null"
    monkeypatch.setattr(report, "write", rep)

    def run():
        H["events"].clear(); H["acct"].clear(); H["report"] = None
        rc = run_daily.main(["--mock", "--date", "2026-10-19", "--report-name", "par-test"])
        return rc, H["report"]
    H["run"] = run
    return H


def _order(H, kind):
    return [e[1] for e in H["events"] if e[0] == kind]


def test_parallel_disabled_is_original_serial(harness):
    harness["parallel"] = False
    rc, rep = harness["run"]()
    assert rc == 0
    kinds = [(e[0], e[1] if e[0] != "book" else None) for e in harness["events"] if e[0] != "analyze_end"]
    exp = []
    for i in COINS3:
        exp += [("book", None), ("analyze_start", i), ("decide", i), ("record", i)]
    assert kinds == exp       # 逐币 读账户→分析→决策→记录，与原串行一致
    assert all(e[2] for e in harness["events"] if e[0] == "analyze_start")   # 全在主线程
    # 串行下后面的币分析时看到前面币已执行后的账户
    assert [d["seen_acct"] for d in rep["decisions"]] == [(), (COINS3[0],), tuple(COINS3[:2])]


def test_parallel_overlaps_and_keeps_decision_order(harness):
    harness["parallel"] = True
    harness["delays"] = {"BTC-USDT-SWAP": 0.6, "ETH-USDT-SWAP": 0.4, "SOL-USDT-SWAP": 0.1}   # SOL 最先算完
    t0 = time.monotonic(); rc, rep = harness["run"](); dt = time.monotonic() - t0
    assert rc == 0
    assert _order(harness, "analyze_end")[0] == "SOL-USDT-SWAP"
    assert _order(harness, "decide") == COINS3 and _order(harness, "record") == COINS3
    assert all(e[2] for e in harness["events"] if e[0] == "decide")           # 决策下单在主线程
    assert not any(e[2] for e in harness["events"] if e[0] == "analyze_start")  # 分析在工作线程
    # 决策时账户按串行顺序累积（护栏在执行时重读账户）
    assert [e[3] for e in harness["events"] if e[0] == "decide"] == [(), (COINS3[0],), tuple(COINS3[:2])]
    # 三路同时启动（不错开、不设上限）：总耗时≈最慢币，而不是总和 1.1 秒
    assert dt < 1.0
    assert [d["inst_id"] for d in rep["decisions"]] == COINS3
    assert [d["stances"]["stage"] for d in rep["decisions"]] == [f"{i}/立场抽取" for i in COINS3]   # STAGE 在线程内正确设置


def test_parallel_and_serial_same_output(harness):
    out = {}
    for flag in (False, True):
        harness["parallel"] = flag
        rc, rep = harness["run"]()
        assert rc == 0
        out[flag] = [{k: v for k, v in d.items() if k not in ("seen_acct", "ap")} for d in rep["decisions"]], rep["errors"]
    assert out[False] == out[True]


@pytest.mark.parametrize("flag", [False, True])
def test_single_coin_failure_isolated(harness, flag):
    harness["parallel"] = flag
    harness["fail"] = {"ETH-USDT-SWAP"}
    rc, rep = harness["run"]()
    assert rc == 0
    assert [d["inst_id"] for d in rep["decisions"]] == ["BTC-USDT-SWAP", "SOL-USDT-SWAP"]
    errs = [e for e in rep["errors"] if "分析/决策失败" in e]
    assert errs == ["ETH-USDT-SWAP 分析/决策失败：RuntimeError: 模拟 ETH-USDT-SWAP 分析失败"]


def test_parallel_single_coin_uses_serial_path(harness):
    from desk import run_daily
    harness["parallel"] = True
    rc = run_daily.main(["--mock", "--coins", "BTC", "--date", "2026-10-19", "--report-name", "par-test"])
    assert rc == 0 and all(e[2] for e in harness["events"] if e[0] == "analyze_start")


def test_config_default_parallel_off():
    """代码默认关闭（缺省 parallel 段 → 串行）；config.yaml 已按用户同意于 2026-10-28 开启。"""
    import yaml
    raw = yaml.safe_load(settings.CONFIG.read_text(encoding="utf-8"))
    assert raw["parallel"]["enabled"] is True
    from desk import run_daily
    assert run_daily.parallel_enabled({}) is False and run_daily.parallel_enabled(raw) is True


@pytest.mark.parametrize("par", [False, True])
def test_analysis_px_recorded_both_paths(harness, par):
    """漂移检查参考价：串行/并行两路都在分析开始时记 analysis_px（含时间）并传给 decide_and_execute。"""
    harness["parallel"] = par
    rc, rep = harness["run"]()
    assert rc == 0 and len(rep["decisions"]) == len(COINS3)
    for d in rep["decisions"]:
        assert d["ap"]["px"] == 100.0 and d["ap"]["time"]
