"""行情判定（2026-11-11 用户决定：以 LLM 判定取代 A+ 的代码机械判定 trend_regime）。

分工：代码只算数据——趋势特征表（均线/斜率/偏离/RSI/区间位置，均由已收盘日线经现有
okx_data 路径计算）；判定由与交易经理同一模型席位（role_models.trade_manager）的 LLM 在每场
TM 决策前做一次，输出严格结构化结果。代码再按判定执行趋势市近价护栏与末段衰竭降级
（desk/tm_order_tool.validate §3.5），判定本身不进共识、不影响否决口径。
判定失败/不可解析 → 本场本币按「非趋势市」处理（规则不生效），决策记录记 regime_judge_failed
数据缺口，绝不因判定失败中断场次。总开关 manager.trend_rule_enabled=false 时整段关闭
（不调判定、不注入提示词、护栏同时失效，回到 A+ 之前行为）。
每次判定（含特征快照）另存 state/ledger/regime_calls.jsonl，供日后与实际走势对照打分（打分未建）。

判定标准 v2（judge_standard=2，2026-11-14 用户批准）：审计发现 v1 标准下 18 次场次判定 16 次
late_exhaustion、事后 8 次独立观测 8/8 沿趋势延续、衰竭证据恒收敛于 RSI 超卖/区间底部/偏离过深/
短期跌幅四个「伸展变量」，肯定式衰竭证据引用 0/18——stage 边界实质是拉伸程度阈值而非趋势失败
检验。v2 改为：① late_exhaustion 必须给出正面衰竭证据 exhaustion_signals（五项闭集，见下）；
② RSI/区间位置/偏离/短期涨跌幅归类为伸展变量，只支持 direction/strength，不许推出 late；
③ early/mid 给正面定义，结构完整时默认 mid；④ 代码门（apply_stage_gate）：返回 late 但
exhaustion_signals 无任何闭集内有效值 → 生效 stage 强制降为 mid，原始 stage 与覆盖标记随判定
一并存档——纯提示词约束已被审计证明会被模型绕过（自述「动量未衰竭」仍判衰竭），故由代码裁定。
v1 存档记录无 judge_standard 字段，打分时按 v1 分段。

判定标准 v3（judge_standard=3，2026-11-14 用户批准，原周日议程提前）：v2 的三项已知弱点（设计说明
§7.2/§7.3）与资金面盲区一并补强。① 特征表扩建：MA20/MA50 补「5 日前同口径 5 日斜率」，slope_flattening
从单时点近似改为斜率历史实测（收窄 ≥30% 或近零）；最新日量能 z 值与近 5 日最大 z 值入表，volume_stall
改挂 climax 阈值（z≥2.0/2.5）+ 价格推进（×ATR14）双条件；资金费率（当前值 + 近约 90 日分位）与持仓量
（当前 USD + 5 日变化）入表，清算量取最近样本作附加项。② 新增第六项衰竭证据 forced_flow_exhaustion
（资金面衰竭）：费率极端分位（下跌看 ≤10%、上涨看 ≥90%）与 OI 5 日萎缩 ≤−10% 双硬门槛同时满足才可
引用。③ 代码门扩展：本场资金面特征缺失（flow_missing）时 forced_flow_exhaustion 视为无效信号被剔除
（raw 保留审计），其余五项门逻辑不变。资金面抓取全程 fail-soft：失败只标 missing，判定照常进行。
数据源只用 OKX 公共接口（经 desk.flows 的 URL 缓存抓取，与资金流分析师共用缓存，克制请求数）。

周线方向过滤（决策 2，2026-11-15 用户批准）：周期框架定稿——日线波段为主框架；周线只作方向过滤，
不作判定输入。代码按完整周收盘算周线方向（由特征表同一批已收盘日线按 ISO 周聚合，未走完的周不算；
收盘 vs 周 MA20 位置与 MA20 五周斜率同向才算趋势），随特征表一并渲染进判定/TM 提示词。执行层规则
= 逆周线方向开新仓受约束：validate §3.7 经 weekly_block_reason 裁定，作用域同 §3.5（open/pending/
replace_pending；keep_pending 沿用原单、add 为已有持仓加仓腿，不在此列）。与 LLM 判定是否生效无关
（周线是纯代码数据，判定失败但特征表在时过滤照常生效）；周线中性/数据缺失/开关关闭一律不拦
（fail-soft）。周线方向与本场被拦方向随 regime_calls 存档（weekly_direction / weekly_blocked_side，
stage_overridden 式记账）。开关 manager.weekly_filter.enabled（默认开）；整层仍受 trend_rule_enabled
总开关约束（manager 只在总开关开时构建 regime，validate 也只从 regime 读周线状态）。
2026-11-21 门收敛（用户批准）：新增 manager.weekly_filter.mode —— advisory（默认）时 §3.7 不再
硬拦，只计算、披露、留痕（逆周线放行记 [WEEKLY_DIR] 咨询留痕进决策记录）；mode=block 恢复本段
上文的原硬拦行为。weekly_block_reason 本身不变（仍返回「若硬拦时的拦截说明」），由 validate
按 mode 决定拦截还是留痕。

4H 入场结构（2026-11-15 用户批准）：周期框架里 4H 只做入场触发微调——本改动给它一份窄的、代码化
的活：入场触发价不得落进当前 4H 波段内部。代码按已收盘 4H K 线（与分析师参考文本同一缓存路径）
算最近确认波段高/低点（左右各 2 根的严格枢轴，右侧未走完的枢轴不算确认），随特征表一并渲染进
判定/TM 提示词；但判定本身仍只看日线，4H 与周线同为执行层数据、不进判定口径。执行层硬规则 =
做空触发价须 ≥ 最近确认 4H 波段高点、做多触发价须 ≤ 最近确认 4H 波段低点（恰等于可过）：validate
§3.8 经 h4_block_reason 裁定，作用域同 §3.7（open/pending/replace_pending；open 以现价为入场价比对；
add 加仓腿、keep_pending 沿用原单不在此列）。与 LLM 判定是否生效无关；波段数据缺失/开关关闭一律
不拦（fail-soft）。波段值与本场约束界随 regime_calls 存档（h4_swing_high/low、h4_short/long_bound）。
开关 manager.h4_entry_structure.enabled（默认开）；整层仍受 trend_rule_enabled 总开关约束。
2026-11-21 门收敛（用户批准）：① 突破保底（海龟式 failsafe，无状态）——现价已越过 20 日极值且与
下单方向一致（做空：现价 ≤ 20 日最低 lo_20；做多：现价 ≥ 20 日最高 hi_20，均为 mk 的日线口径，
与特征表同源）时，§3.8
不拦截并留痕 [H4_BREAKOUT_PASS]（在 validate §3.8 裁定，h4_block_reason 本身不变）；② 入场门
优先级——§3.5 近价带与本门波段位冲突时 H4 优先，近价上限放宽到波段位（validate §3.5 裁定）。
"""
from __future__ import annotations

import logging
import math
from typing import Literal

from pydantic import BaseModel, Field

from desk import okx_data, settings
from desk import llm as dllm

log = logging.getLogger("desk.regime")

REGIME_CALLS = settings.STATE / "ledger" / "regime_calls.jsonl"
FEATURE_BARS = 260          # MA200 与其 5 日斜率需要 ≥205 根已收盘日线，取 260 留余量
JUDGE_STANDARD = 3          # 判定标准版本（v1=2026-11-11；v2=2026-11-14 衰竭证据门；v3=2026-11-14 特征扩建+资金面衰竭）

