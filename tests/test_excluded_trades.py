"""排除交易（2026-11-09 外国交易清除）：被排除的成交不得进入平仓缓存、复盘候选、交易经理账本与止损记录。
回归背景：2026-11-09 晚手动删掉了旧机器误下 ETH 空单的台账行，但次日早场 run_daily 从 OKX
positions-history 重新拉取后又把它写了回来——删除本身不持久，必须在写入点查排除清单。
"""
import json

from desk import demo_trades as D
from desk import excluded_trades as X
from desk import stop_cooldown as SC
from desk import tm_ledger as TM

FOREIGN = "DEMO-4063068843111297025-1793803696573"
FOREIGN_ALGO = "4070196111256268802"
OWN = "DEMO-4070229932009795585-1793804704625"


def _write_exclusion(tmp_path):
    p = tmp_path / "excluded_trades.json"
    p.write_text(json.dumps({FOREIGN: {"reason": "test", "algo_id": FOREIGN_ALGO}}, ensure_ascii=False))
    return p


def test_helper_match(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "PATH", _write_exclusion(tmp_path))
    assert X.is_excluded(FOREIGN)
    assert not X.is_excluded(OWN)
    assert X.is_excluded_algo(FOREIGN_ALGO)
    assert not X.is_excluded_algo("123")


def test_drop_excluded_prunes_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "PATH", _write_exclusion(tmp_path))
    cache = {FOREIGN: {"trade_id": FOREIGN}, OWN: {"trade_id": OWN}}
    assert list(D.drop_excluded(cache)) == [OWN]


def test_record_closed_skips_excluded(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "PATH", _write_exclusion(tmp_path))
    monkeypatch.setattr(TM, "PATH", tmp_path / "tm_ledger.jsonl")
    monkeypatch.setattr(TM, "DECISIONS", tmp_path / "decisions.jsonl")
    (tmp_path / "decisions.jsonl").write_text("")
    rows = TM.record_closed({}, [
        {"trade_id": FOREIGN, "status": "closed", "inst_id": "ETH-USDT-SWAP", "side": "sell",
         "entry_time": "2026-11-04 22:48:16+0800", "exit_time": "2026-11-04 23:14:34+0800",
         "entry_px": "2377.37", "exit_px": "2444.45", "pnl_usd": "-417.30"},
        {"trade_id": OWN, "status": "closed", "inst_id": "SOL-USDT-SWAP", "side": "sell",
         "entry_time": "2026-11-04 23:05:04+0800", "exit_time": "2026-11-05 23:51:22+0800",
         "entry_px": "96", "exit_px": "90.2179", "pnl_usd": "337.95"},
    ])
    assert [r["trade_id"] for r in rows] == [OWN]
    assert TM.stats() == {"n": 1, "pnl": 337.95, "win_rate": 1.0, "avg_rr": None, "rr_n": 0}


def test_review_candidates_skips_excluded(monkeypatch, tmp_path):
    from desk import ledger
    monkeypatch.setattr(X, "PATH", _write_exclusion(tmp_path))
    monkeypatch.setattr(ledger.store, "read_trades", lambda: [
        {"trade_id": FOREIGN, "status": "closed"},
        {"trade_id": OWN, "status": "closed"},
    ])
    out = D.review_candidates({}, set(), demo=False)
    assert [t["trade_id"] for t in out] == [OWN]


def test_stop_record_skips_excluded_algo(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "PATH", _write_exclusion(tmp_path))
    monkeypatch.setattr(SC, "PATH", tmp_path / "stop_cooldown.json")
    assert SC.record("ETH-USDT-SWAP", "sell", "short", algo_id=FOREIGN_ALGO) is None
    assert SC.last_record("ETH-USDT-SWAP") is None
    assert SC.record("ETH-USDT-SWAP", "sell", "short", algo_id="999") == "short"
    assert SC.last_record("ETH-USDT-SWAP")["algoId"] == "999"
