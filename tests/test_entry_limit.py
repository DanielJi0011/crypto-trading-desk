"""限价入场 + 超时撤单市价补单（全部 mock 交易所 HTTP，不连网、不下单）。"""
import pytest

from desk import demo_exec as DX, entry_limit as EL, okx_demo as X, settings

INST = "BTC-USDT-SWAP"
INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}


class Fake:
    """极简模拟盘：按 path 路由，记录所有 POST。"""
    def __init__(self, child_state="live", acc="0", sz="1", existing_sl=None, pos=None):
        self.posts = []
        self.child = {"ordId": "C1", "instId": INST, "state": child_state, "accFillSz": acc, "sz": sz}
        self.fallback = {}
        self.algos = list(existing_sl or [])
        self.pos = pos

    def get(self, path, **kw):
        if path == "/api/v5/public/instruments":
            return {"data": [INSTR]}
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
        if path == "/api/v5/trade/orders-pending":   # R12：补单护栏查同币其他在途入场子单
            return {"data": [dict(self.child, side="buy", posSide="long", reduceOnly="false")] if self.child["state"] in ("live", "partially_filled") else []}
        if path == "/api/v5/account/balance":   # R5：补单护栏读实时权益（fail-closed）
            return {"data": [{"details": [{"ccy": "USDT", "eq": "100000"}]}]}
        raise AssertionError(path)

    def post(self, path, body, **kw):
        self.posts.append((path, body))
        if path == "/api/v5/trade/cancel-order":
            self.child["state"] = "canceled" if float(self.child["accFillSz"]) == 0 else "canceled"
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


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(EL, "PATH", tmp_path / "el.json")
    monkeypatch.setattr(EL, "LOCK", tmp_path / "el.lock")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {})
    monkeypatch.setattr(DX, "DEMO_ACCOUNT", tmp_path / "none.json")
    from desk import equity_guard
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "marks.json")
    monkeypatch.setattr(EL, "_full_check", lambda cfg, e: [])   # R6 完整风控单独在 test_batch2_rest 测

    def make(**kw):
        f = Fake(**kw)
        monkeypatch.setattr(X, "get", f.get)
        monkeypatch.setattr(X, "post", f.post)
        return f
    return make


def _cfg(**el):
    cfg = settings.load_config()
    cfg["execution"]["entry_limit"] = {**cfg["execution"].get("entry_limit", {}), **el}
    cfg["guardrails"]["kill_switch_file"] = "KILL_TEST_NONEXISTENT"
    return cfg


def _reg(sz="1"):
    EL.register("A1", inst=INST, side="long", pos_side="long", sz=sz, order_px="64935", trigger_px=65000,
                stop_loss=60000, take_profit=70000, attach_cl="taold", lever=1, decision_id="d")


T0 = 1000000
LATE = T0 + 4 * 60000


# ---------------- 限价计算
def test_limit_px_direction_and_tick():
    assert DX.entry_limit_px("long", 65000, 0.1, 0.1) == 64935.0
    assert DX.entry_limit_px("short", 65000, 0.1, 0.1) == 65065.0
    assert DX.entry_limit_px("long", 108.63, 0.1, 0.01) == 108.52      # 108.52137 → 108.52
    assert DX.entry_limit_px("short", 108.63, 0.1, 0.01) == 108.74     # 108.73863 → 108.74
    assert DX.entry_limit_px("long", 2.3456, 0.1, 0.001) == 2.343


def _place(monkeypatch, enabled):
    posts = []
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "check_open", lambda *a, **k: ([], {}))
    monkeypatch.setattr(DX, "cancel_triggers", lambda *a, **k: [])
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "_ps", lambda side: "net")
    monkeypatch.setattr(DX, "_set_lev", lambda *a: None)
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    monkeypatch.setattr(X, "instrument", lambda inst: INSTR)
    monkeypatch.setattr(X, "post", lambda p, b: posts.append((p, b)) or {"data": [{"algoId": "Z9"}]})
    o = {"side": "short", "trigger_px": 65000, "size_pct": 10, "leverage": 1, "stop_loss": 68000, "take_profit": 60000}
    ok, _ = DX.place_trigger(_cfg(enabled=enabled), {"equity": 100000, "triggers": []}, INST, o)
    assert ok
    return [b for p, b in posts if p == "/api/v5/trade/order-algo"][0]


def test_place_trigger_disabled_market(fake, monkeypatch):
    b = _place(monkeypatch, False)
    assert b["orderPx"] == "-1" and b["ordType"] == "trigger"
    assert b["attachAlgoOrds"][0]["slOrdPx"] == "-1" and b["attachAlgoOrds"][0]["tpOrdPx"] == "-1"
    assert "Z9" not in EL._load()["entries"]