# 正面衰竭证据闭集（v2 立集，v3 增 forced_flow_exhaustion）：stage=late_exhaustion 时必须至少给出一项
# （代码侧校验，闭集外值无效；forced_flow_exhaustion 另受资金面数据缺失门约束，见 apply_stage_gate）。
EXHAUSTION_SIGNALS = ("slope_flattening", "deepening_pullbacks", "structure_break",
                      "momentum_divergence", "volume_stall", "forced_flow_exhaustion")
_EXHAUSTION_CN = {"slope_flattening": "均线斜率收敛/走平", "deepening_pullbacks": "反弹/回撤深度递增",
                  "structure_break": "高低点结构破坏", "momentum_divergence": "动量背离",
                  "volume_stall": "放量滞跌/滞涨", "forced_flow_exhaustion": "资金面衰竭"}


def trend_cfg(cfg) -> dict:
    """趋势市规则配置（manager 段）。判定改 LLM 后只剩总开关、近价距离与降级风险上限；
    A+ 的代码判定阈值（trend_consensus/trend_min_votes/trend_ret20d_pct/超卖两条）已删除。"""
    M = (cfg or {}).get("manager") or {}
    return {"enabled": bool(M.get("trend_rule_enabled", True)),
            "near_atr": float(M.get("trend_near_atr", 0.3)),
            "oversold_risk_scale": float(M.get("trend_oversold_risk_scale", 0.5))}


def weekly_cfg(cfg) -> dict:
    """周线方向过滤配置（manager.weekly_filter 段）：enabled=false 时周线方向只进特征表展示、
    不拦截任何下单。默认 True 与 trend_rule_enabled 同口径（显式配置见 config.yaml）。
    mode（2026-11-21 门收敛，用户批准）：advisory（默认）= 只计算、披露、留痕，不硬拦；
    block = 恢复 2026-11-15 原硬拦行为。非法值按 advisory 处理（收敛方向 fail-soft）。"""
    W = ((cfg or {}).get("manager") or {}).get("weekly_filter") or {}
    mode = str(W.get("mode", "advisory")).strip().lower()
    return {"enabled": bool(W.get("enabled", True)),
            "mode": mode if mode in ("advisory", "block") else "advisory"}


def h4_cfg(cfg) -> dict:
    """4H 入场结构配置（manager.h4_entry_structure 段）：enabled=false 时 4H 波段只进特征表展示、
    不拦截任何下单。默认 True 与 weekly_filter 同口径（显式配置见 config.yaml）。"""
    H = ((cfg or {}).get("manager") or {}).get("h4_entry_structure") or {}
    return {"enabled": bool(H.get("enabled", True))}


# ------------------------------------------------------------------ 趋势特征（只算数据，不下结论）
def _ma_stats(close, window: int):
    """(均线现值, 均线 5 日斜率%, 现价相对均线偏离%, 5 日前同口径 5 日斜率%)；样本不足对应项为 None。
    「5 日前斜率」= 以 5 日前那根已收盘日线为终点、再往前 5 日的均线斜率（MA[-6] 相对 MA[-11]），
    与当前斜率同口径并列，供 slope_flattening 做时序实测对比（v3）。"""
    if len(close) < window:
        return None, None, None, None
    ma = close.rolling(window).mean()
    cur = float(ma.iloc[-1])
    slope = slope_prev5 = None
    if len(close) >= window + 5:
        prev = float(ma.iloc[-6])
        if prev:
            slope = round((cur / prev - 1) * 100, 3)
    if len(close) >= window + 10:
        end_prev, base_prev = float(ma.iloc[-6]), float(ma.iloc[-11])
        if base_prev:
            slope_prev5 = round((end_prev / base_prev - 1) * 100, 3)
    dev = round((float(close.iloc[-1]) / cur - 1) * 100, 3) if cur else None
    return round(cur, 4), slope, dev, slope_prev5


def _vol_z(vol) -> tuple:
    """(最新日量能 z 值, 近 5 日最大 z 值)：z 以该日之前 20 日量能的均值/标准差（ddof=1）为基准。
    样本不足或基准窗口无波动（std=0）对应项为 None。量能取合约张数（Volume 列）。"""
    v = [float(x) for x in vol]

    def z_at(i):
        base = v[i - 20:i]
        if len(base) < 20:
            return None
        m = sum(base) / 20
        sd = (sum((x - m) ** 2 for x in base) / 19) ** 0.5
        if sd <= 0:
            return None
        return (v[i] - m) / sd

    last = z_at(len(v) - 1) if len(v) >= 21 else None
    zs = [z for z in (z_at(i) for i in range(max(0, len(v) - 5), len(v))) if z is not None]
    return (round(last, 2) if last is not None else None,
            round(max(zs), 2) if zs else None)


def _last_chg_atr(d) -> float | None:
    """最新已收盘日线的收盘涨跌 ÷ ATR14（与经理 market_ctx 同口径：TR 14 日简单均值），带符号。
    供 volume_stall 的「价格推进」判据用 ATR 倍数表达；样本不足返回 None。"""
    if len(d) < 15:
        return None
    h, l, c = [float(x) for x in d.High], [float(x) for x in d.Low], [float(x) for x in d.Close]
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(d))]
    atr = sum(trs[-14:]) / 14
    if not atr:
        return None
    return round((c[-1] - c[-2]) / atr, 3)


def _rsi14(close) -> float | None:
    """Wilder RSI14（ewm alpha=1/14）；样本不足返回 None，全程无涨跌记 50。"""
    if len(close) < 15:
        return None
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    u, v = float(up.iloc[-1]), float(dn.iloc[-1])
    if v == 0:
        return 100.0 if u > 0 else 50.0
    return round(100 - 100 / (1 + u / v), 2)


# ------------------------------------------------------------------ 周线方向（决策 2，只算数据，fail-soft）
WEEKLY_MIN_WEEKS = 25       # 周 MA20 + 其 5 周斜率需要 ≥25 个完整周（260 根日线 ≈ 36 个完整周，够用）


def _weekly_closes(d) -> list:
    """已收盘日线 → 完整周（ISO 周，周一起始，与 OKX 1Wutc 同界）的周收盘价序列（旧→新）。
    只收满 7 根日线的周：数据起点的残周与当前未走完的周一律剔除——与日线「只用已收盘 K 线」同一
    口径，未收盘的周不下方向结论；某周不足 7 根（数据缺口）同样剔除（fail-soft）。d 须按时间升序
    （okx_data.candles 的返回即升序）。"""
    if d is None or len(d) == 0:
        return []
    iso = d["Date"].dt.isocalendar()
    keys = (iso["year"].astype("int64") * 100 + iso["week"].astype("int64")).tolist()
    closes, cur_key, cur = [], None, []
    for k, c in zip(keys, d["Close"]):
        if k != cur_key:
            if len(cur) == 7:
                closes.append(float(cur[-1]))
            cur_key, cur = k, []
        cur.append(c)
    if len(cur) == 7:
        closes.append(float(cur[-1]))
    return closes


