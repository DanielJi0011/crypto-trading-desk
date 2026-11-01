import datetime as dt
from desk import report, settings

NOW = dt.datetime(2026, 11, 1, 22, 35, tzinfo=settings.TZ)


def test_positive():
    s = report.annualized_line(110, 100, "2026-01-01 00:00+0800", "2027-01-01 00:00+0800")
    assert s == "综合年化收益 +10.00%（累计 +10.00%，已运行 365.0 天）"


def test_negative_short():
    s = report.annualized_line(99, 100, "2026-10-30 22:35+0800", NOW)
    assert s.startswith("综合年化收益 -") and "累计 -1.00%" in s and "已运行 2.0 天" in s
    assert s.endswith("（样本过短，仅供参考）")


def test_zero_days_and_missing():
    for st in (NOW, None, "坏格式"):
        assert "暂无" in report.annualized_line(101, 100, st, NOW)
    assert "暂无" in report.annualized_line(101, 0, "2026-10-20 22:35+0800", NOW)
