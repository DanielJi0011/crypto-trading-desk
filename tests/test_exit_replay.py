"""退出状态机回放（2026-11-22 审查共识包 I 项）：合成价格路径喂给生产 check() 函数——
移动止损棘轮、保本锁触发/改单、到期/自动续期触发链。交易所适配（demo_exec 登记表、
okx_demo 事件）与判定存档（expiry.latest_regime_call）全部打桩，不调 LLM、不连交易所。
状态字典按生产形状构造（登记表 positions[inst] 元数据 + book 持仓字段）。
场景：单边下跌（空单顺势）→ 激活/跟进；随后反转 → 棘轮不放松、止损位即出场位；
保本锁 1R 边界触发；HALT 冻结；到期自动续期的判定边界（同向 mid 续期 / 末段衰竭不续 / 反向不续 / 上限不续）。
"""
import datetime as _dt

import pytest

from desk import breakeven_lock as BL
from desk import demo_exec as DX
from desk import expiry
from desk import okx_demo as X
from desk import settings
from desk import trailing_stop as TS

INST = "SOL-USDT-SWAP"
FMT = "%Y-%m-%d %H:%M:%S%z"   # 生产登记 deadline 的真实格式（带时区后缀）


class Rig:
    """一台回放机：内存登记表 + 持仓 + 改单记录桩。"""

    def __init__(self, monkeypatch, meta, pos):
        self.reg = {"positions": {INST: meta}}
        self.pos = pos
        self.moves = []
        monkeypatch.setattr(DX, "reg", lambda: self.reg)
        monkeypatch.setattr(DX, "save_reg", lambda r: None)
        monkeypatch.setattr(X, "log_event", lambda *a, **k: None)

    def move(self, cfg, p, new_stop):
        self.moves.append((p["inst_id"], float(new_stop)))
        p["sl_px"] = float(new_stop)
        return True, "ok"

    def tick(self, price):
        self.pos["price"] = price


def _pos(price, sl):
    return {"inst_id": INST, "side": "short", "avg_px": 100.0, "sl_px": sl, "price": price,
            "contracts": 10.0, "pos_side": "net"}


@pytest.fixture
def cfg():
    return settings.load_config()


# ---------------- 移动止损：单边下跌 → 激活/跟进 → 反转不放松 ----------------

def test_trailing_downtrend_then_reversal(cfg, monkeypatch):
    meta = {"exit_mode": "trailing", "plan_tp": 80.0, "atr14_open": 5.0, "decision_id": "D-X",
            "trail": {"activated": False, "best_px": None, "stop_px": 107.5}}
    rig = Rig(monkeypatch, meta, _pos(100.0, 107.5))
    # 激活线 = 100 − 0.5×20 = 90；激活前（99/95/92）零动作
    for px in (99.0, 95.0, 92.0):
        rig.tick(px)
        TS.check(cfg, positions=[rig.pos], move=rig.move, tick_of=lambda i: 0.1)
    assert rig.moves == [] and meta["trail"]["activated"] is False
    # 越过激活线 89 → 候选 = 89 + 1.0×5 = 94；继续跌 85 → 90；70 → 75
    for px in (89.0, 85.0, 70.0):
        rig.tick(px)
        TS.check(cfg, positions=[rig.pos], move=rig.move, tick_of=lambda i: 0.1)
    assert [m[1] for m in rig.moves] == [94.0, 90.0, 75.0]
    assert meta["trail"]["activated"] is True and meta["trail"]["best_px"] == 70.0
    # 反转：价格回升到 74（未触止损 75）→ best 不变、候选不变 → 不改单、绝不放松
    rig.tick(74.0)
    TS.check(cfg, positions=[rig.pos], move=rig.move, tick_of=lambda i: 0.1)
    assert [m[1] for m in rig.moves] == [94.0, 90.0, 75.0] and rig.pos["sl_px"] == 75.0
    # 反转触及止损 75 → 该价位即出场位（登记 stop_px 与交易所止损一致）
    assert meta["trail"]["stop_px"] == 75.0 == rig.pos["sl_px"]