def weekly_features(d) -> dict:
    """周线方向（代码结论，供执行层过滤，不进 LLM 判定口径）：完整周收盘与周 MA20 的位置、
    周 MA20 的 5 周斜率，两者同向才算趋势——收盘在 MA20 上方且 MA20 上行 → long；收盘在下方
    且 MA20 下行 → short；其余（贴均线、位置与斜率打架）→ none（中性，不拦截任何方向）。
    完整周 <25（MA20/斜率算不出）→ weekly_direction=None、weekly_missing=True（缺失不拦截）。
    本函数绝不抛异常。"""
    out = {"weekly_direction": None, "weekly_missing": True, "weekly_weeks": 0,
           "weekly_close": None, "weekly_ma10": None, "weekly_ma20": None,
           "weekly_ma20_slope_pct": None}
    try:
        closes = _weekly_closes(d)
        out["weekly_weeks"] = len(closes)
        if not closes:
            return out
        out["weekly_close"] = round(closes[-1], 4)
        if len(closes) >= 10:
            out["weekly_ma10"] = round(sum(closes[-10:]) / 10, 4)
        if len(closes) >= 20:
            out["weekly_ma20"] = round(sum(closes[-20:]) / 20, 4)
        if len(closes) >= WEEKLY_MIN_WEEKS and out["weekly_ma20"]:
            prev20 = sum(closes[-25:-5]) / 20
            if prev20:
                slope = round((out["weekly_ma20"] / prev20 - 1) * 100, 3)
                out["weekly_ma20_slope_pct"] = slope
                last = closes[-1]
                if last > out["weekly_ma20"] and slope > 0:
                    out["weekly_direction"] = "long"
                elif last < out["weekly_ma20"] and slope < 0:
                    out["weekly_direction"] = "short"
                else:
                    out["weekly_direction"] = "none"
                out["weekly_missing"] = False
        return out
    except Exception:
        return out


# ------------------------------------------------------------------ 4H 波段结构（只算数据，fail-soft）
H4_BARS = 100               # 4H 取数与 intraday_text 的 max(42,100) 同窗口同缓存键：同场基本零新增请求
H4_PIVOT_SIDE = 2           # 枢轴左右各看 2 根：波段高 = 高点严格大于左右各 2 根的高点（低点镜像）


def _h4_time(v) -> str | None:
    """4H K 线开盘时刻（naive UTC）→ 'YYYY-MM-DD HH:MM' 字符串；不可解析返回 None。"""
    try:
        import pandas as _pd
        return _pd.Timestamp(v).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def swing_points(d) -> dict:
    """已收盘 4H K 线 → 最近一个「已确认」波段高点与波段低点（值 + 该根 K 线开盘时刻 UTC）。
    枢轴规则（确定性）：第 i 根为波段高 ⟺ High[i] 严格大于左右各 H4_PIVOT_SIDE 根的 High（波段低
    镜像，严格小于）；右侧 H4_PIVOT_SIDE 根必须已存在才算确认——序列末尾未走完右侧的枢轴不算数，
    与日线/周线「只用已收盘数据」同一口径。并列高/低（不严格）不构成枢轴。样本 <2×SIDE+1 或
    无枢轴 → 对应值为 None。d 须按时间升序（okx_data.candles 的返回即升序，且只含已收盘 K 线）。
    本函数绝不抛异常。"""
    out = {"h4_swing_high": None, "h4_swing_high_time": None,
           "h4_swing_low": None, "h4_swing_low_time": None, "h4_bars": 0}
    try:
        if d is None or len(d) == 0:
            return out
        out["h4_bars"] = int(len(d))
        H = [float(x) for x in d.High]
        L = [float(x) for x in d.Low]
        times = d["Date"].tolist() if "Date" in d else [None] * len(d)
        s = H4_PIVOT_SIDE
        hi_i = lo_i = None
        for i in range(s, len(H) - s):
            if all(H[i] > H[j] for j in range(i - s, i + s + 1) if j != i):
                hi_i = i
            if all(L[i] < L[j] for j in range(i - s, i + s + 1) if j != i):
                lo_i = i
        if hi_i is not None:
            out["h4_swing_high"] = round(H[hi_i], 4)
            out["h4_swing_high_time"] = _h4_time(times[hi_i])
        if lo_i is not None:
            out["h4_swing_low"] = round(L[lo_i], 4)
            out["h4_swing_low_time"] = _h4_time(times[lo_i])
        return out
    except Exception:
        return {"h4_swing_high": None, "h4_swing_high_time": None,
                "h4_swing_low": None, "h4_swing_low_time": None, "h4_bars": 0}


def h4_swing_features(inst: str) -> dict:
    """4H 波段特征（fail-soft，绝不抛异常）：经 okx_data.candles 现有缓存/节流路径取已收盘 4H K 线
    （confirm=1 过滤在数据层已做），与分析师 4H 参考文本同一 (inst, '4H') 缓存键、同 100 根窗口，
    同场内基本零新增请求。抓取/计算失败 → 波段值全 None、h4_missing=True（结构门本场不生效）。"""
    out = {"h4_swing_high": None, "h4_swing_high_time": None, "h4_swing_low": None,
           "h4_swing_low_time": None, "h4_bars": 0, "h4_missing": True}
    try:
        out.update(swing_points(okx_data.candles(inst, "4H", H4_BARS)))
    except Exception as e:
        log.warning("4H K 线抓取失败 %s：%s（本场 4H 波段标记缺失）", inst, type(e).__name__)
        return out
    out["h4_missing"] = out["h4_swing_high"] is None and out["h4_swing_low"] is None
    return out


# ------------------------------------------------------------------ 资金面特征（v3，只算数据，fail-soft）
_FUNDING_HIST_PAGES = 3     # funding-rate-history 每页 100 期（8h 一期），3 页 ≈ 297 期 ≈ 99 天

def _fetch_funding(inst: str):
    """(当前资金费率, 近约 90 天已结算费率列表)。经 desk.flows 的 URL 缓存抓取（与资金流分析师
    共用缓存与限频口径）；任一请求失败抛异常，由 flow_features 兜底标 missing。"""
    from desk import flows
    now = float(flows._get(f"{flows.OKX}public/funding-rate?instId={inst}")["data"][0]["fundingRate"])
    if not math.isfinite(now):   # "NaN"/"inf" 字面量能被 float() 静默解析——坏值必须响亮地失败，不许冒充成功
        raise ValueError(f"资金费率非有限值：{now}")
    rates, after = [], None
    for _ in range(_FUNDING_HIST_PAGES):
        url = f"{flows.OKX}public/funding-rate-history?instId={inst}&limit=100"
        if after:
            url += f"&after={after}"
        page = flows._get(url)["data"]
        if not page:
            break
        rates += [float(x.get("realizedRate") or x["fundingRate"]) for x in page]
        after = int(page[-1]["fundingTime"])
    if not all(math.isfinite(r) for r in rates):
        raise ValueError("资金费率历史含非有限值")
    return now, rates


def _fetch_oi(inst: str):
    """(当前持仓量名义额 USD, rubik 日持仓量序列)。OI 5 日变化在同一 rubik 序列内自比（口径自洽，
    不与单品 open-interest 快照混算）。任一请求失败抛异常。"""
    from desk import flows
    ccy = inst.split("-")[0]
    now = float(flows._get(f"{flows.OKX}public/open-interest?instType=SWAP&instId={inst}")["data"][0]["oiUsd"])
    rows = flows._get(f"{flows.OKX}rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1D")["data"]
    series = [float(r[1]) for r in rows][::-1]   # 接口最新在前 → 转升序（旧→新）
    if not math.isfinite(now) or not all(math.isfinite(v) for v in series):
        raise ValueError("持仓量含非有限值")
    return now, series


