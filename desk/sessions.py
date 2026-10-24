"""每日两场（早场 morning / 晚场 evening）的时间与命名。2026-10-23 用户批准。

- 早场：北京时间 08:00 左右，窗口 07:55 到 12:00（沿用原 O1 窗口）。
- 晚场：按 America/New_York 定义（默认纽约 08:30 开场，窗口 08:25 到 10:30），用 zoneinfo 换算成北京时间，
  因此随美国夏令时/冬令时自动切换（夏令时 = 北京 20:30，冬令时 = 北京 21:30）。
- 日期一律用北京日期；晚场的标记、报告、日志、X 摘录目录都带 -evening 后缀，决策编号 D-<日期>-E-<币>。
"""
from __future__ import annotations

import datetime as _dt
import sys
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")
SESSIONS = ("morning", "evening")
DEF = {"morning": {"tz": "Asia/Shanghai", "start": "08:00", "window": ["07:55", "12:00"], "missing_after_minutes": 90},
       "evening": {"tz": "America/New_York", "start": "08:30", "window": ["08:25", "10:30"], "missing_after_minutes": 60}}


def norm(s: str | None) -> str:
    s = (s or "morning").strip().lower()
    if s not in SESSIONS:
        raise ValueError(f"未知场次 {s}（只支持 morning/evening）")
    return s


def suffix(s: str | None) -> str:
    return "" if norm(s) == "morning" else "-evening"


def key(day: str, s: str | None) -> str:
    """标记、报告、日志、X 摘录目录用的名字：早场 <日期>，晚场 <日期>-evening。"""
    return f"{day}{suffix(s)}"


def did_prefix(day: str, s: str | None) -> str:
    return f"D-{day.replace('-', '')}-" + ("E-" if norm(s) == "evening" else "")


def scfg(cfg, s) -> dict:
    s = norm(s)
    d = dict(DEF[s])
    d.update(((cfg or {}).get("sessions") or {}).get(s) or {})
    return d


def _at(day: str, hm: str, tz: str) -> _dt.datetime:
    """北京日期 day 那天、按 tz 当地时间 hm 的时刻（北京时区）。晚场：取北京日期当天落在北京时间内的那个纽约时刻。"""
    h, m = (int(x) for x in hm.split(":"))
    z = ZoneInfo(tz)
    d0 = _dt.date.fromisoformat(day)
    for off in (0, -1, 1):
        t = _dt.datetime.combine(d0 + _dt.timedelta(days=off), _dt.time(h, m), z).astimezone(BJ)
        if t.date() == d0:
            return t
    return _dt.datetime.combine(d0, _dt.time(h, m), z).astimezone(BJ)


def start_bj(cfg, s, day: str) -> _dt.datetime:
    c = scfg(cfg, s)
    return _at(day, c["start"], c["tz"])


def window_bj(cfg, s, day: str) -> tuple[_dt.datetime, _dt.datetime]:
    c = scfg(cfg, s)
    return _at(day, c["window"][0], c["tz"]), _at(day, c["window"][1], c["tz"])


def in_window(cfg, s, now: _dt.datetime) -> bool:
    now = now.astimezone(BJ)
    a, b = window_bj(cfg, s, now.date().isoformat())
    return a <= now <= b


def missing_at(cfg, s, day: str) -> _dt.datetime:
    """开场后 missing_after_minutes 仍无完成标记 → 告警时刻（北京时区）。"""
    return start_bj(cfg, s, day) + _dt.timedelta(minutes=int(scfg(cfg, s)["missing_after_minutes"]))


def main(argv=None) -> int:
    """python -m desk.sessions window <session> <北京日期> [北京 HH:MM]：在窗口内退出 0，否则 1，并打印北京时间窗口。"""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) >= 3 and a[0] == "window":
        from desk import settings
        cfg = settings.load_config()
        s, day = a[1], a[2]
        lo, hi = window_bj(cfg, s, day)
        if len(a) >= 4:
            h, m = (int(x) for x in a[3].split(":"))
            now = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(h, m), BJ)
        else:
            now = _dt.datetime.now(BJ)
        print(f"{lo:%H:%M}-{hi:%H:%M}")
        return 0 if lo <= now <= hi else 1
    print(main.__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