def test_trailing_halt_freezes(cfg, monkeypatch):
    meta = {"exit_mode": "trailing", "plan_tp": 80.0, "atr14_open": 5.0,
            "trail": {"activated": False, "best_px": None, "stop_px": 107.5}}
    rig = Rig(monkeypatch, meta, _pos(80.0, 107.5))   # 已深跌到计划止盈位
    assert TS.check(cfg, positions=[rig.pos], halt=True, move=rig.move, tick_of=lambda i: 0.1) == []
    assert rig.moves == [] and meta["trail"]["activated"] is False


def test_trailing_min_improvement_filter(cfg, monkeypatch):
    # 激活后微小改善（≤ max(tick, 0.05×ATR)=0.25）不改单，防频繁改单
    meta = {"exit_mode": "trailing", "plan_tp": 80.0, "atr14_open": 5.0,
            "trail": {"activated": True, "best_px": 89.0, "stop_px": 94.0}}
    rig = Rig(monkeypatch, meta, _pos(89.0, 94.0))
    rig.tick(88.9)   # 候选 93.9，改善 0.1 < 0.25
    TS.check(cfg, positions=[rig.pos], move=rig.move, tick_of=lambda i: 0.1)
    assert rig.moves == []


# ---------------- 保本锁：1R 边界触发、只锁一次、互斥与资格 ----------------

def test_breakeven_locks_at_1r_boundary(cfg, monkeypatch):
    # 空单入场 100、原始止损 107.5 → R=7.5（冻结值），触发线 = 100 − 7.5 = 92.5
    meta = {"exit_mode": "fixed", "decision_id": "D-Y",
            "breakeven": {"eligible": True, "locked": False, "r_dist": 7.5}}
    rig = Rig(monkeypatch, meta, _pos(100.0, 107.5))
    rig.tick(95.0)
    BL.check(cfg, positions=[rig.pos], move=rig.move)
    assert rig.moves == []                                   # 未到 1R
    rig.tick(92.5)                                           # 恰在触发线（边界含等号）
    BL.check(cfg, positions=[rig.pos], move=rig.move)
    assert rig.moves == [(INST, 100.0)]                      # 止损改到入场价正点位
    assert meta["breakeven"]["locked"] is True and meta["breakeven"]["lock_px"] == 100.0
    rig.tick(85.0)                                           # 已锁定 → 不再改单
    BL.check(cfg, positions=[rig.pos], move=rig.move)
    assert rig.moves == [(INST, 100.0)]


def test_breakeven_exclusions(cfg, monkeypatch):
    # 移动止损仓互斥：即便带 breakeven 元数据也跳过
    meta_t = {"exit_mode": "trailing", "breakeven": {"eligible": True, "locked": False, "r_dist": 7.5}}
    rig = Rig(monkeypatch, meta_t, _pos(90.0, 107.5))
    BL.check(cfg, positions=[rig.pos], move=rig.move)
    assert rig.moves == []
    # 资格冻结为 False（末段衰竭开仓/加仓腿在登记时即 False）→ 跳过
    meta_l = {"exit_mode": "fixed", "breakeven": {"eligible": False, "locked": False, "r_dist": 7.5}}
    rig2 = Rig(monkeypatch, meta_l, _pos(90.0, 107.5))
    BL.check(cfg, positions=[rig2.pos], move=rig.move)
    assert rig.moves == []
    # HALT 冻结
    meta_h = {"exit_mode": "fixed", "breakeven": {"eligible": True, "locked": False, "r_dist": 7.5}}
    rig3 = Rig(monkeypatch, meta_h, _pos(90.0, 107.5))
    assert BL.check(cfg, positions=[rig3.pos], halt=True, move=rig.move) == [] and rig.moves == []