def _fetch_liq(inst: str) -> dict | None:
    """最近强平样本（public/liquidation-orders 单页 100 笔，uly 口径）：按被强平方向汇总名义额。
    该端点是滚动近窗、100 笔在活跃市况只覆盖约 0.25 小时，凑满 24h 需约百页请求，不成比例——
    只作「当下强平压力」的附加参考（渲染时带覆盖窗口），不参与任何信号门。失败返回 None。"""
    try:
        from desk import flows
        uly = inst.rsplit("-SWAP", 1)[0]
        ct = float(flows._get(f"{flows.OKX}public/instruments?instType=SWAP&instId={inst}",
                              ttl=86400)["data"][0]["ctVal"])
        page = flows._get(f"{flows.OKX}public/liquidation-orders?instType=SWAP&uly={uly}"
                          f"&state=filled&limit=100")["data"]
        det = [x for g in page for x in (g.get("details") or [])]
        if not det:
            return None
        ts = [int(x["ts"]) for x in det]
        agg = {"long": 0.0, "short": 0.0}
        for x in det:
            if x.get("posSide") in agg:
                agg[x["posSide"]] += float(x["sz"]) * ct * float(x["bkPx"])
        return {"liq_long_usd": round(agg["long"], 2), "liq_short_usd": round(agg["short"], 2),
                "liq_n": len(det), "liq_span_h": round((max(ts) - min(ts)) / 3600000, 2)}
    except Exception:
        return None


def flow_features(inst: str) -> dict:
    """资金面特征（fail-soft，绝不抛异常）：资金费率（当前值 + 近约 90 日分位）与持仓量（当前 USD +
    5 日变化%）。flow_missing 从终态推导（见函数末尾）：费率/OI 任一分支抓取失败，或核心字段
    （费率现值/费率分位/OI 5 日变化）最终为 None/非有限值 → flow_missing=True；flow_missing 时
    第六项证据 forced_flow_exhaustion 会被代码门剔除（见 apply_stage_gate）。
    清算量是附加项，失败只省略字段、不影响 flow_missing。"""
    out: dict = {"flow_missing": True, "funding_now": None, "funding_pctile_90d": None,
                 "funding_hist_n": 0, "oi_usd": None, "oi_chg_5d_pct": None}
    funding_failed = oi_failed = False
    try:
        now_rate, hist = _fetch_funding(inst)
        out["funding_now"] = now_rate
        out["funding_hist_n"] = len(hist)
        if hist:
            out["funding_pctile_90d"] = round(100.0 * sum(1 for r in hist if r <= now_rate) / len(hist), 1)
    except Exception as e:
        funding_failed = True
        log.warning("资金费率抓取失败 %s：%s（本场资金面标记缺失）", inst, type(e).__name__)
    try:
        oi_now, series = _fetch_oi(inst)
        out["oi_usd"] = oi_now
        if len(series) >= 6 and series[-6]:
            out["oi_chg_5d_pct"] = round((series[-1] / series[-6] - 1) * 100, 2)
    except Exception as e:
        oi_failed = True
        log.warning("持仓量抓取失败 %s：%s（本场资金面标记缺失）", inst, type(e).__name__)
    # flow_missing 单一事实来源 = 分支失败标记 + 核心字段终态，不靠各分支自觉维护平行标志：
    # 任何让核心字段静默为 None/NaN 的返回形态（不抛异常的坏返回、解析坏值）都会被这里兜住，
    # 不可能再出现「字段缺失但 flow_missing=False」——代码门正是靠这个标记作废第六项证据。
    def _bad(v) -> bool:
        return v is None or (isinstance(v, float) and not math.isfinite(v))
    out["flow_missing"] = bool(funding_failed or oi_failed
                               or _bad(out["funding_now"]) or _bad(out["funding_pctile_90d"])
                               or _bad(out["oi_chg_5d_pct"]))
    liq = _fetch_liq(inst)
    if liq:
        out.update(liq)
    return out


def features(inst: str) -> dict:
    """趋势特征表：从已收盘日线（okx_data.candles 现有缓存/节流路径，一次取回 ≥300 根）计算。
    与 manager.market_ctx 的 5/20 日涨跌幅、ATR、20 日高低同源同口径，渲染时从 mk 取。
    v3 起并入资金面特征（flow_features，fail-soft：失败只标 flow_missing，不拖垮特征表）。"""
    d = okx_data.candles(inst, "1Dutc", FEATURE_BARS)
    c, h, l = d.Close, d.High, d.Low
    out: dict = {"bars": int(len(d))}
    for w in (20, 50, 200):
        v, slope, dev, slope_prev5 = _ma_stats(c, w)
        out[f"ma{w}"], out[f"ma{w}_slope_pct"], out[f"dev_ma{w}_pct"] = v, slope, dev
        out[f"ma{w}_slope_pct_prev5"] = slope_prev5
    out["rsi14"] = _rsi14(c)
    hi20, lo20, last = float(h.tail(20).max()), float(l.tail(20).min()), float(c.iloc[-1])
    out["pos_in_range"] = round((last - lo20) / (hi20 - lo20), 4) if hi20 > lo20 else None
    out["vol_z_last"], out["vol_z_max5"] = _vol_z(d.Volume)
    out["last_chg_atr"] = _last_chg_atr(d)
    try:
        out.update(weekly_features(d))
    except Exception:   # weekly_features 本应绝不抛异常，此处再兜一层：周线绝不拖垮特征表
        out.update({"weekly_direction": None, "weekly_missing": True, "weekly_weeks": 0,
                    "weekly_close": None, "weekly_ma10": None, "weekly_ma20": None,
                    "weekly_ma20_slope_pct": None})
    try:
        out.update(h4_swing_features(inst))
    except Exception:   # h4_swing_features 本应绝不抛异常，此处再兜一层：4H 绝不拖垮特征表
        out.update({"h4_swing_high": None, "h4_swing_high_time": None, "h4_swing_low": None,
                    "h4_swing_low_time": None, "h4_bars": 0, "h4_missing": True})
    try:
        out.update(flow_features(inst))
    except Exception:   # flow_features 本应绝不抛异常，此处再兜一层：资金面绝不拖垮特征表
        out["flow_missing"] = True
    return out


def _num(v) -> str:
    return "n/a" if v is None else f"{v:.6g}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _flow_seg(f) -> str:
    """资金面渲染（v3）：核心字段全缺失 → 明示缺失且第六项不可引用；部分缺失 → 逐项 n/a
    并在 flow_missing 时附注第六项不可引用。P4：渲染改紧凑英文（判定与 TM 共用）。"""
    if f.get("funding_now") is None and f.get("oi_usd") is None and f.get("oi_chg_5d_pct") is None:
        return ("flows (OKX public): MISSING this run (fetch failed); "
                "forced_flow_exhaustion NOT citable this run")
    fr = f.get("funding_now")
    fr_s = ("n/a" if fr is None else
            f"{fr * 100:+.4f}% (~90d pctile {_num(f.get('funding_pctile_90d'))}, "
            f"n={f.get('funding_hist_n', 0)})")
    oi = f.get("oi_usd")
    oi_s = "n/a" if oi is None else f"${oi / 1e9:.2f}B"
    s = f"flows (OKX public): funding 8h {fr_s}; OI {oi_s}, 5d chg {_pct(f.get('oi_chg_5d_pct'))}"
    if f.get("liq_n"):
        s += (f"; liquidations last ~{f['liq_span_h']:g}h, {f['liq_n']} prints: longs "
              f"≈${f['liq_long_usd'] / 1e6:.1f}M, shorts ≈${f['liq_short_usd'] / 1e6:.1f}M "
              "(short window, liquidation-pressure context only)")
    if f.get("flow_missing"):
        s += " (note: flows data incomplete; forced_flow_exhaustion NOT citable this run)"
    return s


_WEEKLY_CN = {"long": "long", "short": "short", "none": "neutral"}