def test_place_trigger_enabled_limit(fake, monkeypatch):
    b = _place(monkeypatch, True)
    assert b["orderPx"] == "65065"     # 空：65000×1.001
    assert b["attachAlgoOrds"][0]["slOrdPx"] == "-1" and b["attachAlgoOrds"][0]["tpOrdPx"] == "-1"
    assert EL._load()["entries"]["Z9"]["status"] == "armed"


# ---------------- 超时
def test_not_timed_out_waits(fake):
    f = fake(); _reg()
    assert dict(EL.check_timeouts(_cfg(), now_ms=T0 + 60000))["A1"] == "waiting"
    assert f.posts == []


def test_timeout_cancel_and_market_fill_with_attach(fake):
    f = fake(child_state="live", acc="0"); _reg()
    assert dict(EL.check_timeouts(_cfg(fallback_market=True), now_ms=LATE))["A1"] == "fallback_done"
    assert f.paths("/api/v5/trade/cancel-order") == [{"instId": INST, "ordId": "C1"}]
    m = f.paths("/api/v5/trade/order")
    assert len(m) == 1 and m[0]["ordType"] == "market" and m[0]["sz"] == "1" and m[0]["side"] == "buy" and m[0]["posSide"] == "long"
    att = m[0]["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "60000" and att["tpTriggerPx"] == "70000" and att["slOrdPx"] == "-1" and att["tpOrdPx"] == "-1"


def test_fully_filled_no_fallback(fake):
    f = fake(child_state="filled", acc="1"); _reg()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "filled"
    assert f.posts == []


def test_repeat_calls_no_double_fill(fake):
    f = fake(child_state="live", acc="0"); _reg()
    EL.check_timeouts(_cfg(fallback_market=True), now_ms=LATE); EL.check_timeouts(_cfg(fallback_market=True), now_ms=LATE + 1)
    assert len(f.paths("/api/v5/trade/order")) == 1
    # 中断重入：状态停在 filling 且补单其实已下 → 不再下第二张
    s = EL._load(); s["entries"]["A1"]["status"] = "filling"; EL._save(s)
    EL.check_timeouts(_cfg(fallback_market=True), now_ms=LATE + 2)
    assert len(f.paths("/api/v5/trade/order")) == 1
    # 拿不到锁时什么都不做
    with EL._locked():
        assert EL.check_timeouts(_cfg(), now_ms=LATE + 3) == []


def test_partial_fill_sltp_covers_total(fake):
    old = {"algoId": "OLD", "ordType": "oco", "instId": INST, "posSide": "long", "side": "sell", "sz": "0.4", "algoClOrdId": "taold"}
    f = fake(child_state="partially_filled", acc="0.4", existing_sl=[old],
             pos={"instId": INST, "posSide": "long", "pos": "1"})
    _reg()
    assert dict(EL.check_timeouts(_cfg(fallback_market=True), now_ms=LATE))["A1"] == "fallback_done"
    m = f.paths("/api/v5/trade/order")
    assert len(m) == 1 and m[0]["sz"] == "0.6" and "attachAlgoOrds" not in m[0]
    oco = f.paths("/api/v5/trade/order-algo")
    assert len(oco) == 1 and oco[0]["sz"] == "1" and oco[0]["ordType"] == "oco" and oco[0]["reduceOnly"] is True
    assert oco[0]["slOrdPx"] == "-1" and oco[0]["tpOrdPx"] == "-1"
    # 先挂新 OCO 再撤旧的：不出现裸仓
    order = [p for p, _ in f.posts]
    assert order.index("/api/v5/trade/order-algo") < order.index("/api/v5/trade/cancel-algos")
    assert f.paths("/api/v5/trade/cancel-algos") == [[{"instId": INST, "algoId": "OLD"}]]


def test_kill_blocks_fallback_but_protects(fake, tmp_path, monkeypatch):
    f = fake(child_state="partially_filled", acc="0.4", pos={"instId": INST, "posSide": "long", "pos": "0.4"})
    _reg()
    from desk import ledger
    k = tmp_path / "KILL"; k.write_text("x")
    monkeypatch.setattr(settings, "KILL_PATH", k)
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "blocked"
    assert f.paths("/api/v5/trade/order") == []
    assert f.paths("/api/v5/trade/order-algo")[0]["sz"] == "0.4"   # 已成交部分仍有止损


def test_ws_records_trigger_time(fake):
    fake(); _reg()
    assert EL.record_trigger("A1", ms=T0 + 5) and not EL.record_trigger("A1", ms=T0 + 9)
    assert not EL.record_trigger("UNKNOWN")
    assert EL._load()["entries"]["A1"]["triggered_ms"] == T0 + 5


# ---------------- 等待/重挂（fallback_market=false，2026-11-11 用户决定：取消市价兜底，就等它触发）
import json as _json

EXP = "2099-01-01 00:00:00+0800"


class RearmFake:
    """带现价、且新算法单 id 可区分的模拟盘（重挂断言用）。"""
    def __init__(self, spot="64000", child_state="live", acc="0", sz="1", pos=None):
        self.posts = []
        self.spot = spot
        self.child = {"ordId": "C1", "instId": INST, "state": child_state, "accFillSz": acc, "sz": sz}
        self.pos = pos
        self.n = 0

    def get(self, path, **kw):
        if path == "/api/v5/public/instruments":
            return {"data": [INSTR]}
        if path == "/api/v5/market/ticker":
            return {"data": [{"last": self.spot}]}
        if path == "/api/v5/trade/orders-algo-history":
            if kw.get("algoId") == "A1":
                return {"data": [{"algoId": "A1", "state": "effective", "ordId": "C1", "triggerTime": "1000000"}]}
            return {"data": []}
        if path == "/api/v5/trade/order":
            if kw.get("ordId") == "C1":
                return {"data": [dict(self.child)]}
            raise X.DemoError("GET /api/v5/trade/order code=51603 msg=Order does not exist")
        if path == "/api/v5/trade/orders-algo-pending":
            return {"data": []}
        if path == "/api/v5/account/positions":
            return {"data": [self.pos] if self.pos else []}
        if path == "/api/v5/account/positions-history":
            return {"data": []}
        if path == "/api/v5/trade/orders-pending":
            return {"data": [dict(self.child, side="buy", posSide="long", reduceOnly="false")]
                    if self.child["state"] in ("live", "partially_filled") else []}
        if path == "/api/v5/account/balance":
            return {"data": [{"details": [{"ccy": "USDT", "eq": "100000"}]}]}
        raise AssertionError(path)

    def post(self, path, body, **kw):
        self.posts.append((path, body))
        if path == "/api/v5/trade/cancel-order":
            self.child["state"] = "canceled"
            return {"data": [{}]}
        if path == "/api/v5/trade/order-algo":
            self.n += 1
            return {"data": [{"algoId": f"N{self.n}"}]}
        if path == "/api/v5/trade/cancel-algos":
            return {"data": [{}]}
        if path == "/api/v5/trade/order":
            return {"data": [{"ordId": "M1"}]}
        raise AssertionError(path)

    def paths(self, p):
        return [b for x, b in self.posts if x == p]

    def triggers(self):
        return [b for b in self.paths("/api/v5/trade/order-algo") if b.get("ordType") == "trigger"]

    def ocos(self):
        return [b for b in self.paths("/api/v5/trade/order-algo") if b.get("ordType") in ("oco", "conditional")]


@pytest.fixture
def rfake(monkeypatch, tmp_path):
    monkeypatch.setattr(EL, "PATH", tmp_path / "el.json")
    monkeypatch.setattr(EL, "LOCK", tmp_path / "el.lock")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {})
    monkeypatch.setattr(DX, "DEMO_ACCOUNT", tmp_path / "none.json")
    monkeypatch.setattr(DX, "REG", tmp_path / "demo_orders.json")
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "_ps", lambda side: side)   # 真实 _ps 会查 /account/config 的 posMode，Fake 不路由
    from desk import entry_state, equity_guard
    monkeypatch.setattr(entry_state, "PATH", tmp_path / "entry_states.json")
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "marks.json")
    monkeypatch.setattr(EL, "_full_check", lambda cfg, e: [])

    def make(**kw):
        f = RearmFake(**kw)
        monkeypatch.setattr(X, "get", f.get)
        monkeypatch.setattr(X, "post", f.post)
        return f
    return make


