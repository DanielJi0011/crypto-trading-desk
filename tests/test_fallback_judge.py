"""兜底市价补单前判定（desk/fallback_judge.py + entry_limit 钩子；全部 mock，不连网、不下单）。

覆盖：限价正常成交不调用判定 / 判定 proceed 照常补单 / 判定 abandon 不补单且事件带全事实 /
判定失败按未批准 abandon / 开关关闭恢复无条件兜底 / 做空镜像 / ETH 2026-11-11 回归事实包 /
判定结论落盘后重入不重复判定 / 现价取不到时 skip 放行。
"""
import json

import pytest

from desk import demo_exec as DX, entry_limit as EL, fallback_judge as FJ, okx_demo as X, settings

INST = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}


class Fake:
    """与 test_entry_limit 同款极简模拟盘 + 可配置现价 ticker。"""
    def __init__(self, child_state="live", acc="0", sz="1", existing_sl=None, pos=None, spot="65000"):
        self.posts = []
        self.child = {"ordId": "C1", "instId": INST, "state": child_state, "accFillSz": acc, "sz": sz}
        self.fallback = {}
        self.algos = list(existing_sl or [])
        self.pos = pos
        self.spot = spot

    def get(self, path, **kw):
        if path == "/api/v5/public/instruments":
            return {"data": [INSTR]}
        if path == "/api/v5/market/ticker":
            if self.spot is None:
                raise X.DemoError("ticker unavailable")
            return {"data": [{"last": str(self.spot)}]}
        if path == "/api/v5/trade/orders-algo-history":
            return {"data": [{"algoId": kw.get("algoId"), "state": "effective", "ordId": "C1", "triggerTime": "1000000"}]}
        if path == "/api/v5/trade/order":
            if kw.get("ordId") == "C1":
                return {"data": [dict(self.child)]}
            o = self.fallback.get(kw.get("clOrdId"))
            if o is None:
                raise X.DemoError("GET /api/v5/trade/order code=51603 msg=Order does not exist")
            return {"data": [o]}
        if path == "/api/v5/trade/orders-algo-pending":
            return {"data": [a for a in self.algos if a["ordType"] == kw.get("ordType")]}
        if path == "/api/v5/account/positions":
            return {"data": [self.pos] if self.pos else []}
        if path == "/api/v5/account/positions-history":
            return {"data": []}
        if path == "/api/v5/trade/orders-pending":
            return {"data": [dict(self.child, side="buy", posSide="long", reduceOnly="false")] if self.child["state"] in ("live", "partially_filled") else []}
        if path == "/api/v5/account/balance":
            return {"data": [{"details": [{"ccy": "USDT", "eq": "100000"}]}]}
        raise AssertionError(path)

    def post(self, path, body, **kw):
        self.posts.append((path, body))
        if path == "/api/v5/trade/cancel-order":
            self.child["state"] = "canceled"
            return {"data": [{}]}
        if path == "/api/v5/trade/order":
            self.fallback[body["clOrdId"]] = {"state": "filled", "accFillSz": body["sz"], "sz": body["sz"]}
            return {"data": [{"ordId": "F1"}]}
        if path == "/api/v5/trade/order-algo":
            return {"data": [{"algoId": "NEW"}]}
        if path == "/api/v5/trade/cancel-algos":
            return {"data": [{}]}
        raise AssertionError(path)

    def paths(self, p):
        return [b for x, b in self.posts if x == p]


class JudgeStub:
    """记录提示词的判定替身：result 可为 verdict 字符串、None（失败）或 Exception（抛出）。"""
    def __init__(self, result="proceed"):
        self.result = result
        self.prompts = []

    def __call__(self, cfg, prompt, inst):
        self.prompts.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None:
            return None
        return {"verdict": self.result, "reason": f"替身判定：{self.result}"}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(EL, "PATH", tmp_path / "el.json")
    monkeypatch.setattr(EL, "LOCK", tmp_path / "el.lock")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {})
    monkeypatch.setattr(DX, "DEMO_ACCOUNT", tmp_path / "none.json")
    from desk import equity_guard
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "marks.json")
    monkeypatch.setattr(EL, "_full_check", lambda cfg, e: [])
    monkeypatch.setattr(FJ, "_atr14_daily", lambda inst: None)   # 默认无 ATR；事实包用例单独覆写

    def make(**kw):
        f = Fake(**kw)
        monkeypatch.setattr(X, "get", f.get)
        monkeypatch.setattr(X, "post", f.post)
        return f
    return make


@pytest.fixture
def judge(monkeypatch):
    stub = JudgeStub()
    monkeypatch.setattr(FJ, "call_judge", stub)
    return stub