def _weekly_seg(f) -> str:
    """周线方向渲染：缺失明示缺失（过滤本场不生效）；中性明示不拦截。P4：紧凑英文。"""
    d = f.get("weekly_direction")
    if d not in _WEEKLY_CN:
        return "weekly direction (code, from completed weekly closes): n/a (weekly filter inactive this run)"
    return (f"weekly direction (code, {f.get('weekly_weeks', '?')} completed weeks): {d}; "
            f"weekly close {_num(f.get('weekly_close'))}; weekly MA10 {_num(f.get('weekly_ma10'))}; "
            f"weekly MA20 {_num(f.get('weekly_ma20'))} (5w slope {_pct(f.get('weekly_ma20_slope_pct'))})")


def _h4_seg(f) -> str:
    """4H 波段结构渲染：两侧全缺失明示缺失（结构门本场不生效）；单侧缺失逐项 n/a。P4：紧凑英文。"""
    hi, lo = f.get("h4_swing_high"), f.get("h4_swing_low")
    if hi is None and lo is None:
        return "4H swing structure (code, closed 4H bars): n/a (4H entry-structure gate inactive this run)"
    hi_s = "n/a" if hi is None else f"{_num(hi)} ({f.get('h4_swing_high_time') or 'time n/a'} UTC)"
    lo_s = "n/a" if lo is None else f"{_num(lo)} ({f.get('h4_swing_low_time') or 'time n/a'} UTC)"
    return f"4H swing structure (code, closed 4H bars): latest confirmed swing high {hi_s}; swing low {lo_s}"


def feature_block(feat, mk) -> str:
    """趋势特征渲染：判定提示词与交易经理提示词共用同一块，保证两边看到的数据完全一致。
    P4（2026-11-15）：标签改紧凑英文（值与口径不变）。"""
    f = feat or {}

    def ma_seg(label: str, key: str) -> str:
        s = (f"{label} {_num(f.get(key))} (5d slope {_pct(f.get(key + '_slope_pct'))}")
        pv = f.get(key + "_slope_pct_prev5")
        if pv is not None:
            s += f", slope 5d ago {_pct(pv)}"
        return s + f", dev {_pct(f.get('dev_' + key + '_pct'))})"
    pos = f.get("pos_in_range")
    pos_s = "n/a" if pos is None else f"{pos * 100:.0f}%"

    def mkn(key, pct=False):   # mk 字段缺失（旧记录/测试桩）时渲染为 n/a，不抛错
        v = (mk or {}).get(key)
        return "n/a" if v is None else (f"{float(v):+.2f}%" if pct else f"{float(v):.6g}")
    return ("## Trend features (code-computed, data only)\n"
            f"last {mkn('last')}; {ma_seg('MA20', 'ma20')}; {ma_seg('MA50', 'ma50')}; {ma_seg('MA200', 'ma200')}; "
            f"RSI14 {_num(f.get('rsi14'))}; 20d range pos {pos_s} (high {mkn('hi_20')} / low {mkn('lo_20')}); "
            f"5d {mkn('ret_5d', pct=True)}, 20d {mkn('ret_20d', pct=True)}; "
            f"ATR14 {mkn('atr14')} ({f.get('bars', '?')} closed daily bars); "
            f"volume: latest-day z {_num(f.get('vol_z_last'))}, 5d max z {_num(f.get('vol_z_max5'))} "
            f"(vs prior-20d mean/std), last close move {_num(f.get('last_chg_atr'))}xATR14; "
            + _weekly_seg(f) + "; " + _h4_seg(f) + "; " + _flow_seg(f))


# ------------------------------------------------------------------ LLM 行情判定
class RegimeJudgment(BaseModel):
    direction: Literal["short", "long", "none"] = Field(description="趋势方向：short=空头趋势，long=多头趋势，none=无趋势/震荡")
    strength: Literal["strong", "moderate", "none"] = Field(description="趋势强度：strong=多项特征同向且显著，moderate=同向但一般；direction=none 时填 none")
    stage: Literal["early", "mid", "late_exhaustion"] = Field(description="趋势阶段：early=刚确立，mid=进行中，late_exhaustion=末段衰竭。"
        "late_exhaustion 只许在 exhaustion_signals 至少给出一项正面衰竭证据时返回；RSI 极端、贴区间边缘、偏离均线深、"
        "短期涨跌幅大都是伸展变量，在强趋势中是常态，只支持 direction/strength，永远不构成衰竭证据")
    exhaustion_signals: list[str] = Field(default_factory=list, description="正面衰竭证据（闭集，可多选；stage 非 late_exhaustion 时留空）："
        "slope_flattening=均线斜率收敛/走平（以特征表斜率历史实测）；deepening_pullbacks=反弹/回撤深度递增；structure_break=高低点结构破坏；"
        "momentum_divergence=动量背离实际出现（evidence 中须指明哪个指标与价格背离）；volume_stall=放量滞跌/滞涨（量能 climax+价格滞行）；"
        "forced_flow_exhaustion=资金面衰竭（资金费率极端分位与持仓量 5 日明显萎缩双门槛同时满足，evidence 中须引用特征表原值；"
        "本场资金面数据缺失时不许引用，引用会被代码剔除）。"
        "stage=late_exhaustion 时必须非空且只能取上述值，否则代码校验会把 stage 强制降为 mid")
    confidence: float = Field(description="0~1，对本次行情判定的置信度")
    evidence: list[str] = Field(description="支撑判定的证据，每条引用具体特征值（如「MA50 斜率 -1.23%」「RSI14 31.2」）")
    summary: str = Field(description="一句话中文结论（≤60 字）")


_DIRS, _STRS, _STGS = ("short", "long", "none"), ("strong", "moderate", "none"), ("early", "mid", "late_exhaustion")


def normalize_judgment(obj) -> dict | None:
    """结构化输出（RegimeJudgment 实例或 dict）→ 归一化 dict；不合格返回 None（按判定失败处理）。"""
    try:
        if isinstance(obj, RegimeJudgment):
            d = obj.model_dump()
        elif isinstance(obj, dict):
            obj = dict(obj)
            if isinstance(obj.get("evidence"), str):   # 模型偶尔把 evidence 给成单个字符串，先包成列表再校验
                obj["evidence"] = [obj["evidence"]]
            if isinstance(obj.get("exhaustion_signals"), str):
                obj["exhaustion_signals"] = [obj["exhaustion_signals"]]
            d = RegimeJudgment(**obj).model_dump()
        else:
            return None
    except Exception:
        return None
    if d["direction"] not in _DIRS or d["strength"] not in _STRS or d["stage"] not in _STGS:
        return None
    conf = d.get("confidence")
    if not isinstance(conf, (int, float)) or conf != conf:   # 非数值或 NaN
        return None
    ev = d.get("evidence") or []
    if isinstance(ev, str):
        ev = [ev]
    sig_raw_in = d.get("exhaustion_signals") or []
    if isinstance(sig_raw_in, str):
        sig_raw_in = [sig_raw_in]
    elif not isinstance(sig_raw_in, (list, tuple)):
        sig_raw_in = [sig_raw_in]
    sig_raw = [str(x)[:40] for x in list(sig_raw_in)[:8]]
    sig_valid = list(dict.fromkeys(s for s in sig_raw if s in EXHAUSTION_SIGNALS))   # 闭集过滤+保序去重
    return {"direction": d["direction"], "strength": d["strength"], "stage": d["stage"],
            "confidence": max(0.0, min(1.0, float(conf))),
            "evidence": [str(x)[:120] for x in list(ev)[:8]],
            "summary": str(d.get("summary") or "")[:120],
            "exhaustion_signals": sig_valid,
            "exhaustion_signals_raw": sig_raw,
            "judge_standard": JUDGE_STANDARD}


