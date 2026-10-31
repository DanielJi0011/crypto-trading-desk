"""2026-10-30 用户决定：最后由交易经理拍板（固定行为，无开关）。
交易经理无结论（下单工具 3 次调用失败/解析失败/空输出）→ 不开仓、不加仓、不挂条件单，日报写明原因。全部离线打桩。"""
import pytest

from desk import settings
from tests.test_tm_no_vote import SOURCES, _st, desk_env  # noqa: F401


class _Boom:
    def __init__(self, exc):
        self.exc = exc


def _install(monkeypatch, box, exc):
    from desk import llm as dllm

    class Fake:
        def with_structured_output(self, *_):
            return self

        def invoke(self, *_a, **_k):
            raise exc
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: Fake())


class OutputParserException(Exception):
    pass


def _usage():
    return {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", ["call_fail", "parse_fail", "empty_none", "empty_dict"])
def test_no_conclusion_no_open_all_paths(desk_env, monkeypatch, source, kind):
    manager, calls, box = desk_env
    cfg = settings.load_config()
    if kind == "call_fail":
        _install(monkeypatch, box, RuntimeError("timeout after 3 retries"))
    elif kind == "parse_fail":
        _install(monkeypatch, box, OutputParserException("bad json"))
    else:
        box["dec"] = None if kind == "empty_none" else {}
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None, source=source)
    assert rec["tm_status"] == "absent"
    assert not calls["open_now"] and not calls["place_trigger"]
    assert rec["final_action"] == "hold(交易经理缺席)" and rec["pending"] is None
    assert rec["notes"][0].startswith("交易经理无结论，不开仓")
    want = {"call_fail": "3 次调用均未通过", "parse_fail": "3 次调用均未通过", "empty_none": "输出为空", "empty_dict": "输出为空"}[kind]
    assert want in rec["tm_absent_reason"] and want in rec["notes"][0]


def test_report_shows_reason(desk_env, monkeypatch):
    manager, calls, box = desk_env
    from desk import report
    cfg = settings.load_config()
    _install(monkeypatch, box, OutputParserException("bad"))
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    from desk import demo_exec as DX
    monkeypatch.setattr(DX, "is_demo", lambda cfg: False)
    txt = open(report.write(cfg, "2099-01-01", [rec], [], [], [], [], _usage(), {}, [], name="t-tmreq"), encoding="utf-8").read()
    assert "交易经理无结论，不开仓" in txt and "工具参数无法解析" in txt

