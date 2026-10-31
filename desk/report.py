"""中文日报：reports/YYYY-MM-DD.md"""
from __future__ import annotations

from desk import ledger, scoring, settings
from desk.manager import ACT_CN, weights
from desk.roles import ROLE_CN, ROLE_KEYS, read_meta

CN = {**ROLE_CN, "trade_manager": "交易经理"}
BASIS_CN = {"initial": "初始权益", "live": "实时权益"}
DIR_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性", "close": "平仓"}


def _num(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _sizing_line(z, agg=None) -> str:
    """R1：日报显示风险预算、名义、杠杆、止损亏损与被哪条上限削减。
    兼容两种 sizing 结构：旧路径 sizing.size（risk_usd/stop_loss_usd 齐全）与交易经理下单工具路径
    （manager._tool_contracts：只有 how/notional/leverage/sz/size_pct/risk_scale 或 margin_pct）。
    缺字段时用开仓价、止损与名义（=张数×面值×价格）现算止损亏损；算不出写“按交易经理指定”。任何缺字段都不抛异常。"""
    if not z or not isinstance(z, dict):
        return ""
    if not z.get("ok"):
        return f"；定仓拒绝 {z.get('reason') or '—'}"
    agg = agg or {}
    clip = "、".join(str((c or {}).get("limit", c)) if isinstance(c, dict) else str(c) for c in z.get("clipped") or [])
    notional, lev = _num(z.get("notional")), _num(z.get("leverage"))
    entry, stop = _num(agg.get("entry_px")), _num(agg.get("stop_loss"))
    loss = _num(z.get("stop_loss_usd"))
    if loss is None and notional and entry and stop:
        loss = notional * abs(entry - stop) / entry
    risk = _num(z.get("risk_usd"))
    if z.get("how") == "tm_margin_pct":
        basis = f"交易经理 margin_pct {_f(z.get('margin_pct'), 2)}%"
    elif z.get("how") == "risk_sizing":
        basis = f"按风险定仓×risk_scale {_f(z.get('risk_scale'), 2)}"
    else:
        basis = None
    parts = []
    if basis:
        parts.append(f"定仓 {basis}")
    parts.append(f"风险预算 {risk:.2f} USDT" if risk is not None else "风险预算 按交易经理指定")
    if z.get("sz") not in (None, ""):
        parts.append(f"张数 {z['sz']}")
    parts.append(f"名义 {notional:.2f}" if notional is not None else "名义 —")
    parts.append(f"杠杆 {lev:g}x" if lev is not None else "杠杆 —")
    if loss is not None:
        parts.append(f"止损亏损 {loss:.2f}" + ("（按入场价与止损现算）" if _num(z.get("stop_loss_usd")) is None else ""))
    else:
        parts.append("止损亏损 按交易经理指定")
    return "；" + "，".join(parts) + (f"，被上限削减：{clip}" if clip else "")


def model_note(r: dict) -> str:
    """S1：经第三方中转站的角色（grok/gpt/claude 且未回退）在模型分配表中标注。"""
    relay = "经第三方中转站（提示词与数据经过中转方）" if r.get("provider") in ("grok", "gpt", "claude") and not r.get("fallback") else ""
    return "；".join(x for x in (relay, ("⚠️ " + r["fallback"]) if r.get("fallback") else "") if x)


def _f(x, nd=2, pct=False):
    if x is None or x == "":
        return "—"
    try:
        return f"{float(x) * 100:.0f}%" if pct else f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _px(x):
    if x in (None, ""):
        return "—"
    x = float(x)
    return f"{x:,.2f}" if x >= 100 else f"{x:,.4f}"


def limit_px(t: dict, m: dict | None = None) -> str:
    """条件单触发后限价：优先交易所挂单的 ordPx / orderPx，其次登记表 order_px；-1 → 市价。"""
    for v in (t.get("ordPx"), t.get("orderPx"), (m or {}).get("order_px")):
        if v not in (None, ""):
            return "市价" if str(v).strip() in ("-1", "-1.0") else str(v)
    return "—"


def calls_text(u: dict) -> str:
    """调用次数文案：共 N 次，成功 S 次，失败 F 次（usage['calls'] 只计成功调用）。"""
    ok, bad = int(u.get("calls", 0)), int(u.get("errors", 0))
    return f"共 {ok + bad} 次，成功 {ok} 次，失败 {bad} 次"


def _cost(v: dict) -> str:
    n = v.get("unpriced_calls", 0)
    if n and n >= v.get("calls", 0):
        return "单价未知"
    return f"{v['cost_usd']:.4f}" + (f"（另 {n} 次单价未知）" if n else "")


def _exec_title(d: dict) -> str:
    """标题按实际执行结果写：只有真正下单/挂单成功才写仓位；被护栏拒绝或观望的写“未下单”。"""
    act, ex, sz = str(d.get("final_action", "")), d.get("execution") or {}, d.get("size_pct")
    if not sz:
        return ""
    if act.startswith("hold") or "拒" in act or (ex and ex.get("ok") is False):
        return f" · 未下单（计划保证金 {sz:.1f}%）"
    return f" · 仓位 {sz:.1f}%"


def write(cfg, date, decisions, closed, reviews, weight_changes, pb_events, usage, prices, errors, mock=False,
          research=None, name=None, coin_notes=None, prices_time=None, macro=None, banner=None) -> str:
    """coin_notes：{inst_id: [补记行]}，写进该币决策段（无决策时单列一段）；交易经理否决导致的撤单会从 demo_events 自动补记。"""
    from desk import providers
    from desk import llm as dllm
    from desk import demo_exec
    demo = demo_exec.is_demo(cfg)
    try:
        bk = demo_exec.book(cfg, prices) if demo else ledger.book(cfg, prices)
    except Exception as e:
        errors = list(errors) + [f"读取模拟盘账户失败：{e}（日报账户部分改用纸面账本）"]
        demo, bk = False, ledger.book(cfg, prices)
    prices_time = prices_time or settings.now_str()
    for p in bk["positions"]:   # 统一现价：持仓现价/浮盈亏都按本报告同一次 last 快照重算（交易所 upl 按标记价，口径不同）
        px = (prices or {}).get(p["inst_id"])
        if px and p.get("avg_px") and p.get("qty"):
            p["price"] = float(px)
            p["upnl"] = (1 if p["side"] == "long" else -1) * (float(px) - float(p["avg_px"])) * float(p["qty"])
            p["notional"] = float(p["qty"]) * float(px)
    trades = ledger.store.read_trades()
    realized = sum(float(t["pnl_usd"] or 0) for t in trades if t["status"] == "closed")
    today_real = sum(float(t["pnl_usd"] or 0) for t in trades if t["status"] == "closed" and t["exit_time"].startswith(date))
    w = weights(cfg)
    L = [f"# 多角色交易台日报 · {date}", "",
         *([f'<span style="color:red">**🛑 {banner}**</span>', ""] if banner else []),
         f"> 生成时间 {settings.now_str()}（北京时间）· 模式：" + ("**OKX 模拟盘直连（Demo Trading，x-simulated-trading: 1）· USDT 永续逐仓**，禁止实盘" if demo else "**纸面交易（paper）· OKX USDT 永续逐仓模拟**，不下真实订单")
         + ("· ⚠️ 本次为模拟 LLM 演练，结论无意义" if mock else ""), "",
         "## 一、账户概览" + ("（OKX 模拟盘）" if demo else "（纸面）"), "",
         *([f"权益基准（{bk['equity_basis']}）{_f(bk['equity'])} USDT；模拟盘实时权益 {_f(bk['live_equity'])} USDT；初始权益 {_f(bk['initial'])} USDT；"
            f"下表“累计/今日已实现”仅为旧纸面仓。", ""] if demo else []),
         f"总名义占比的分母 = 权益基准（execution.equity_basis={bk.get('equity_basis', 'paper')}）{_f(bk['equity'])} USDT。", "",
         "| 权益 USDT | 可用现金 | 总名义 | 总名义占比 | 累计已实现 | 今日已实现 | 累计收益率 |", "|---|---|---|---|---|---|---|",
         f"| {_f(bk.get('live_equity', bk['equity']))} | {_f(bk['cash'])} | {_f(bk['exposure'])} | 占{BASIS_CN.get(bk.get('equity_basis'), '权益基准')} {_f(bk['equity'])} 的 {bk['exposure'] / bk['equity'] * 100:.1f}% | "
         f"{realized:+.2f} | {today_real:+.2f} | {(bk.get('live_equity', bk['equity']) / bk['initial'] - 1) * 100:+.2f}% |", ""]
    _nc = [d for d in decisions if d.get("tm_status") == "absent"]
    if _nc:   # 今日未开仓说明：交易经理无结论的币放在最前
        _i = next((i for i, x in enumerate(L) if str(x).startswith("## 一、")), len(L))
        L[_i:_i] = ["## 今日未开仓说明（交易经理无结论）", ""] + [
            f"- {(d.get('inst_id') or '').split('-')[0]}（{d.get('decision_id')}）：交易经理无结论，不开仓；原因：{d.get('tm_absent_reason') or '调用失败'}"
            for d in _nc] + [""]
    if macro:
        try:
            from desk import macro as _macro
            L += _macro.report_lines(macro)
        except Exception as e:
            L += ["## 宏观数据（FRED）", "", f"宏观小节生成失败：{type(e).__name__}", ""]
    L += ["## 二、当前持仓", "", f"现价 = OKX 公共行情最新成交价（last），本报告统一使用 {prices_time} 的同一次快照；"
          "浮盈亏 =（现价 − 均价）× 数量 × 方向，按该快照重算（不含手续费）。第四节“决策时价格”是各币决策当时的价格。", ""]
    if bk["positions"]:
        L += ["| 编号 | 品种 | 方向 | 杠杆 | 保证金 | 名义 | 数量 | 均价 | 现价 | 止损 | 止盈 | 强平价 | 资金费 | 浮盈亏 | 到期 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in bk["positions"]:
            L.append(f"| {p['trade_id']}{'（旧纸面仓）' if p.get('legacy') else ''} | {p['inst_id']} | {'空' if p['side'] == 'short' else '多'} | {p['leverage']:g}x | {p['margin']:.2f} | "
                     f"{p['notional']:.2f} | {p['qty']:.6g} | {_px(p['avg_px'])} | {_px(p['price'])} | {_px(p['sl_px'])} | "
                     f"{_px(p['tp_px'])} | {_px(p['liq_px'])} | {p['funding']:+.4f} | {p['upnl']:+.2f} | {p['deadline'] or '—'} |")
    else:
        L.append("无持仓。")
    from desk import pending as _pend
    L += ["", "## 二·五、条件入场单与触发事件", ""]
    if demo:
        rg = demo_exec.reg()["triggers"]
        if bk["triggers"]:
            L += ["| 模拟盘算法单 | 品种 | 方向 | 张数 | 触发价 | 触发后限价 | 止损 | 止盈 | 来源 | 到期 |", "|---|---|---|---|---|---|---|---|---|---|"]
            for t in bk["triggers"]:
                at = (t.get("attachAlgoOrds") or [{}])[0]; m = rg.get(t["algoId"], {})
                L.append(f"| {t['algoId']} | {t['instId']} | {t.get('posSide') or t['side']} | {t['sz']} | {t['triggerPx']} | {limit_px(t, m)} | "
                         f"{at.get('slTriggerPx') or '—'} | {at.get('tpTriggerPx') or '—'} | {m.get('decision_id') or '—'} | {m.get('expires') or '—'} |")
        else:
            L.append("模拟盘当前无条件单。")
        from desk import okx_demo as _od
        ev = [e for e in settings.read_jsonl(_od.EVENTS) if str(e.get("time", "")).startswith(date) and e.get("event") != "verify_ok"]
        if ev:
            L += ["", "今日模拟盘事件：", ""] + [f"- {e['time'][11:19]} **{e['event']}** " + str({k: v for k, v in e.items() if k not in ('time', 'event')}) for e in ev[-40:]]
        from desk import expiry as _exp
        _el = _exp.report_lines(date)
        if _el:
            L += [""] + _el
    pe = {} if demo else _pend.load()
    if pe:
        L += ["| 品种 | 方向 | 杠杆 | 条件 | 触发价 | 止损 | 止盈 | 保证金% | 共识S | 来源 | 到期 | 理由 |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for inst, o in pe.items():
            L.append(f"| {inst} | {'空' if o.get('side') == 'short' else '多'} | {o.get('leverage', 1)}x | {'≥' if o['condition'] == 'gte' else '≤'} | "
                     f"{_px(o['trigger_px'])} | {_px(o['stop_loss'])} | {_px(o.get('take_profit'))} | {o['size_pct']:.1f} | "
                     f"{o.get('consensus')} | {o.get('source')} | {o['expires']} | {(o.get('reason') or '').replace('|', '/')[:50]} |")
    elif not demo:
        L.append("当前无挂单。")
    ev = [] if demo else _pend.events(date)
    if ev:
        L += ["", "今日事件：", ""]
        for e in ev:
            extra = {k: v for k, v in e.items() if k not in ("time", "date", "type", "inst_id", "role_levels")}
            L.append(f"- {e['time'][11:19]} {e['inst_id']} **{e['type']}** {extra}")
    L += ["", "## 三、今日平仓与复盘会", ""]
    if closed:
        L += ["| 编号 | 品种 | 入场 | 出场 | 原因 | 盈亏 USDT | 盈亏% |", "|---|---|---|---|---|---|---|"]
        for t in closed:
            L.append(f"| {t['trade_id']} | {t['inst_id']} | {_px(t['entry_px'])} | {_px(t['exit_px'])} | {t['exit_reason']} | "
                     f"{float(t['pnl_usd']):+.2f} | {float(t['pnl_pct']):+.2f}% |")
        for rv in reviews:
            L += ["", f"### 复盘：{rv['trade_id']}", ""]
            tl = rv.get("tm_line")
            if tl:   # 方案一：复盘主线是交易经理这一单
                L += [f"- 主线（交易经理）：动作 {tl.get('action') or '—'}（{tl.get('decision_type') or '—'}），入场 {_px(tl.get('entry_px'))}，"
                      f"止损 {_px(tl.get('sl_px'))}，止盈 {_px(tl.get('tp_px'))}，出场 {_px(tl.get('exit_px'))}（{tl.get('exit_reason') or '—'}），"
                      f"盈亏 {float(tl.get('pnl_usd') or 0):+.2f} USDT，实际盈亏比 {_f(tl.get('realized_rr'))}"
                      + ("，当时否决过共识" if tl.get("vetoed_consensus") else ""), "- 下表各角色方向对错只作参考。", ""]
            L += [rv.get("summary", ""), "",
                  "| 角色 | 当时观点 | 置信 | 对错（代码判定） | 复盘会判定 | 教训 |", "|---|---|---|---|---|---|"]
            vd = {v["role"]: v for v in rv.get("verdicts", [])}
            for t in rv["table"]:
                v = vd.get(t["role"], {})
                L.append(f"| {ROLE_CN[t['role']]} | {DIR_CN[t['direction']]} | {_f(t['confidence'])} | "
                         f"{'—' if t['right'] is None else ('✅' if t['right'] else '❌')} | {v.get('verdict', '—')} | {v.get('lesson', '—')} |")
            if rv.get("manager_lesson"):
                L.append(f"\n交易经理教训：{rv['manager_lesson']}")
    else:
        L.append("今日无平仓。")
    from desk import tm_ledger as _tml
    if _tml.enabled(cfg):
        L += [""] + _tml.report_lines(cfg)
    notes_by = {k: list(v) for k, v in (coin_notes or {}).items()}
    if demo:   # 交易经理否决导致的撤单：自动补记（已在 coin_notes 里提到的单号不重复）
        from desk import okx_demo as _od2
        for e in settings.read_jsonl(_od2.EVENTS):
            if e.get("event") == "trigger_cancelled" and str(e.get("time", "")).startswith(date) and "交易经理否决" in str(e.get("why", "")):
                lst = notes_by.setdefault(e.get("inst_id"), [])
                if not any(str(e.get("algoId")) in x for x in lst):
                    lst.append(f"{e['time'][11:16]} 撤单 {e.get('algoId')}，原因：{e.get('why')}")
    L += ["", "## 四、今日决策", ""]
    for d in decisions:
        m = d["manager"]
        ex = d.get("execution") or {}
        L += [f"### {d['inst_id']} · 最终：**{ACT_CN.get(d['final_action'], d['final_action'])}**"
              + _exec_title(d), "",
              f"- 决策时价格 {_px(d['price'])}（{str(d.get('time', ''))[11:16] or '—'}）；20日 {d['market']['ret_20d']:+.2f}%（市况：{scoring.regime_of(d['market']['ret_20d'], cfg)}）；"
              f"ATR14 {_px(d['market']['atr14'])}",
              f"- TradingAgents 组合经理评级：{d.get('ta_signal') or '—'}；角色加权共识 S = {d['consensus']:+.3f}",
              f"- 加权聚合（组合经理只执行）：方向 {({'long': '做多', 'short': '做空'}).get((d.get('aggregate') or {}).get('side'), '不交易')}，"
              f"入场 {_px((d.get('aggregate') or {}).get('entry_px'))}（{(d.get('aggregate') or {}).get('condition', '—')}），"
              f"止损 {_px((d.get('aggregate') or {}).get('stop_loss'))}，止盈 {_px((d.get('aggregate') or {}).get('take_profit'))}，"
              f"保证金 {_f((d.get('aggregate') or {}).get('size_pct') or 0, 1)}%，杠杆 {(d.get('aggregate') or {}).get('leverage', '—')}"
              + _sizing_line((d.get('aggregate') or {}).get('sizing'), d.get('aggregate') or {}),
              f"- 交易经理（{'不投票，定点位' if 'trade_manager' not in d.get('weights', {}) and d.get('decided_by') else '投票角色之一'}）建议：{('条件入场计划（' + ('多' if m.get('entry_side') == 'long' else '空') + ' ' + str(m.get('entry_trigger_px')) + '）') if m['action'] == 'pending' else ACT_CN.get(m['action'], m['action'])}，仓位 {m.get('size_pct')}%，入场 {_px(m.get('entry'))}，"
              f"止损 {_px(m.get('stop_loss'))}，止盈 {_px(m.get('take_profit'))}，周期 {m.get('horizon_days')} 天，置信 {m.get('confidence')}",
              f"- 失效条件：{m.get('invalidation') or '—'}",
              f"- 理由：{m.get('rationale')}"]
        if d.get("add_preview"):
            from desk.manager import describe_add_preview as _dap
            L.append("- " + _dap(d["add_preview"]))
        _add = (d.get("aggregate") or {}).get("add")
        if _add:
            from desk import add_position as _AP
            L.append("- 同向加仓决定：" + _AP.describe(_add))
        if d.get("tm_status") == "veto":
            L.append("- **交易经理否决**：本币不下现价单、不挂条件单、不加仓；按 cancel_pending_on_new_decision 撤旧条件单（受保护的不撤），已有持仓不动")
        elif d.get("tm_status") == "absent":
            _why = d.get("tm_absent_reason") or "调用失败"
            L.append(f"- **交易经理无结论，不开仓**（原因：{_why}）：不新开、不加仓、不挂条件单；这一票不计入共识（不按中性 0.5 计），也不算否决；已有持仓的止损、到期回退平仓照常")
        if (d.get("aggregate") or {}).get("tm_no_conclusion"):
            L.append("- **交易经理无同向有效入场结论，不开仓**：未给与共识同向的现价单或完整条件单，不回退现价 + ATR+2R 基准开仓")
        if d.get("tm_label_mismatch"):
            L.append("- ⚠️ 交易经理字段冲突（以动作标签为准）：" + d.get("tm_label_mismatch"))
        if d.get("field_missing"):
            L.append("- ⚠️ 交易经理字段缺失：" + "、".join(d["field_missing"]))
        for r, c in (d.get("stance_field_conflicts") or {}).items():
            L.append(f"- ⚠️ {ROLE_CN.get(r, r)} 字段冲突：{c}")
        if d.get("drift_skipped"):
            from desk import drift as _DR
            L.append("- " + _DR.describe(d["drift_skipped"]))
        elif (d.get("drift_check") or {}).get("ok") is None and d.get("drift_check"):
            L.append("- ⚠️ 漂移检查取现价失败，未阻断（" + str(d["drift_check"].get("error"))[:80] + "）")
        if d["notes"]:
            L.append("- 代码规则/护栏：" + "；".join(d["notes"]))
        L += [f"- 补记：{x}" for x in notes_by.pop(d["inst_id"], [])]
        if ex:
            if demo and ex.get("type") == "open" and ex.get("ok"):
                L.append(f"- 模拟盘市价开{'空' if ex.get('side') == 'short' else '多'} {ex.get('sz')} 张（≈{ex['qty']:.6g} 币）@≈{_px(ex['px'])}，{ex.get('leverage', 1):g}x，"
                         f"名义≈{ex.get('notional', 0):.2f}，订单 {ex.get('ord_id')}")
            elif demo and ex.get("type") in ("close", "reduce"):
                L.append(f"- 模拟盘平仓/减仓（{ex.get('trade_id')}），平前浮盈亏≈{ex.get('pnl', 0):+.2f} USDT")
            elif ex.get("type") == "open" and ex.get("ok"):
                L.append(f"- 纸面成交：开{'空' if ex.get('side') == 'short' else '多'} {ex['qty']:.8g} @ {_px(ex['px'])}，{ex.get('leverage', 1):g}x，"
                         f"保证金 {ex.get('margin', 0):.2f}，名义 {ex.get('notional', 0):.2f}，强平价 {_px(ex.get('liq_px'))}，手续费 {ex['fee']:.4f} USDT（{ex['trade_id']}）")
            elif ex.get("type") in ("close", "reduce"):
                L.append(f"- 纸面成交：平仓/减仓 @ {_px(ex['px'])}，盈亏 {ex['pnl']:+.2f} USDT（{ex['trade_id']}）")
        L += ["", "| 角色 | 权重 | 观点 | 置信 | 目标价 | 止损价 | 建议入场 | 周期 | 要点 | 来源 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in ROLE_KEYS + (["trade_manager"] if "trade_manager" in d["stances"] else []):
            s = d["stances"][r]
            wt = "执行（不投票）" if r == "portfolio_manager" else f"{d['weights'].get(r, 1.0):.2f}"
            L.append(f"| {CN.get(r, r)} | {wt} | {DIR_CN[s['direction']]} | {_f(s.get('confidence'))} | {_px(s.get('target'))} | "
                     f"{_px(s.get('stop'))} | {_px(s.get('entry_px'))}{({'gte': '↑', 'lte': '↓', 'now': '现价'}).get(s.get('entry_condition'), '')} | {s.get('horizon_days') or '—'} | {(s.get('reason') or '').replace('|', '/')[:60]} | {s.get('source', '')} |")
        L.append("")
    for inst, xs in notes_by.items():
        if xs:
            L += [f"### {inst} · 补记", ""] + [f"- {x}" for x in xs] + [""]
    if errors:
        L += ["### 运行错误", ""] + [f"- {e}" for e in errors] + [""]
    L += ["## 五、角色成绩单（打分 v2：各观点按自己周期打分，中性带宽随 ATR 变化；skill=命中率−基础率）", "",
          "| 角色 | 实用权重 | 影子权重 | 手册版本 | 已打分 | 命中率 | 近20 | 上涨市 | 下跌市 | 震荡市 | 止损有效/被洗出 | skill | Brier | 自带方案R(n) | 未触发 | 方向对齐 USDT |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    w_sh = settings.read_json(scoring.WEIGHTS_SHADOW, {})   # L1：学习冻结时实际决策用实用权重，影子权重只展示
    st_by = {x["decision_id"]: x["stances"] for x in settings.read_jsonl(settings.STATE / "decisions.jsonl")}
    for r in [k for k in ROLE_KEYS if k != "portfolio_manager"] + ["trade_manager"]:   # 组合经理只执行不投票，不列
        c = scoring.scorecard(r, trades, st_by)
        br = c["by_regime"]
        L.append(f"| {CN[r]} | {format(w[r], '.2f') if r in w else '不计（不投票）'} | {_f(w_sh.get(r))} | v{read_meta(r)['current']:03d} | {c['n']} | {_f(c['hit'], pct=True)} | "
                 f"{_f(c['recent']['hit'], pct=True)} | {_f(br['上涨']['hit'], pct=True)}({br['上涨']['n']}) | "
                 f"{_f(br['下跌']['hit'], pct=True)}({br['下跌']['n']}) | {_f(br['震荡']['hit'], pct=True)}({br['震荡']['n']}) | "
                 f"{c['stops']['止损有效']}/{c['stops']['被洗出']} | {_f(c.get('skill'), 3)} | {_f(c.get('brier'), 3)} | "
                 f"{_f(c['plan_r']['mean'])}({c['plan_r']['n']}) | {c['not_triggered']} | {c['pnl_attr']:+.2f} |")
    n_sc = len(scoring.scores())
    L += ["", "说明：方向对齐 = 交易盈亏 × 该角色当时方向，只表示同向程度，不是因果归因；影子权重按收缩公式 "
              f"w0·clip(exp(k·Σskill/(n+n0)), 0.5, 2)，n0={cfg['scoring'].get('n0', 30)}，k={cfg['scoring'].get('k', 4)}，同日多币算一个样本。",
          f"累计已打分观点 {n_sc} 条。"
              f"手册修订需≥{cfg['learning']['min_scored_before_edit']} 条新打分，每次最多 {cfg['learning']['max_changes_per_edit']} 处。", ""]
    L += ["## 六、角色手册与权重变动", ""]
    if not weight_changes and not pb_events:
        L.append("今日无变动。")
    for c in weight_changes:
        L.append(f"- 权重：{CN.get(c['role'], c['role'])} {c['old']:.2f} → {c['new']:.2f}（依据 {c['n']} 条打分）")
    for e in pb_events:
        if e["type"] == "修订":
            L.append(f"- 手册修订：{ROLE_CN[e['role']]} → v{e['version']:03d}（修订前近20命中 {_f(e['baseline_hit'], pct=True)}）")
            L += [f"  - {c['op']}：{c['rule']}（依据：{c['why']}）" for c in e["changes"]]
        elif e["type"] == "建议":
            L.append(f"- 修订建议（未生效）：{ROLE_CN[e['role']]} → playbooks/{e['role']}/proposals/{e['file']}")
        elif e["type"] == "回滚":
            L.append(f"- 自动回滚：{ROLE_CN[e['role']]} v{e['from']:03d} → v{e['to']:03d}（修订前 {_f(e['base'], pct=True)}，修订后 {_f(e['post'], pct=True)}）")
        elif e["type"] == "保留":
            L.append(f"- 观察期结束保留：{ROLE_CN[e['role']]} v{e['version']:03d}（{_f(e['base'], pct=True)} → {_f(e['post'], pct=True)}）")
        else:
            L.append(f"- {ROLE_CN[e['role']]}：{e['type']} {e.get('why', '')}")
    if research:
        L += ["", f"### 每周自研（{research['week']}）", "",
              f"详见 research/report-{research['week']}.md；本次研究成本约 ${research.get('cost_usd', 0):.4f}，tokens {research.get('tokens', 0):,}"
              + (f"；{research['stopped']}" if research.get("stopped") else "")]
        from desk.research import ROLE_FEATURE_NOTES
        for role, x in research["roles"].items():
            t = x.get("tested", [])
            if role in ROLE_FEATURE_NOTES:
                L.append(f"- 说明：{ROLE_FEATURE_NOTES[role]}")
            L.append(f"- {ROLE_CN[role]}：检验 {sum(1 for r in t if not r.get('skipped'))} 条，通过 {sum(1 for r in t if r['passed'])} 条" + (f"，未知方向跳过 {sum(1 for r in t if r.get('skipped'))} 条" if any(r.get('skipped') for r in t) else "") + (f"（{x['error']}）" if "error" in x else "") + (f"（{x['note']}）" if x.get("note") else ""))
            for r in t:
                sv = str((r.get("hypothesis") or {}).get("side") or "").lower()
                sd = {"long": "做多", "short": "做空"}.get(sv, "未知")
                res = "⏭️未知方向，跳过检验" if r.get("skipped") or sd == "未知" else ("✅通过" if r["passed"] else ("🔍探索通过，待季度确认" if r.get("explore_passed") else "❌未通过"))
                L.append(f"  - [{sd}] {r['id']}" + ("（菜单违规，仍计入检验数）" if r.get("menu_violation") else "") + f" {(r.get('hypothesis') or {}).get('name', '')}：{res}")
    try:
        from desk import baselines
        L += [""] + baselines.report_lines(cfg)
    except Exception as e:
        L += ["", f"对照组小节生成失败：{type(e).__name__}"]
    L += ["", "## 七、模型分配", "", "| 角色 | 厂商 | 模型 | 备注 |", "|---|---|---|---|"]
    for r in providers.assignment_table(cfg):
        L.append(f"| {ROLE_CN.get(r['role'], r['role'])} | {r['provider']} | {r['model']} | {model_note(r)} |")
    for fb in providers.FALLBACKS:
        L.append(f"\n⚠️ 本次回退：{fb}")
    L += ["", "## 八、LLM 用量与成本（本次运行）", "",
          f"{calls_text(usage)}；输入 {usage['input']:,} tokens（缓存命中 {usage['cached']:,}），"
          f"输出 {usage['output']:,} tokens；已知价格部分（DeepSeek）估算成本 **${usage['cost_usd']:.4f}**（约 ¥{usage['cost_usd'] * cfg['llm'].get('usd_cny', 7.2):.3f}）"
          + (f"；另有 {usage['unpriced_calls']} 次中转（grok/gpt/claude）调用单价未知，token 已统计、成本未计入" if usage.get("unpriced_calls") else "") + "。", "",
          "| 环节 | 调用 | 输入 | 缓存命中 | 输出 | 成本 USD |", "|---|---|---|---|---|---|"]
    for k, v in usage["by_stage"].items():
        L.append(f"| {k} | {v['calls']} | {v['input']:,} | {v['cached']:,} | {v['output']:,} | {_cost(v)} |")
    L += ["", "| 厂商 | 调用 | 输入 | 缓存命中 | 输出 | 成本 USD |", "|---|---|---|---|---|---|"]
    for k, v in usage.get("by_provider", {}).items():
        L.append(f"| {k} | {v['calls']} | {v['input']:,} | {v['cached']:,} | {v['output']:,} | "
                 + (f"{v['cost_usd']:.4f}" if k == "deepseek" else "单价未知（中转站价格未提供，仅统计 token）") + " |")
    L += ["", "| 模型（接口返回） | 调用 | 输入 | 输出 | 成本 USD |", "|---|---|---|---|---|"]
    for k, v in usage["by_model"].items():
        L.append(f"| {k} | {v['calls']} | {v['input']:,} | {v['output']:,} | "
                 + (f"{v['cost_usd']:.4f}" if dllm.price_known(cfg, k) else "单价未知") + " |")
    L += ["", "---", "风险提示：以上为 OKX 模拟盘/纸面模拟结果，不构成投资建议；系统禁止实盘、不会下真实订单。"]
    settings.REPORTS.mkdir(parents=True, exist_ok=True)
    path = settings.REPORTS / f"{name or date}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return str(path)
