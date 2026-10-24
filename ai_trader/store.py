"""路径、配置、日志（交易日志 / 订单日志 / 信号队列 / 状态）的读写工具。"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HOME = Path(os.environ.get("AI_TRADER_HOME", Path(__file__).resolve().parent))
TZ = ZoneInfo("Asia/Shanghai")

TRADES_CSV = HOME / "journal" / "trades.csv"
ORDERS_LOG = HOME / "journal" / "orders.jsonl"
REVIEWS_DIR = HOME / "journal" / "reviews"
SIGNALS = HOME / "signals" / "queue.jsonl"
STATE = HOME / "state" / "state.json"
THESIS_DIR = HOME / "thesis"

TRADE_FIELDS = [
    "trade_id", "status", "mode", "thesis_version", "inst_id", "inst_type", "side", "sz",
    "notional_usd", "entry_time", "entry_px", "sl_px", "tp_px", "exit_time", "exit_px",
    "exit_reason", "fee_usd", "pnl_usd", "pnl_pct", "ord_id", "signal_id", "entry_reason", "note",
]


def now() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def now_str() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S%z")


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def load_config(path: Path | None = None) -> dict:
    with open(path or HOME / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- jsonl
def append_jsonl(path: Path, rec: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl(path: Path, recs: list[dict]):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


# ---------------------------------------------------------------- 交易日志
def read_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with open(TRADES_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_trades(rows: list[dict]):
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADES_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRADE_FIELDS})


def new_id(prefix: str) -> str:
    return f"{prefix}{now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------- 状态
def read_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"check_count": 0, "last_check": None, "last_daily_review": None, "last_weekly_summary": None}


def write_state(st: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 思路文档版本
def thesis_versions() -> list[int]:
    out = []
    for p in THESIS_DIR.glob("v*.md"):
        try:
            out.append(int(p.stem[1:]))
        except ValueError:
            pass
    return sorted(out)


def current_thesis() -> tuple[str, Path]:
    vs = thesis_versions()
    if not vs:
        raise FileNotFoundError("thesis/ 下没有任何版本文件")
    return f"v{vs[-1]}", THESIS_DIR / f"v{vs[-1]}.md"