def judge_prompt(coin_name: str, inst: str, feat, mk, S: float, votes: dict, market_text: str) -> str:
    lines = [f"你是加密货币交易台的行情判定员。你只做一件事：判断 {coin_name}（OKX 永续 {inst}）当前处于什么行情状态。",
             "你不做交易决策、不给买卖点位；只依据下面给出的数据下判定，结论必须能在 evidence 里引用到具体特征值。",
             "",
             feature_block(feat, mk),
             "",
             f"团队共识（仅供参考，不是判定依据本身）：加权共识 S={S:+.3f}；有效投票 {votes.get('valid', 0)} 个，"
             f"其中看多 {votes.get('long', 0)}、看空 {votes.get('short', 0)}、中性 {votes.get('neutral', 0)}。"]
    if market_text:
        lines += ["", "## 市场分析师结论摘录（供参考）", market_text]
    lines += ["", "判定口径：",
              "- direction：多项特征一致指向单边行情时给 short/long；方向混杂、均线纠缠、震荡特征明显时给 none。",
              "- strength：strong=多项特征同向且幅度显著；moderate=同向但幅度一般、或个别特征不一致；direction=none 时 strength=none。",
              "- stage 的两类变量必须分清（v3 标准）：",
              "  · 伸展变量：RSI14 水平、20 日区间位置、现价偏离均线的距离、5 日/20 日涨跌幅。它们只说明行情已经走了多远。"
              "强趋势中 RSI 长期钉在极值区、价格贴着区间边缘运行是常态（强下跌里 RSI 20 上下、贴区间底可以持续很多天），"
              "因此伸展变量只许用来支撑 direction 与 strength，单独或任意组合都不许推出 late_exhaustion。",
              "  · 衰竭证据：只有出现「趋势正在失败」的正面信号，才谈得上末段衰竭（见 stage 定义的闭集）。",
              "- stage（正面定义）：",
              "  · early=趋势刚确立：均线刚转为同向排列、斜率刚转向趋势方向、现价与均线距离尚近、顺趋势的新极值刚开始出现。",
              "  · mid=趋势进行中且结构完整：均线斜率稳定或还在加深、回调浅且很快被趋势方向重新吞没、高低点结构沿趋势方向推进"
              "（下跌看更低的低点/高点，上涨镜像）。只要结构未破，即使伸展变量已经很极端，也判 mid，不要因为「跌多了/涨多了」改判衰竭。",
              "  · late_exhaustion=末段衰竭：必须至少出现一项正面衰竭证据，并把选中的项填入 exhaustion_signals（只能取下列闭集值，可多选），"
              "同时在 evidence 中为每一项写明对应的具体特征数值：",
              "    slope_flattening=均线斜率收敛/走平（以特征表的斜率历史实测，不再用单时点近似：当前 MA20 5 日斜率绝对值"
              "相对 5 日前同口径斜率收窄 ≥30%（方向未反转到趋势对侧），或当前 MA20 斜率绝对值已 <0.3% 近零；MA50 斜率历史作旁证。"
              "特征表缺 5 日前斜率时不许主张此项）；",
              "    deepening_pullbacks=逆趋势反弹/回撤的深度一轮比一轮深；",
              "    structure_break=高低点结构破坏（下跌趋势出现更高的低点并站稳、或更高的高点；上涨镜像）；",
              "    momentum_divergence=动量背离实际出现（evidence 须指明哪个指标与价格如何背离，如「价格创新低而 RSI14 未创新低」；"
              "只写「无背离」或指标未提及不算）；",
              "    volume_stall=放量滞跌/滞涨（须同时满足量能 climax 与价格滞行：最新日量能 z 值 ≥2.0（特征表给出，相对此前 20 日）"
              "且最新日收盘涨跌在 ±0.3×ATR14 以内；或近 5 日最大 z 值 ≥2.5 且其后价格未再顺趋势方向创出新极值。"
              "爆量同时价格决定性推进（收盘涨跌 ≥0.8×ATR14）是趋势加速，不是滞行，不许主张此项）；",
              "    forced_flow_exhaustion=资金面衰竭（双硬门槛须同时满足，方向相对趋势取极端侧）：(a) 资金费率处于近约 90 日分布的"
              "极端分位——下跌趋势看深度负费率侧、分位 ≤10%，上涨趋势镜像、分位 ≥90%；(b) 持仓量 5 日变化 ≤−10%（杠杆仓位明显萎缩）。"
              "evidence 须引用特征表中的费率数值、分位与 OI 5 日变化原值。本场资金面数据缺失（特征表标注缺失/不完整）时不许引用此项，"
              "引用了也会被代码剔除。",
              "  · 若拿不到任何一项衰竭证据，stage 不许给 late_exhaustion；系统有代码校验：返回 late_exhaustion 但 exhaustion_signals "
              "为空或含闭集外的值，生效 stage 会被强制降为 mid 并记入存档；引用 forced_flow_exhaustion 但本场资金面数据缺失的，"
              "该项会被剔除、按剩余信号裁定（无剩余信号同样降为 mid）。",
              "- 特征表中的周线方向是代码计算的执行层过滤数据（逆周线方向开仓会被代码拦截），不是你的判定对象；"
              "你的 direction/strength/stage 仍只按日线特征判定，不要因周线方向改判。",
              "- 特征表中的 4H 波段高低点同样是代码计算的执行层入场结构数据（触发价落进 4H 波段内部会被代码拦截），"
              "不是你的判定对象；判定仍只按日线特征，不要因 4H 波段改判。",
              "只输出结构化结果，不要输出其他文字。"]
    return "\n".join(lines)


def _judge_model(cfg, tracker):
    """与交易经理同一模型席位（role_models.trade_manager）；不套交易经理手册——判定是独立职责，
    与立场抽取（stances.extract）一样直接用结构化输出。"""
    return dllm.make_llm(cfg, "deep", tracker, role="trade_manager").with_structured_output(RegimeJudgment)


def call_judge(cfg, tracker, prompt: str) -> dict | None:
    """调用判定：底层 BaseChatModel.invoke 已有 llm_retry 退避；此处对解析/校验失败再重试一次，
    仍失败返回 None（上层按判定失败处理）。模型构建失败同样返回 None。"""
    try:
        model = _judge_model(cfg, tracker)
    except Exception:
        return None
    for _ in range(2):
        try:
            out = model.invoke(prompt)
        except Exception:
            continue
        j = normalize_judgment(out)
        if j is not None:
            return j
    return None


