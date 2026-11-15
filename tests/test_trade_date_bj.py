"""早场日期校验：vendor 按服务器本地时间（UTC）判“今天”，07:35 北京场
（= 23:35 UTC 前一日）时把当日误判为未来日期，2026-11-15 早场三币全在
propagate 里抛 ValueError。desk 侧把校验换成北京时间口径（不改 vendor
源码，仿照 patch_ta_memory 的猴子补丁）。离线，不调用真实 LLM。"""
from datetime import date as _date

import pytest

from desk import analysis, settings


def _fake_system_date(monkeypatch, ymd):
    """把 vendor 的 get_current_date 看到的系统日期钉住（模拟服务器 UTC 时钟）。"""
    import tradingagents.dataflows.date_window as dw

    class _D:
        @classmethod
        def today(cls):
            return _date.fromisoformat(ymd)

        fromisoformat = staticmethod(_date.fromisoformat)

    monkeypatch.setattr(dw, "date", _D)


def test_morning_0735_bj_passes_when_utc_still_yesterday(monkeypatch):
    # 2026-11-14 23:35 UTC = 2026-11-15 07:35 北京：vendor 口径下“今天”
    # 还是 2026-11-14，当日会被判未来；北京口径下必须通过
    _fake_system_date(monkeypatch, "2026-11-14")
    import tradingagents.dataflows.date_window as dw

    assert dw.get_current_date() == "2026-11-14"
    assert "2026-11-15" > dw.get_current_date()  # vendor 逻辑会在此拒绝
    monkeypatch.setattr(analysis, "_beijing_today", lambda: "2026-11-15")
    assert analysis._validate_trade_date_bj("2026-11-15") == "2026-11-15"


def test_genuinely_future_date_still_rejected(monkeypatch):
    monkeypatch.setattr(analysis, "_beijing_today", lambda: "2026-11-15")
    with pytest.raises(ValueError, match="cannot be in the future"):
        analysis._validate_trade_date_bj("2026-11-16")
    # 当日及过去日期通过
    assert analysis._validate_trade_date_bj("2026-11-15") == "2026-11-15"
    assert analysis._validate_trade_date_bj("2026-11-14") == "2026-11-14"


def test_evening_caliber_passes(monkeypatch):
    # 晚场 21:00 北京 = 13:00 UTC 当日：系统日期与北京日期同一天，补丁后同样通过
    _fake_system_date(monkeypatch, "2026-11-15")
    monkeypatch.setattr(analysis, "_beijing_today", lambda: "2026-11-15")
    assert analysis._validate_trade_date_bj("2026-11-15") == "2026-11-15"


def test_format_errors_preserved(monkeypatch):
    monkeypatch.setattr(analysis, "_beijing_today", lambda: "2026-11-15")
    for bad in ("2026/11/15", "15-11-2026", "2026-11-5", "not-a-date"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            analysis._validate_trade_date_bj(bad)


def test_patch_applied_to_vendor_module(monkeypatch):
    import tradingagents.graph.trading_graph as tg

    orig = tg._validate_trade_date
    monkeypatch.setattr(tg, "_validate_trade_date", orig)  # teardown 时恢复原值
    monkeypatch.setattr(analysis, "_beijing_today", lambda: "2026-11-15")
    analysis.patch_trade_date(settings.load_config(), object)
    assert tg._validate_trade_date is analysis._validate_trade_date_bj
    assert tg._validate_trade_date("2026-11-15") == "2026-11-15"