def _reg2(side="long", cond="gte", expires=EXP, sz="1"):
    if side == "long":
        EL.register("A1", inst=INST, side="long", pos_side="long", sz=sz, order_px="64935", trigger_px=65000,
                    stop_loss=60000, take_profit=70000, attach_cl="taold", lever=1, decision_id="d",
                    condition=cond, expires=expires)
    else:
        EL.register("A1", inst=INST, side="short", pos_side="short", sz=sz, order_px="65065", trigger_px=65000,
                    stop_loss=70000, take_profit=60000, attach_cl="taold", lever=1, decision_id="d",
                    condition=cond, expires=expires)


def _events(tmp_path):
    p = tmp_path / "ev.jsonl"
    return [_json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def test_rearm_when_price_back_on_waiting_side(rfake, tmp_path):
    f = rfake(spot="64000")   # 做多 gte 突破单触发后价格跌回触发价下方 = 回到等待侧
    _reg2()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "rearmed"
    assert f.paths("/api/v5/trade/order") == []                       # 绝不市价追单
    trig = f.triggers()
    assert len(trig) == 1 and trig[0]["triggerPx"] == "65000" and trig[0]["sz"] == "1"
    att = trig[0]["attachAlgoOrds"][0]
    assert att["slTriggerPx"] == "60000" and att["tpTriggerPx"] == "70000"
    s = EL._load()["entries"]
    assert s["A1"]["status"] == "done" and s["A1"]["result"] == "rearmed"
    new_id = s["A1"]["rearmed_to"]
    assert s[new_id]["status"] == "armed" and s[new_id]["rearms"] == 1 and s[new_id]["rearm_of"] == "A1"
    m = DX.reg()["triggers"][new_id]                                  # 登记表：同一 decision、沿用原到期
    assert m["decision_id"] == "d" and m["expires"] == EXP and m["rearm_of"] == "A1"
    ev = [x for x in _events(tmp_path) if x["event"] == "entry_rearmed"]
    assert len(ev) == 1 and ev[0]["new_algoId"] == new_id and ev[0]["algoId"] == "A1"


def test_no_rearm_when_price_beyond_trigger(rfake, tmp_path):
    f = rfake(spot="66000")   # gte 单触发后价格继续涨在触发价上方 = 条件仍成立，不重挂、不追
    _reg2()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "missed"
    assert f.triggers() == [] and f.paths("/api/v5/trade/order") == []
    e = EL._load()["entries"]["A1"]
    assert e["status"] == "done" and e["result"] == "missed_no_chase"
    ev = [x for x in _events(tmp_path) if x["event"] == "entry_missed_no_chase"]
    assert len(ev) == 1 and ev[0]["spot"] == 66000.0 and ev[0]["deviation"] == 1000.0


def test_short_lte_rearm_when_price_back_above(rfake):
    f = rfake(spot="66000")   # 做空 lte 破位单：跌穿后价格反弹回触发价上方 = 回到等待侧
    _reg2(side="short", cond="lte")
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "rearmed"
    trig = f.triggers()
    assert len(trig) == 1 and trig[0]["triggerPx"] == "65000" and trig[0]["side"] == "sell"
    assert f.paths("/api/v5/trade/order") == []


def test_short_lte_missed_when_price_stays_below(rfake):
    f = rfake(spot="64000")   # 破位后价格留在触发价下方 = 行情已走，不追
    _reg2(side="short", cond="lte")
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "missed"
    assert f.triggers() == [] and f.paths("/api/v5/trade/order") == []


def test_partial_fill_remainder_rearmed_and_protected(rfake):
    f = rfake(spot="64000", child_state="partially_filled", acc="0.4",
              pos={"instId": INST, "posSide": "long", "pos": "0.4"})
    _reg2()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "rearmed"
    trig = f.triggers()
    assert len(trig) == 1 and trig[0]["sz"] == "0.6"                  # 只重挂未成交的剩余
    oco = f.ocos()
    assert len(oco) == 1 and oco[0]["sz"] == "0.4"                    # 已成交部分照常挂保护
    assert f.paths("/api/v5/trade/order") == []


def test_rearm_bounded_by_original_expiry(rfake, tmp_path):
    f = rfake(spot="64000")
    _reg2(expires="2000-01-01 00:00:00+0800")                          # 原到期已过：即使价格在等待侧也不重挂
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "missed"
    assert f.triggers() == []
    ev = [x for x in _events(tmp_path) if x["event"] == "entry_missed_no_chase"]
    assert len(ev) == 1 and "有效期" in ev[0]["reason"]


def test_second_cycle_rearm_then_missed(rfake):
    f = rfake(spot="64000")
    _reg2()
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "rearmed"
    new_id = EL._load()["entries"]["A1"]["rearmed_to"]
    # 模拟重挂单再次触发并超时（子单仍记为 C1），这次现价已穿过触发价
    s = EL._load(); s["entries"][new_id].update(status="triggered", ordId="C1", triggered_ms=T0); EL._save(s)
    f.spot = "66000"; f.child["state"] = "live"
    out = dict(EL.check_timeouts(_cfg(), now_ms=LATE + 10 * 60000))
    assert out[new_id] == "missed"
    assert len(f.triggers()) == 1                                     # 没有第二张重挂单


def test_halt_blocks_rearm_but_protects(rfake, tmp_path, monkeypatch):
    f = rfake(spot="64000", child_state="partially_filled", acc="0.4",
              pos={"instId": INST, "posSide": "long", "pos": "0.4"})
    _reg2()
    k = tmp_path / "KILL"; k.write_text("x")
    monkeypatch.setattr(settings, "KILL_PATH", k)
    assert dict(EL.check_timeouts(_cfg(), now_ms=LATE))["A1"] == "blocked"
    assert f.triggers() == [] and f.paths("/api/v5/trade/order") == []
    assert f.ocos()[0]["sz"] == "0.4"                                  # 已成交部分仍有保护