def test_breakeven_already_tighter_marks_only(cfg, monkeypatch):
    # 现止损已被 TM 收紧到入场价之内（空单止损 99 < 入场 100）→ 只标记锁定、不改单
    meta = {"exit_mode": "fixed", "breakeven": {"eligible": True, "locked": False, "r_dist": 7.5}}
    rig = Rig(monkeypatch, meta, _pos(92.0, 99.0))
    BL.check(cfg, positions=[rig.pos], move=rig.move)
    assert rig.moves == [] and meta["breakeven"]["locked"] is True
    assert meta["breakeven"].get("already_tighter") is True


# ---------------- 到期链：due + 自动续期判定边界 ----------------

def _meta_trailing(deadline="2026-11-20 10:00:00+0800", extensions=0):
    return {"exit_mode": "trailing", "side": "short", "deadline": deadline,
            "horizon_days": 5, "extensions": extensions}


def _judge_rec(direction="short", strength="strong", stage="mid", failed=False):
    return {"inst_id": INST, "failed": failed, "date": "2026-11-20", "session": "morning",
            "time": "2026-11-20 08:10:00+0800", "decision_id": "D-Z", "judge_standard": 3,
            "judge": {"direction": direction, "strength": strength, "stage": stage,
                      "stage_raw": stage}}


def test_expiry_due_window(cfg):
    m = _meta_trailing()
    dl = _dt.datetime.strptime(m["deadline"], FMT)
    assert expiry.due(cfg, m, dl) is True
    assert expiry.due(cfg, m, dl - _dt.timedelta(hours=2)) is False   # 预窗口外
    assert expiry.due(cfg, {}, dl) is False                            # 无 deadline 不到期


def test_trailing_horizon_extend_and_boundaries(cfg, monkeypatch):
    now_dl = "2026-11-20 10:00:00+0800"
    # 同向 strong/mid → 续期一期（+5 天），apply_extend 落盘语义正确
    monkeypatch.setattr(expiry, "latest_regime_call", lambda inst: _judge_rec())
    m = _meta_trailing(now_dl)
    v = expiry.trailing_extend_check(cfg, INST, m, "short")
    assert v is not None and v["new_deadline"] == "2026-11-25 10:00:00+0800" and v["days"] == 5.0
    expiry.apply_extend(m, v)
    assert m["deadline"] == "2026-11-25 10:00:00+0800" and m["extensions"] == 1
    assert m["orig_deadline"] == now_dl
    # 末段衰竭（生效 stage=late_exhaustion）→ 不续，走原到期路径
    monkeypatch.setattr(expiry, "latest_regime_call",
                        lambda inst: _judge_rec(stage="late_exhaustion"))
    assert expiry.trailing_extend_check(cfg, INST, _meta_trailing(now_dl), "short") is None
    # 判定反向 → 不续
    monkeypatch.setattr(expiry, "latest_regime_call", lambda inst: _judge_rec(direction="long"))
    assert expiry.trailing_extend_check(cfg, INST, _meta_trailing(now_dl), "short") is None
    # 判定失败（judge 为空）→ 不续
    monkeypatch.setattr(expiry, "latest_regime_call", lambda inst: _judge_rec(failed=True))
    assert expiry.trailing_extend_check(cfg, INST, _meta_trailing(now_dl), "short") is None
    # 已达延期上限 → 不续
    monkeypatch.setattr(expiry, "latest_regime_call", lambda inst: _judge_rec())
    max_ext = int(expiry.ecfg(cfg)["max_extensions"])
    assert expiry.trailing_extend_check(cfg, INST, _meta_trailing(now_dl, extensions=max_ext),
                                        "short") is None
    # 非 trailing 仓位 → 不续
    m2 = _meta_trailing(now_dl)
    m2["exit_mode"] = "fixed"
    assert expiry.trailing_extend_check(cfg, INST, m2, "short") is None