# ------------------------------------------------------------------ 判定结果 → 规则/提示词/日志
def apply_stage_gate(judge, feat=None) -> dict | None:
    """代码门（v2 立门，v3 扩展）：stage=late_exhaustion 但 exhaustion_signals 无任何闭集内有效值
    （normalize 已做闭集过滤，此处只看过滤后的列表）→ 生效 stage 强制降为 mid。v3 扩展：feat 的
    flow_missing 为真（本场资金面特征缺失）时，forced_flow_exhaustion 不是有效证据——从生效 signals
    中剔除并记入 exhaustion_signals_dropped（模型原始引用仍保留在 exhaustion_signals_raw 里可审计）；
    剔除后若无剩余信号，同样降为 mid（原因为 late_exhaustion_only_forced_flow_but_flow_missing）。
    feat 缺失/无 flow_missing 标记（旧记录、测试桩）按资金面可用处理，不改变 v2 行为。
    返回标注后的判定副本：stage=生效值、stage_raw=模型原始值、stage_overridden 标记、
    exhaustion_signals 一律为生效有效值（防御性再过滤一次，未经 normalize 的直构 dict 也受同一门约束）。
    原始判定不丢：raw 与覆盖/剔除动作都随判定进存档。"""
    if not judge:
        return None
    j = dict(judge)
    sig = [s for s in (j.get("exhaustion_signals") or []) if s in EXHAUSTION_SIGNALS]
    sig = list(dict.fromkeys(sig))
    dropped = []
    if isinstance(feat, dict) and feat.get("flow_missing") and "forced_flow_exhaustion" in sig:
        sig = [s for s in sig if s != "forced_flow_exhaustion"]
        dropped = ["forced_flow_exhaustion"]
        j["exhaustion_signals_dropped"] = dropped
        j["exhaustion_signals_drop_reason"] = "flow_data_missing"
    j["exhaustion_signals"] = sig
    raw = j.get("stage")
    j["stage_raw"] = raw
    if raw == "late_exhaustion" and not sig:
        j["stage"] = "mid"
        j["stage_overridden"] = True
        j["stage_override_reason"] = ("late_exhaustion_only_forced_flow_but_flow_missing" if dropped
                                      else "late_exhaustion_without_exhaustion_signals")
    else:
        j["stage_overridden"] = False
        j.pop("stage_override_reason", None)
    j.setdefault("judge_standard", JUDGE_STANDARD)
    return j


def build_regime(cfg, judge, feat) -> dict:
    """把判定结果整理成 validate/提示词/决策记录共用的 regime dict。judge 先过代码门
    （apply_stage_gate，含 v3 资金面缺失剔除），此后所有消费者（降级、移动止损资格、提示词、存档）
    只见生效 stage。规则生效（active）= 方向为 long/short 且强度 strong/moderate；
    late_exhaustion 在生效时即降级。"""
    tc = trend_cfg(cfg)
    judge = apply_stage_gate(judge, feat)
    active = bool(judge and judge["direction"] in ("long", "short") and judge["strength"] in ("strong", "moderate"))
    # 周线方向过滤状态（决策 2）：方向只认特征表里的代码计算值，非法/缺失一律按缺失处理（不拦）。
    # blocked_side = 本场会被过滤拦下的开仓方向（周线方向的反面），随判定一并存档备查。
    wd = (feat or {}).get("weekly_direction") if isinstance(feat, dict) else None
    if wd not in ("long", "short", "none"):
        wd = None
    wc = weekly_cfg(cfg)
    weekly = {"enabled": wc["enabled"], "mode": wc["mode"], "direction": wd, "missing": wd is None,
              "blocked_side": ({"long": "short", "short": "long"}.get(wd)
                               if wc["enabled"] and wd in ("long", "short") else None)}
    # 4H 入场结构状态（2026-11-15）：波段值只认特征表里的代码计算值，非法/非有限/非正一律按缺失
    # 处理（不拦）。short_bound/long_bound = 本场该方向入场的约束界（做空触发价下限、做多触发价
    # 上限），开关关闭时置 None，随判定一并存档备查（stage_overridden 式记账）。
    h4c = h4_cfg(cfg)
    fd = feat if isinstance(feat, dict) else {}

    def _px(v):
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v > 0 else None
    sh, sl_ = _px(fd.get("h4_swing_high")), _px(fd.get("h4_swing_low"))
    h4 = {"enabled": h4c["enabled"], "swing_high": sh, "swing_high_time": fd.get("h4_swing_high_time"),
          "swing_low": sl_, "swing_low_time": fd.get("h4_swing_low_time"),
          "missing": sh is None and sl_ is None,
          "short_bound": sh if h4c["enabled"] else None,
          "long_bound": sl_ if h4c["enabled"] else None}
    return {"enabled": tc["enabled"], "source": "llm", "failed": judge is None,
            "judge": judge, "features": feat, "cfg": tc, "active": active,
            "judge_standard": JUDGE_STANDARD,
            "side": judge["direction"] if active else None,
            "downgrade": bool(active and judge["stage"] == "late_exhaustion"),
            "weekly": weekly, "weekly_direction": wd, "h4": h4}


def weekly_block_reason(regime, side) -> str | None:
    """周线方向过滤的代码门（validate §3.7 调用）：过滤启用、周线方向明确（long/short）且下单方向
    与周线方向相反 → 返回 [WEEKLY_DIR] 拦截说明；其余情况（同向/中性/缺失/关闭/regime 缺失）一律
    返回 None 不拦截（fail-soft，与资金面缺失同口径）。"""
    wk = (regime or {}).get("weekly") or {}
    if not wk.get("enabled"):
        return None
    wd = wk.get("direction")
    if wd not in ("long", "short") or side not in ("long", "short") or side == wd:
        return None
    return (f"[WEEKLY_DIR] 周线方向过滤（代码按完整周收盘计算）：本场周线方向为{_DIR_CN[wd]}（{wd}），"
            f"禁止逆周线方向开新仓，本单 side={side} 被拦。请改 hold 并在 rationale 写明周线过滤，"
            "或等周线方向转向后再议；不要改方向绕过（方向由共识决定，你无权翻转）。")


def h4_block_reason(regime, side, trigger_px) -> str | None:
    """4H 入场结构的代码门（validate §3.8 调用）：结构门启用且对应侧波段值存在时——做空入场价
    （pending/replace_pending 看触发价，open 看现价）低于最近确认 4H 波段高点 → 返回 [H4_STRUCT]
    拦截说明（含最低可接受触发价）；做多镜像（入场价高于最近确认 4H 波段低点 → 拦）。入场价恰
    等于波段点位可过。其余情况（同侧无约束/波段缺失/开关关闭/regime 缺失/价格不可解析）一律
    返回 None 不拦截（fail-soft，与周线过滤同口径）。"""
    h4 = (regime or {}).get("h4") or {}
    if not h4.get("enabled") or side not in ("long", "short"):
        return None
    try:
        px = float(trigger_px)
    except (TypeError, ValueError):
        return None
    if side == "short":
        bound = h4.get("swing_high")
        if bound is not None and px < float(bound):
            return (f"[H4_STRUCT] 4H 入场结构（代码按已收盘 4H K 线计算）：做空入场价 {px:g} 低于最近确认的 "
                    f"4H 波段高点 {float(bound):g}（{h4.get('swing_high_time') or '时刻未登记'} UTC），触发价落进了"
                    f"当前 4H 上涨波段内部；做空触发价最低须 ≥ {float(bound):g}，请改高后重试，或改 hold 并在 "
                    "rationale 写明 4H 结构约束。")
    else:
        bound = h4.get("swing_low")
        if bound is not None and px > float(bound):
            return (f"[H4_STRUCT] 4H 入场结构（代码按已收盘 4H K 线计算）：做多入场价 {px:g} 高于最近确认的 "
                    f"4H 波段低点 {float(bound):g}（{h4.get('swing_low_time') or '时刻未登记'} UTC），触发价落进了"
                    f"当前 4H 下跌波段内部；做多触发价最高须 ≤ {float(bound):g}，请改低后重试，或改 hold 并在 "
                    "rationale 写明 4H 结构约束。")
    return None


_DIR_CN = {"short": "空头", "long": "多头", "none": "无趋势"}
_STR_CN = {"strong": "强", "moderate": "中等", "none": "无"}
_STAGE_CN = {"early": "早期", "mid": "中段", "late_exhaustion": "末段衰竭"}


