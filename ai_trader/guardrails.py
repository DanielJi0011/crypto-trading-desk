"""安全护栏：对每一笔下单请求做强制检查。任何一条不通过即拒绝。

护栏只管"能不能做"（风险边界），不管"该不该做"（策略由智能体在思路文档中决定）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import store


@dataclass
class OrderIntent:
    inst_id: str
    side: str                    # buy / sell
    sz: float                    # 现货=基础币数量；永续=合约张数
    ref_px: float                # 参考价（限价单用 px，市价单用最新价）
    notional_usd: float          # 估算名义价值
    ord_type: str = "market"
    sl_trigger_px: Optional[float] = None
    tp_trigger_px: Optional[float] = None
    close: bool = False          # 平仓/减仓单（现货卖出持仓、永续 reduceOnly）
    inst_type: str = "SPOT"


@dataclass
class GuardResult:
    ok: bool
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def as_dict(self):
        return {"ok": self.ok, "reasons": self.reasons, "checks": self.checks}


def _today_orders(mode: str) -> int:
    """当日已提交的开仓单数（live 计真实订单，signal_only 计信号，便于纸面模拟同样受限）。"""
    t = store.today_str()
    n = 0
    for r in store.read_jsonl(store.ORDERS_LOG):
        if r.get("date") == t and r.get("result") in ("placed", "signal") and not r.get("close"):
            n += 1
    return n


def _today_realized_pnl() -> float:
    t = store.today_str()
    pnl = 0.0
    for r in store.read_trades():
        if r.get("status") == "closed" and str(r.get("exit_time", "")).startswith(t):
            try:
                pnl += float(r.get("pnl_usd") or 0)
            except ValueError:
                pass
    return pnl


def _open_trades() -> list[dict]:
    return [r for r in store.read_trades() if r.get("status") == "open"]


def check(intent: OrderIntent, cfg: dict) -> GuardResult:
    g = cfg["guardrails"]
    cap = float(cfg["capital"]["total_usd"])
    mode = cfg.get("mode", "signal_only")
    reasons, checks = [], {}

    def fail(code, msg):
        reasons.append(f"[{code}] {msg}")

    # 0. 紧急停止开关
    kill = store.HOME / g.get("kill_switch_file", "state/KILL")
    checks["kill_switch"] = kill.exists()
    if kill.exists() and not intent.close:
        fail("KILL", f"紧急停止文件 {kill.name} 存在，禁止开仓")

    # 1. 提现权限：配置层面必须为 false
    if g.get("allow_withdrawal"):
        fail("WITHDRAW", "配置 allow_withdrawal 必须为 false")

    # 2. 杠杆
    lev = int(g.get("leverage", 1))
    checks["leverage"] = lev
    if lev != 1 and not g.get("allow_leverage"):
        fail("LEVERAGE", f"未允许杠杆，但 leverage={lev}")

    # 3. 品种类型 / 品种池
    it = cfg["instruments"]["inst_type"]
    if intent.inst_type != it:
        fail("INST_TYPE", f"配置只允许 {it}，收到 {intent.inst_type}")
    uni = cfg["instruments"].get("universe") or []
    if uni and intent.inst_id not in uni:
        fail("UNIVERSE", f"{intent.inst_id} 不在品种池 {uni} 中")
    if it == "SPOT" and not intent.inst_id.endswith("-" + cfg["capital"]["quote_ccy"]):
        fail("QUOTE", f"现货只交易 {cfg['capital']['quote_ccy']} 计价品种")

    # 4. 订单类型/方向
    if intent.ord_type not in g.get("allowed_ord_types", ["market", "limit"]):
        fail("ORD_TYPE", f"不允许的订单类型 {intent.ord_type}")
    if intent.side not in ("buy", "sell"):
        fail("SIDE", "side 必须是 buy 或 sell")
    if it == "SPOT" and intent.side == "sell" and not intent.close:
        fail("SPOT_SHORT", "现货不能做空：卖出单必须标记为 --close（卖出已有持仓）")
    if intent.sz <= 0 or intent.ref_px <= 0:
        fail("SIZE", "数量与价格必须为正")

    # 5. 仓位大小
    checks["notional_usd"] = round(intent.notional_usd, 4)
    if not intent.close:
        max_n = cap * g["max_position_pct"] / 100
        checks["max_notional_usd"] = max_n
        if intent.notional_usd > max_n + 1e-9:
            fail("POSITION", f"名义价值 {intent.notional_usd:.2f} 超过单笔上限 {max_n:.2f} USD（{g['max_position_pct']}%）")
        if intent.notional_usd < g.get("min_order_usd", 0):
            fail("MIN_ORDER", f"名义价值低于最小下单额 {g.get('min_order_usd')} USD")
        opens = _open_trades()
        exposure = sum(float(r.get("notional_usd") or 0) for r in opens)
        checks["open_positions"] = len(opens)
        checks["open_exposure_usd"] = round(exposure, 4)
        if len(opens) >= g.get("max_open_positions", 3):
            fail("MAX_POS", f"未平仓数 {len(opens)} 已达上限 {g.get('max_open_positions')}")
        max_exp = cap * g.get("max_total_exposure_pct", 100) / 100
        if exposure + intent.notional_usd > max_exp + 1e-9:
            fail("EXPOSURE", f"总敞口将达 {exposure + intent.notional_usd:.2f}，超过 {max_exp:.2f} USD")

    # 6. 强制止损
    if not intent.close and g.get("require_stop_loss", True):
        sl = intent.sl_trigger_px
        if sl is None:
            fail("STOP_LOSS", "开仓单必须附带止损价（--sl）")
        else:
            if intent.side == "buy" and sl >= intent.ref_px:
                fail("STOP_LOSS", f"买入单止损价 {sl} 必须低于参考价 {intent.ref_px}")
            if intent.side == "sell" and sl <= intent.ref_px:
                fail("STOP_LOSS", f"卖出单止损价 {sl} 必须高于参考价 {intent.ref_px}")
            dist = abs(intent.ref_px - sl) / intent.ref_px * 100
            checks["sl_distance_pct"] = round(dist, 3)
            if dist > g.get("max_stop_loss_pct", 100):
                fail("STOP_LOSS", f"止损距离 {dist:.2f}% 超过上限 {g.get('max_stop_loss_pct')}%")

    # 7. 每日亏损上限
    pnl = _today_realized_pnl()
    checks["today_realized_pnl_usd"] = round(pnl, 4)
    lim = cap * g["max_daily_loss_pct"] / 100
    if not intent.close and pnl <= -lim:
        fail("DAILY_LOSS", f"当日已实现亏损 {pnl:.2f} 达到上限 -{lim:.2f} USD，今日禁止开仓")

    # 8. 每日下单次数
    n = _today_orders(mode)
    checks["today_orders"] = n
    if not intent.close and n >= g["max_orders_per_day"]:
        fail("MAX_ORDERS", f"今日开仓单 {n} 已达上限 {g['max_orders_per_day']}")

    return GuardResult(ok=not reasons, reasons=reasons, checks=checks)
