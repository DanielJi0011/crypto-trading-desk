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

# 固定时间决策说明（2026-11-05 用户要求）：注入全体角色与交易经理的提示词，让所有决策者知道本台是
# 时点决策而非连续盯盘——本场不定，下一例行决策点就是下一场；盘中只有条件单/止损止盈/触发再讨论在工作。
CADENCE_NOTE = (
    "## 决策节奏（固定时间决策）\n"
    "本台是固定时间决策，不是连续盯盘：例行场次只有早场（北京时间约 08:05）与晚场（纽约时间 08:30，北京约 21:30）两个时点。"
    "本场做出的决策会被当场执行或否决；本场不下单，下一次例行决策就是下一场（止损/止盈/到期触发的再讨论是事件驱动的例外）。"
    "盘中只有已挂条件单与止损止盈在工作，没有人盯盘改单：需要盘中响应请用条件单表达；"
    "写“等待确认”时请想清楚等的是哪一场的确认，并把触发条件写成下一场可核对的形式。"
)
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
    """python -m desk.sessions window <session> <北京日期> [北京 HH:MM]：在窗口内退出 0，否则 1，并打印北京时间窗口；判断失败退出 3。"""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) >= 3 and a[0] == "window":
        try:
            from desk import settings
            cfg = settings.load_config()
            s, day = a[1], a[2]
            lo, hi = window_bj(cfg, s, day)
            if len(a) >= 4:
                h, m = (int(x) for x in a[3].split(":"))
                now = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(h, m), BJ)
            else:
                now = _dt.datetime.now(BJ)
        except Exception as e:   # 判断失败 ≠ 不在窗口：返回 3，stdout 不打印窗口，由 run_daily.sh 告警
            print(f"window check failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
        print(f"{lo:%H:%M}-{hi:%H:%M}")
        return 0 if lo <= now <= hi else 1
    print(main.__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
