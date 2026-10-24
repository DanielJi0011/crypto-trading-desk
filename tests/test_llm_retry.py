"""LLM 自动重试：可重试错误退避重试并记日志；全部失败才抛出并进入日报"需报告用户"；不可重试错误不重试。离线，不 sleep，不联网。"""
import pytest

from desk import llm_retry as R


class E503(Exception):
    status_code = 503


class APITimeoutError(Exception):
    pass


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    waits = []
    monkeypatch.setattr(R, "SLEEP", waits.append)
    monkeypatch.setattr(R, "DELAYS", [5, 15, 30, 60])
    monkeypatch.setattr(R, "RETRIES", [])
    monkeypatch.setattr(R, "FAILURES", [])
    return waits


def _flaky(n, exc):
    c = {"n": 0}
    def f():
        c["n"] += 1
        if c["n"] <= n:
            raise exc
        return "ok"
    return f, c


def test_recovers_after_retries(reset, caplog):
    f, c = _flaky(2, E503("Error code: 503"))
    with caplog.at_level("WARNING", logger="desk.llm_retry"):
        assert R.call(f) == "ok"
    assert c["n"] == 3 and reset == [5, 15] and len(R.RETRIES) == 2 and R.FAILURES == []
    assert sum("第" in r.message and "次重试" in r.message for r in caplog.records) == 2
    assert R.report_lines() == []


def test_all_fail_then_raise_and_report(reset):
    f, c = _flaky(99, APITimeoutError("timeout"))
    with pytest.raises(APITimeoutError):
        R.call(f)
    assert c["n"] == 5 and reset == [5, 15, 30, 60]
    lines = R.report_lines()
    assert len(lines) == 1 and "需报告用户" in lines[0] and "重试 4 次" in lines[0]


def test_non_retryable_not_retried(reset):
    f, c = _flaky(99, ValueError("bad schema"))
    with pytest.raises(ValueError):
        R.call(f)
    assert c["n"] == 1 and reset == []


def test_retryable_detection_via_cause_and_text():
    try:
        try:
            raise E503("x")
        except E503 as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as e:
        assert R.retryable(e)
    assert R.retryable(Exception("Error code: 502 - bad gateway"))
    assert not R.retryable(Exception("Error code: 400 - bad request"))


def test_install_wraps_chat_model(reset, monkeypatch):
    from langchain_core.language_models.chat_models import BaseChatModel
    from desk.llm import FakeChatModel
    monkeypatch.setattr(R, "_installed", False)
    orig_invoke = BaseChatModel.invoke
    c = {"n": 0}
    def boom(self, *a, **k):
        c["n"] += 1
        if c["n"] == 1:
            raise E503("Error code: 503")
        return "ok"
    monkeypatch.setattr(BaseChatModel, "invoke", boom)
    R.install({"llm": {"retry_delays": [1, 2]}})
    try:
        assert FakeChatModel(tier="quick").invoke("hi") == "ok"
        assert c["n"] == 2 and reset == [1]
    finally:
        monkeypatch.setattr(BaseChatModel, "invoke", orig_invoke)
