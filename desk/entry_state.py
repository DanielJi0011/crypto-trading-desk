"""R12 入场状态机。每笔入场（条件单以 algoId，现价单以 ordId 为键）只能按下表推进，非法跳转记 entry_state_illegal 且不改状态。

armed（条件单已挂）→ triggered（触发，子单已出）/ canceled / expired
open_sent（现价单已发）→ partial / filled / canceled
triggered → partial（部分成交）/ filled / canceled（子单撤销且无成交）
partial → partial / filled（含部分成交后撤单的最终态）
filled → closed（持仓已平）
canceled、expired、closed 为终态。
状态保存在 state/entry_states.json，每次变化记 entry_state 事件。
"""
from __future__ import annotations

import contextlib
import fcntl
import json

from desk import okx_demo as X
from desk import settings

PATH = settings.STATE / "entry_states.json"
TRANS = {
    None: {"armed", "open_sent"},
    "armed": {"triggered", "canceled", "expired"},
    "open_sent": {"partial", "filled", "canceled"},
    "triggered": {"partial", "filled", "canceled"},
    "partial": {"partial", "filled"},
    "filled": {"closed"},
    "canceled": set(), "expired": set(), "closed": set(),
}


@contextlib.contextmanager
def _locked():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(str(PATH) + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get(key) -> str | None:
    return (load().get(str(key)) or {}).get("state")


def advance(key, new: str, **info) -> bool:
    if not key:
        return False
    key = str(key)
    with _locked():
        d = load()
        cur = (d.get(key) or {}).get("state")
        if cur == new and new != "partial":
            return True
        if new not in TRANS.get(cur, set()):
            X.log_event("entry_state_illegal", key=key, frm=cur, to=new, **info)
            return False
        rec = d.setdefault(key, {"history": []})
        rec.update(info); rec["state"] = new
        rec["history"] = (rec.get("history") or [])[-20:] + [[settings.now_str(), new]]
        PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    X.log_event("entry_state", key=key, frm=cur, to=new, **info)
    return True
