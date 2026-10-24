"""run_daily.sh 的辅助入口（O1 第 1、2 点与 O2 daily_failed 的第一批实现）：只记事件与发通知，不做交易。

  python -m desk.daily_gate failed <日期> <返回码>   → 事件 daily_failed + webhook daily_failed（每次）
  python -m desk.daily_gate done <日期> 0         → 事件 daily_done + webhook daily_done
  python -m desk.daily_gate duplicate <日期> <原因>  → 事件 daily_duplicate_trigger（running / done）
"""
from __future__ import annotations

import sys

from desk import okx_demo as X


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 3:
        print(__doc__); return 2
    cmd, day, extra = a[0], a[1], a[2]
    if cmd == "failed":
        X.log_event("daily_failed", day=day, rc=extra)
        from desk.demo_ws import notify
        notify({"kind": "daily_failed", "message": f"run_daily {day} 失败，返回码 {extra}（见 logs/run-{day}.log）"})
        return 0
    if cmd == "done":
        X.log_event("daily_done", day=day)
        from desk.demo_ws import notify
        notify({"kind": "daily_done", "message": f"run_daily {day} 已完成"})
        return 0
    if cmd == "duplicate":
        X.log_event("daily_duplicate_trigger", day=day, why=extra)
        return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
