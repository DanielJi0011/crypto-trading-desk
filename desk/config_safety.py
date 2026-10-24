"""D1 第 4 点：配置安全校验（每次 settings.load_config 都执行，失败即抛 ConfigSafetyError，进程启动失败）。

规则：
1. guardrails / risk / swap 三节的每个键都必须登记在 ENFORCED（有代码读取并执行）或 INFO（非安全项，只作说明或成本估算）中；
   出现未登记的键 → 报错。不得通过删除配置项来消除冲突（任务书 §7 第 12 条），新增安全项必须同时实现读取并登记到这里。
2. 杠杆（Q11）：新开仓杠杆上限 = min(swap.leverage_max, guardrails.leverage)，guardrails.allow_leverage=false 时再夹到 1。
   allow_leverage=false 而 guardrails.leverage≠1 → 报错（两项互相矛盾）。
3. 不可关闭的安全项：require_stop_loss 必须为 true，allow_withdrawal 必须为 false，mode 必须为 paper。
4. 数值范围：risk.max_daily_loss_pct、risk.max_drawdown_pct、risk.risk_per_trade_pct 必须在 (0, 0.5]。
ENFORCED 的值是读取该键的源文件（tests/test_config_safety.py 扫描核实这些文件确实引用了该键名）。
"""
from __future__ import annotations


class ConfigSafetyError(RuntimeError):
    pass


ENFORCED = {
    "guardrails.max_position_pct": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.max_total_exposure_pct": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.max_total_notional_pct": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.require_stop_loss": ["desk/config_safety.py"],
    "guardrails.max_stop_loss_pct": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.max_daily_loss_pct": ["desk/demo_exec.py", "desk/entry_limit.py", "desk/ledger.py"],
    "guardrails.max_orders_per_day": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.max_open_positions": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.leverage": ["desk/config_safety.py"],
    "guardrails.allow_leverage": ["desk/config_safety.py"],
    "guardrails.allow_withdrawal": ["desk/ledger.py", "desk/config_safety.py"],
    "guardrails.min_order_usd": ["desk/demo_exec.py", "desk/ledger.py"],
    "guardrails.allowed_ord_types": ["desk/okx_demo.py"],
    "risk.max_daily_loss_pct": ["desk/equity_guard.py"],
    "risk.max_drawdown_pct": ["desk/equity_guard.py"],
    "risk.risk_per_trade_pct": ["desk/demo_exec.py", "desk/sizing.py"],
    "risk.max_notional_per_trade_pct": ["desk/sizing.py"],
    "risk.max_margin_pct": ["desk/sizing.py"],
    "risk.stop_atr_mult": ["desk/sizing.py"],
    "risk.stop_min_pct": ["desk/sizing.py"],
    "risk.stop_max_pct": ["desk/sizing.py"],
    "risk.tp_r_multiple": ["desk/sizing.py"],
    "risk.liq_buffer_atr_mult": ["desk/sizing.py"],
    "risk.max_open_risk_pct": ["desk/sizing.py"],
    "risk.max_net_directional_pct": ["desk/sizing.py"],
    "risk.cost_buffer_pct": ["desk/sizing.py"],
    "risk.plan_conflict_pct": ["desk/sizing.py"],
    "risk.max_price_drift_atr": ["desk/sizing.py", "desk/manager.py"],
    "risk.max_daily_bar_age_days": ["desk/sizing.py", "desk/manager.py"],
    "risk.stop_cooldown_hours": ["desk/stop_cooldown.py"],
    "swap.leverage_max": ["desk/config_safety.py", "desk/demo_exec.py"],
    "swap.maint_margin_rate": ["desk/demo_exec.py", "desk/manager.py"],
}
INFO = {
    "swap.fee_rate": "纸面账本手续费估算",
    "swap.funding_fallback_rate": "纸面账本资金费估算",
}
SECTIONS = ("guardrails", "risk", "swap")


def max_open_leverage(cfg) -> float:
    """新开仓允许的最大杠杆（Q11 默认按更严的 1 倍）。现有持仓不受影响（只用于开仓/挂单/限价补单前的检查与定杠杆）。"""
    g, sw = cfg.get("guardrails") or {}, cfg.get("swap") or {}
    cap = min(float(sw.get("leverage_max", 1)), float(g.get("leverage", 1)))
    if not g.get("allow_leverage", False):
        cap = min(cap, 1.0)
    return max(cap, 1.0)


def validate(cfg) -> None:
    errs = []
    for sec in SECTIONS:
        for k in (cfg.get(sec) or {}):
            name = f"{sec}.{k}"
            if name not in ENFORCED and name not in INFO:
                errs.append(f"{name} 存在但没有代码读取执行（未登记到 desk/config_safety.py）")
    g, rk = cfg.get("guardrails") or {}, cfg.get("risk") or {}
    if "leverage" not in g or "allow_leverage" not in g:
        errs.append("guardrails.leverage / guardrails.allow_leverage 必须显式配置（Q11）")
    elif not g.get("allow_leverage") and float(g["leverage"]) != 1:
        errs.append(f"guardrails.allow_leverage=false 与 guardrails.leverage={g['leverage']} 矛盾")
    if g.get("require_stop_loss") is not True:
        errs.append("guardrails.require_stop_loss 必须为 true（止损不可关闭）")
    if g.get("allow_withdrawal"):
        errs.append("guardrails.allow_withdrawal 必须为 false")
    if cfg.get("mode", "paper") != "paper":
        errs.append("mode 必须为 paper（本版本禁止实盘）")
    for k in ("max_daily_loss_pct", "max_drawdown_pct", "risk_per_trade_pct"):
        if k in rk and not (0 < float(rk[k]) <= 0.5):
            errs.append(f"risk.{k}={rk[k]} 超出 (0, 0.5]（单位是小数，0.025 即 2.5%）")
    if errs:
        raise ConfigSafetyError("配置安全校验失败：" + "；".join(errs))