def _weekly_line(regime) -> str:
    """TM 提示词里的周线过滤说明行（决策 2）：过滤关闭时不出现；启用时按方向/中性/缺失三态
    说明口径。周线过滤独立于 LLM 判定——判定失败时本行照常出现（只要 regime 已构建）。
    2026-11-21 门收敛后默认 advisory：只披露不拦截，措辞按 mode 切换（block 才写 will be rejected）。
    P4：紧凑英文渲染。"""
    wk = (regime or {}).get("weekly") or {}
    if not wk.get("enabled"):
        return ""
    wd = wk.get("direction")
    if wd in ("long", "short"):
        opp = "short" if wd == "long" else "long"
        if wk.get("mode") == "block":
            return (f"\nWeekly direction filter (code-computed): weekly direction this run is {wd}; "
                    f"no new opens against it — {opp} open/pending/replace_pending will be rejected ([WEEKLY_DIR]); "
                    "if consensus is opposite to the weekly direction, hold and say so in the rationale; do not try to order.")
        return (f"\nWeekly direction filter (code-computed, [WEEKLY_DIR] now ADVISORY only): weekly direction this run is {wd}; "
                f"a {opp} open/pending/replace_pending runs against it but will NOT be rejected for that reason; "
                "treat the weekly direction as one input, and if you order against it, note the weekly direction in the rationale.")
    if wk.get("missing"):
        return "\nWeekly direction filter (code-computed): weekly data missing this run; filter inactive; use the regular rules."
    return "\nWeekly direction filter (code-computed): weekly direction neutral this run; the filter blocks nothing."


def _h4_line(regime) -> str:
    """TM 提示词里的 4H 入场结构说明行（2026-11-15）：结构门关闭时不出现；启用时按波段值有无
    说明约束界。结构门独立于 LLM 判定——判定失败时本行照常出现（只要 regime 已构建）。
    P4：紧凑英文渲染（约束界不变）。"""
    h4 = (regime or {}).get("h4") or {}
    if not h4.get("enabled"):
        return ""
    parts = []
    if h4.get("swing_high") is not None:
        parts.append(f"short triggers must be >= latest confirmed 4H swing high {float(h4['swing_high']):g}")
    if h4.get("swing_low") is not None:
        parts.append(f"long triggers must be <= latest confirmed 4H swing low {float(h4['swing_low']):g}")
    if not parts:
        return "\n4H entry structure (code-computed): 4H swing data missing this run; gate inactive; use the regular rules."
    return ("\n4H entry structure (code, closed 4H bars): " + "; ".join(parts) +
            " — non-conforming open/pending/replace_pending will be rejected ([H4_STRUCT]); a trigger exactly at the swing level passes. "
            "Breakout failsafe: if spot has already crossed the 20-day extreme in your order's direction "
            "(short: spot <= 20-day low; long: spot >= 20-day high), the structure gate does not block ([H4_BREAKOUT_PASS]).")


def regime_block(regime: dict, mk) -> str:
    """交易经理提示词里的行情判定段：判定结果 + 依判定生效的执行规则。
    P4：代码侧框架文字改紧凑英文；判定员自己的 summary/evidence 原文（中文）原样嵌入。"""
    tc = regime["cfg"]
    if regime.get("failed") or not regime.get("judge"):
        return ("## Regime judgment (LLM)\n"
                "Judgment failed (regime_judge_failed): trend execution rules NOT active this session; use the regular rules."
                + _weekly_line(regime) + _h4_line(regime))
    j = regime["judge"]
    head = (f"## Regime judgment (LLM, same model seat as the trade manager, judged before this decision)\n"
            f"direction={j['direction']} ({_DIR_CN[j['direction']]} trend); strength={j['strength']}; "
            f"stage={j['stage']}; confidence {j['confidence']:.2f}.\n"
            f"Conclusion: {j['summary']}\n"
            "Evidence: " + ("; ".join(j["evidence"]) if j["evidence"] else "(none)"))
    if j.get("stage_overridden"):
        head += ("\nNote: the judge's raw stage was late_exhaustion but it cited no valid affirmative exhaustion "
                 "evidence (signals empty, or forced-flow dropped with flows data missing); per judge standard v3 "
                 "the code forced the effective stage to mid; the late-exhaustion downgrade does NOT trigger.")
    elif j["stage"] == "late_exhaustion" and j.get("exhaustion_signals"):
        head += ("\nExhaustion evidence (affirmative signals): " + "; ".join(
            f"{_EXHAUSTION_CN.get(s, s)} ({s})" for s in j["exhaustion_signals"]))
    if j.get("exhaustion_signals_dropped"):
        head += ("\nNote: the judge cited forced_flow_exhaustion, but flows data was missing this run; "
                 "that evidence was dropped by code validation (the original citation stays in the archive).")
    if not regime.get("active"):
        return head + "\nTrend execution rules NOT active this session (judgment: no trend or weak strength); use the regular rules." + _weekly_line(regime) + _h4_line(regime)
    cap = tc["near_atr"] * float((mk or {}).get("atr14") or 0)
    s = (head + f"\nTrend execution rules (active per this judgment; they change only HOW you enter, not the veto bar): "
         f"when your order direction matches the judged direction, never rest the only entry far from spot: "
         f"(a) open at market, or (b) near-price pending, trigger within <={tc['near_atr']:g}xATR14={cap:.6g} of spot "
         f"(shorts: gte bounce or lte breakdown both qualify); beyond that is rejected "
         "([TREND_DIST]). Exception (gate priority): when the near band's edge conflicts with the 4H swing bound, "
         "the band extends to the swing level (H4 takes precedence over [TREND_DIST]), so a trigger at the swing level is allowed. "
         "If a market open is blocked by price drift, switch to (b); never fall back to hold or a far order.")
    if regime.get("downgrade"):
        s += (f"\nStage is late_exhaustion this session; downgrade active: market open DISABLED, near-price pending only, "
              f"risk_scale<={tc['oversold_risk_scale']:g} ([TREND_OVERSOLD] hard check).")
    s += ("\nHolding in a trend session is allowed only if the rationale names which piece of the judgment's evidence "
          "fails, or which hard guardrail blocks the order; 'waiting for a bounce' / 'not confident enough' alone counts "
          "as unexplained and is flagged in the daily report.")
    return s + _weekly_line(regime) + _h4_line(regime)


def log_call(*, date, session, source, inst, decision_id, regime) -> dict:
    """每次判定追加一行 state/ledger/regime_calls.jsonl（判定 + 特征快照），供日后对照实际走势打分。"""
    wk = regime.get("weekly") or {}
    h4 = regime.get("h4") or {}
    rec = {"time": settings.now_str(), "date": date, "session": session, "source": source,
           "inst_id": inst, "decision_id": decision_id, "judge_standard": JUDGE_STANDARD,
           "judge": regime.get("judge"), "failed": bool(regime.get("failed")),
           "active": bool(regime.get("active")), "downgrade": bool(regime.get("downgrade")),
           "weekly_direction": wk.get("direction"), "weekly_missing": bool(wk.get("missing")),
           "weekly_filter_enabled": bool(wk.get("enabled")), "weekly_filter_mode": wk.get("mode"),
           "weekly_blocked_side": wk.get("blocked_side"),
           "h4_swing_high": h4.get("swing_high"), "h4_swing_high_time": h4.get("swing_high_time"),
           "h4_swing_low": h4.get("swing_low"), "h4_swing_low_time": h4.get("swing_low_time"),
           "h4_missing": bool(h4.get("missing")), "h4_filter_enabled": bool(h4.get("enabled")),
           "h4_short_bound": h4.get("short_bound"), "h4_long_bound": h4.get("long_bound"),
           "features": regime.get("features")}
    settings.append_jsonl(REGIME_CALLS, rec)
    return rec