def _cfg(**el):
    cfg = settings.load_config()
    # 兜底判定只存在于市价兜底路径：本文件全部用例钉 fallback_market=True（config 默认已改为 false=等触发）
    cfg["execution"]["entry_limit"] = {**cfg["execution"].get("entry_limit", {}), "fallback_market": True, **el}
    cfg["guardrails"]["kill_switch_file"] = "KILL_TEST_NONEXISTENT"
    return cfg


def _reg(inst=INST, side="long", sz="1"):
    if side == "long":
        EL.register("A1", inst=inst, side="long", pos_side="long", sz=sz, order_px="64935", trigger_px=65000,
                    stop_loss=60000, take_profit=70000, attach_cl="taold", lever=1, decision_id="d")
    else:
        EL.register("A1", inst=inst, side="short", pos_side="short", sz=sz, order_px="65065", trigger_px=65000,
                    stop_loss=70000, take_profit=60000, attach_cl="taold", lever=1, decision_id="d")


def _events(tmp_path):
    p = tmp_path / "ev.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


T0 = 1000000
LATE = T0 + 4 * 60000


# ---------------- 不触发判定的路径
def test_limit_filled_judge_never_called(fake, judge):
    f = fake(child_state="filled", acc="1"); _reg()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "filled"
    assert judge.prompts == [] and f.posts == []


def test_judge_disabled_unconditional_fallback(fake, judge):
    f = fake(child_state="live", acc="0"); _reg()
    judge.result = "abandon"   # 即使替身会否决，开关关闭时也不该被调用
    assert dict(EL.check_timeouts(_cfg(fallback_judge_enabled=False), now_ms=LATE))["A1"] == "fallback_done"
    assert judge.prompts == [] and len(f.paths("/api/v5/trade/order")) == 1


# ---------------- proceed / abandon / failed
def test_judge_proceed_places_market_order(fake, judge):
    f = fake(child_state="live", acc="0"); _reg()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "fallback_done"
    assert len(judge.prompts) == 1
    m = f.paths("/api/v5/trade/order")
    assert len(m) == 1 and m[0]["ordType"] == "market"
    e = EL._load()["entries"]["A1"]
    assert e["fallback_judge"]["verdict"] == "proceed"


def test_judge_abandon_no_order_and_event_facts(fake, judge, tmp_path):
    f = fake(child_state="live", acc="0", spot="64000"); _reg()
    judge.result = "abandon"
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "abandoned"
    assert f.paths("/api/v5/trade/order") == []                       # 没发市价单
    assert f.paths("/api/v5/trade/cancel-order")                        # 限价子单已照常撤销
    e = EL._load()["entries"]["A1"]
    assert e["status"] == "done" and e["result"] == "abandoned" and e["fallback_judge"]["verdict"] == "abandon"
    ab = [x for x in _events(tmp_path) if x["event"] == "fallback_abandoned"]
    assert len(ab) == 1
    assert ab[0]["spot"] == 64000.0 and ab[0]["trigger_px"] == 65000.0 and ab[0]["judge_outcome"] == "abandon"
    assert ab[0]["planned_rr"] == pytest.approx(1.0) and ab[0]["effective_rr"] == pytest.approx(1.5) and ab[0]["reason"]


def test_judge_abandon_partial_fill_still_protects(fake, judge):
    f = fake(child_state="partially_filled", acc="0.4", pos={"instId": INST, "posSide": "long", "pos": "0.4"})
    _reg(); judge.result = "abandon"
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "abandoned"
    assert f.paths("/api/v5/trade/order") == []
    oco = f.paths("/api/v5/trade/order-algo")          # 已成交 0.4 张照常挂保护（与 blocked 收尾一致）
    assert len(oco) == 1 and oco[0]["sz"] == "0.4" and oco[0]["reduceOnly"] is True


def test_judge_failure_treated_as_abandon(fake, judge, tmp_path):
    f = fake(child_state="live", acc="0"); _reg()
    judge.result = None                                # 调用失败/不可解析
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "abandoned"
    assert f.paths("/api/v5/trade/order") == []
    kinds = [x["event"] for x in _events(tmp_path)]
    assert "fallback_judge_failed" in kinds and "fallback_abandoned" in kinds
    ab = [x for x in _events(tmp_path) if x["event"] == "fallback_abandoned"][0]
    assert ab["judge_outcome"] == "failed"


def test_judge_exception_treated_as_failure(fake, judge, tmp_path):
    fake(child_state="live", acc="0"); _reg()
    judge.result = RuntimeError("boom")                # 替身抛错也要走 failed→abandon（call_judge 内部吞错返回 None 的等价路径由 judge_fallback 承担）
    # 真实 call_judge 对异常返回 None；此处直接验证 judge_fallback 的失败语义
    out, facts, j = FJ.judge_fallback(_cfg(), EL._load()["entries"]["A1"], filled=0.0, remain=1.0, now_ms=LATE)
    assert out == "failed" and j is None and facts["inst"] == INST


