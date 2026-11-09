"""OKX K 线限流防护（2026-11-09 晚场 50011 事故）：
- _paced_candles：K 线请求串行化 + 最小间隔，防并发打满 OKX 按 IP 的 2 秒窗口；
- _candles 缓存：按 (inst, bar) 存最大窗口，不同 days 切尾共用，不重复拉取；
- _retry：50011 走指数退避 + 抖动，普通错误保持原短退避。
"""
import threading
import time

import pytest

from desk import okx_data

T0 = 1_700_000_000_000
STEP = 86_400_000


def _rows(n, start_ts):
    """OKX 风格 K 线行（新→旧）：[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]"""
    return [[str(start_ts - i * STEP), "100", "110", "90", "105", "10", "10", "1000", "1"]
            for i in range(n)]


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_candles(self, inst_id, bar="15m", limit=100, after=None, before=None):
        self.calls.append("candles")
        return _rows(limit, T0)

    def get_history_candles(self, inst_id, bar="15m", limit=100, after=None, before=None):
        self.calls.append("history")
        return _rows(limit, int(after) - STEP)


@pytest.fixture
def fake(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(okx_data, "_client", fc)
    monkeypatch.setattr(okx_data, "_cache", {})
    monkeypatch.setattr(okx_data, "_last_candle_at", 0.0)
    return fc


def test_cache_shared_across_days(fake):
    df = okx_data.candles("BTC-USDT-SWAP", "1Dutc", 420)
    assert len(df) == 420
    n_calls = len(fake.calls)
    assert n_calls >= 2   # 300 首拉 + 至少 1 页历史
    df2 = okx_data.candles("BTC-USDT-SWAP", "1Dutc", 30)
    assert len(df2) == 30
    assert len(fake.calls) == n_calls   # 小窗口直接切缓存，零网络请求
    assert df2["ts"].tolist() == df["ts"].tolist()[-30:]


def test_cache_refetch_when_window_too_small(fake):
    okx_data.candles("BTC-USDT-SWAP", "1Dutc", 100)
    n_calls = len(fake.calls)
    df = okx_data.candles("BTC-USDT-SWAP", "1Dutc", 420)
    assert len(df) == 420
    assert len(fake.calls) > n_calls   # 缓存窗口不够才重新拉


def test_paced_candles_serializes_and_spaces():
    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fn():
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.02)
        with lock:
            state["cur"] -= 1
        return "ok"

    threads = [threading.Thread(target=okx_data._paced_candles, args=(fn,)) for _ in range(4)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    assert state["max"] == 1            # 全局锁串行化，无并发
    assert elapsed >= 3 * okx_data._PACE_INTERVAL   # 相邻请求有最小间隔


def test_retry_50011_exponential_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception("OKX 错误 code=50011 msg=Too Many Requests")
        return "ok"

    assert okx_data._retry(flaky) == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert 3 <= sleeps[0] < 4 and 6 <= sleeps[1] < 7   # 3·2^i + [0,1) 抖动


def test_retry_other_errors_short_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception("connection reset")
        return "ok"

    assert okx_data._retry(flaky) == "ok"
    assert sleeps == [2, 4]   # 原行为不变


def test_retry_reraises_after_tries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def always_50011():
        raise Exception("OKX 错误 code=50011 msg=Too Many Requests")

    with pytest.raises(Exception, match="50011"):
        okx_data._retry(always_50011)