def test_spot_unavailable_skips_and_proceeds(fake, judge, tmp_path):
    f = fake(child_state="live", acc="0", spot=None); _reg()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "fallback_done"
    assert judge.prompts == [] and len(f.paths("/api/v5/trade/order")) == 1
    assert "fallback_judge_skipped" in [x["event"] for x in _events(tmp_path)]


# ---------------- 做空镜像
def test_short_side_mirror_proceed(fake, judge):
    f = fake(child_state="live", acc="0", spot="64000"); _reg(side="short")
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "fallback_done"
    m = f.paths("/api/v5/trade/order")
    assert len(m) == 1 and m[0]["side"] == "sell" and m[0]["posSide"] == "short"
    assert "做空" in judge.prompts[0]


# ---------------- 结论落盘后重入不重复判定
def test_recorded_proceed_reentry_no_rejudge(fake, judge):
    f = fake(child_state="live", acc="0"); _reg()
    EL.check_timeouts(_cfg(), now_ms=LATE)
    assert len(judge.prompts) == 1 and len(f.paths("/api/v5/trade/order")) == 1
    # 模拟崩溃重入：状态停在 filling、结论已落盘、补单其实未下 → 直接补单，不再判定
    s = EL._load(); s["entries"]["A1"]["status"] = "filling"; EL._save(s)
    f.fallback.clear()
    n0 = len(f.paths("/api/v5/trade/order"))
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE + 1))["A1"] in ("fallback_done", "protecting")
    assert len(judge.prompts) == 1 and len(f.paths("/api/v5/trade/order")) == n0 + 1


# ---------------- 事实包（ETH 2026-11-11 回归）
def test_eth_regression_facts(fake, monkeypatch):
    monkeypatch.setattr(FJ, "_atr14_daily", lambda inst: 208.5)
    e = {"inst": ETH, "side": "short", "pos_side": "short", "sz": "84.2", "order_px": "2077.07",
         "trigger_px": 2075, "stop_loss": 2126, "take_profit": 1985, "triggered_ms": T0, "decision_id": "d-eth"}
    f = FJ.build_facts(_cfg(), e, filled=0.0, remain=84.2, spot=2055.74, now_ms=T0 + 192000)
    assert f["planned_rr"] == pytest.approx(90 / 51, abs=1e-3)            # 计划 1.76
    assert f["effective_rr"] == pytest.approx(70.74 / 70.26, abs=1e-3)    # 实际 ≈1.007
    assert f["worse_amount"] == pytest.approx(19.26, abs=1e-6)            # 做空：现价低于触发价即恶化
    assert f["dev_pct"] == pytest.approx(-0.928, abs=1e-3)
    assert f["dev_atr"] == pytest.approx(19.26 / 208.5, abs=1e-3)
    assert f["elapsed_secs"] == 192.0
    p = FJ.judge_prompt(f)
    assert "2075" in p and "2055.74" in p and "1.765" in p and "1.007" in p and "交易经理" in p


def test_eth_regression_abandon_via_prompt(fake, judge, tmp_path):
    fake(child_state="live", acc="0", sz="84.2", spot="2055.74")
    EL.register("A1", inst=ETH, side="short", pos_side="short", sz="84.2", order_px="2077.07", trigger_px=2075,
                stop_loss=2126, take_profit=1985, attach_cl="taold", lever=1, decision_id="d-eth")
    judge.result = "abandon"
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "abandoned"
    assert "1.007" in judge.prompts[0]
    ab = [x for x in _events(tmp_path) if x["event"] == "fallback_abandoned"][0]
    assert ab["effective_rr"] == pytest.approx(70.74 / 70.26, abs=1e-3)


# ---------------- 判定管道本身（mock LLM 真实路径）
def test_normalize_judgment():
    assert FJ.normalize_judgment({"verdict": "proceed", "reason": "漂移小"})["verdict"] == "proceed"
    assert FJ.normalize_judgment({"verdict": "hold", "reason": "x"}) is None
    assert FJ.normalize_judgment("proceed") is None
    assert FJ.normalize_judgment(None) is None


def test_call_judge_mock_llm_proceeds(fake):
    # conftest 已设 TA_DESK_MOCK_LLM=1：走 make_llm quick 通道 + FakeChatModel 结构化输出（Literal 首值=proceed）
    j = FJ.call_judge(_cfg(), "（测试提示词）", INST)
    assert j is not None and j["verdict"] == "proceed"
