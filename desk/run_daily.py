"""每日主流程：结算/盯市 → 打分 → 分析各币 → 经理决策并纸面执行 → 复盘会 → 手册/权重 → 日报。"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

from desk import settings


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="", help="逗号分隔，如 BTC,ETH,SOL；默认配置里全部")
    ap.add_argument("--mock", action="store_true", help="使用模拟 LLM（无需密钥）")
    ap.add_argument("--date", default="", help="交易日 YYYY-MM-DD（默认北京今天）")
    ap.add_argument("--research", action="store_true", help="强制本次运行每周研究（默认只在周日）")
    ap.add_argument("--report-name", default="", help="日报文件名（不含 .md），如 2026-10-20-rerun；默认用日期")
    ap.add_argument("--session", default="morning", choices=["morning", "evening"], help="场次：morning 早场 / evening 晚场")
    a = ap.parse_args(argv)
    settings.SESSION = a.session
    if a.mock:
        os.environ["TA_DESK_MOCK_LLM"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    # 延迟导入：确保 TA_DESK_MOCK_LLM 先生效
    from desk import analysis, drift, ledger, manager, okx_data, report, review, roles, scoring, stances
    from desk import llm as dllm

    cfg = settings.load_config()
    ledger.assert_paper(cfg)
    ledger.migrate_to_swap(cfg)
    from desk import demo_exec
    if demo_exec.is_demo(cfg):
        ledger._demo("startup", cfg)   # OKX 模拟盘自检（禁止实盘；失败则本进程拒绝下单）
    date = a.date or settings.today()
    want = {c.strip().upper() for c in a.coins.split(",") if c.strip()}
    coins = [c for c in cfg["coins"] if not want or c["inst_id"].split("-")[0] in want]
    mock = os.environ.get("TA_DESK_MOCK_LLM") == "1"
    if not mock and not dllm.prepare_env(cfg):
        print("错误：未设置 LLM 密钥（TA_LLM_API_KEY 或 DEEPSEEK_API_KEY）。可加 --mock 做模拟演练。", file=sys.stderr)
        return 2
    if not mock:
        from desk import llm_retry
        llm_retry.install(cfg)   # 503/超时/连接错误指数退避重试（config llm.retry_delays）
    print(f"[ta-desk] 日期 {date} 场次 {a.session} 币种 {[c['inst_id'] for c in coins]} 模式 {cfg['mode']} 密钥来源 {settings.key_source(cfg)} "
          f"深/快模型 {cfg['llm']['deep_model']}/{cfg['llm']['quick_model']} base_url {settings.llm_base_url(cfg)}", flush=True)
    roles.ensure_playbooks()
    tracker = dllm.UsageTracker(cfg)
    errors, decisions, closed, reviews = [], [], [], []

    # R7：KILL/HALT 下跳过全部 LLM 调用（分析/决策/复盘会/手册修订/自研），只做盯市、复盘数据收集与日报
    halt, kill = settings.halt_active(), settings.kill_active()
    stop = halt or kill
    banner = None
    if stop:
        banner = ("HALT 全冻结生效" if halt else "KILL 生效") + "：本次跳过全部 LLM 调用，只做盯市、复盘数据收集与日报，不开新仓"
        print(f"[ta-desk] {banner}（{settings.HALT_PATH if halt else settings.KILL_PATH}）", flush=True)

    # 1) 盯市 & 平仓（直连模式下旧纸面仓交给旧仓盯盘 run_monitor.sh，这里不再盯市）
    try:
        if not demo_exec.is_demo(cfg):
            closed += ledger.mark_to_market(cfg)
    except Exception as e:
        errors.append(f"盯市失败：{type(e).__name__}: {e}")
    # 2) 打分、权重
    try:
        scoring.score_due(cfg)
        weight_changes = scoring.update_weights(cfg)
    except Exception as e:
        errors.append(f"打分失败：{type(e).__name__}: {e}"); weight_changes = []
    # 2b) FRED 宏观数据（按日缓存；失败降级，不影响主流程）
    macro_data = None
    try:
        from desk import macro
        macro_data = macro.safe_get(cfg, date)
    except Exception as e:
        errors.append(f"宏观数据失败：{type(e).__name__}")
    # 3) 分析 + 决策
    run_coins = [] if stop else coins
    if parallel_enabled(cfg) and len(run_coins) > 1:
        _run_coins_parallel(cfg, run_coins, date, tracker, errors, decisions)
    else:
        for coin in run_coins:   # parallel.enabled=false：原串行流程（逐币 读账户→分析→立场→决策执行），行为不变
            try:
                prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
                bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
                ap = drift.stamp(coin["inst_id"])   # 分析开始时的参考价（漂移检查用）
                res = analysis.analyze(cfg, coin, date, bk, tracker)
                fs = res["final_state"]; fs["_signal"] = res["signal"]; fs["_analysis_px"] = ap
                tok = dllm.STAGE.set(f"{coin['inst_id']}/立场抽取")
                try:
                    st = stances.extract(fs, cfg, tracker)
                finally:
                    dllm.STAGE.reset(tok)
                rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker)
                rec["report_dir"] = res["report_dir"]
                scoring.record_stances(cfg, rec)
                decisions.append(rec)
                print(f"[ta-desk] {coin['inst_id']}: TA评级={res['signal']} 共识={rec['consensus']:+.3f} → {rec['final_action']}", flush=True)
            except Exception as e:
                traceback.print_exc()
                errors.append(f"{coin['inst_id']} 分析/决策失败：{type(e).__name__}: {e}")
    # 4) 刚平仓的（含盯市与今日决策产生的）复盘
    reviewed = {p.stem for p in review.REVIEWS.glob("*.json")} if review.REVIEWS.exists() else set()
    # 本地纸面账本 closed（旧纸面仓）+ OKX 模拟盘已平仓（positions-history 只读 + 本地事件关联 decision_id）
    from desk import demo_trades
    try:
        today_closed = demo_trades.review_candidates(cfg, reviewed, demo_exec.is_demo(cfg))
    except Exception as e:
        errors.append(f"读取模拟盘平仓失败：{type(e).__name__}: {e}（本次只复盘纸面账本）")
        today_closed = [t for t in ledger.store.read_trades() if t["status"] == "closed" and t["trade_id"] not in reviewed]
    try:   # 方案一 B：平仓盈亏记到交易经理账本（幂等；只写本地文件）
        from desk import tm_ledger
        tm_ledger.record_closed(cfg, today_closed)
    except Exception as e:
        errors.append(f"写交易经理账本失败：{type(e).__name__}: {e}")
    if stop and today_closed:
        errors.append(f"{'HALT' if halt else 'KILL'} 生效：{len(today_closed)} 笔已平仓待复盘，本次只收集数据，复盘会（LLM）待解除后运行")
    for t in ([] if stop else today_closed):
        try:
            reviews.append(review.review_trade(cfg, t, tracker))
        except Exception as e:
            errors.append(f"复盘 {t['trade_id']} 失败：{type(e).__name__}: {e}")
    closed = today_closed
    # 5) 手册评估/回滚/修订
    pb_events = []
    try:
        if not stop:
            pb_events += review.evaluate_and_rollback(cfg)
            pb_events += review.maybe_edit_playbooks(cfg, tracker)
    except Exception as e:
        errors.append(f"手册修订失败：{type(e).__name__}: {e}")
    # 5b) 每周自研（默认周日；有成本上限）
    research_rep = None
    try:
        from desk import research
        research_rep = {"skipped": True} if stop else research.run(cfg, tracker, force=a.research)
        if research_rep.get("skipped"):
            research_rep = None
        else:
            pb_events += research_rep.get("edits", [])
    except Exception as e:
        errors.append(f"每周研究失败：{type(e).__name__}: {e}")
    # 5c) 对照组（B1，纸面记账，不调用模型）
    try:
        from desk import baselines
        if not mock and settings.SESSION == "morning":   # 模拟演练不写对照组账本；晚场不单独记对照组（用户决定）
            baselines.update(cfg, date, decisions)
    except Exception as e:
        errors.append(f"对照组更新失败：{type(e).__name__}: {e}")
    # 6) 日报
    usage = tracker.summary()
    settings.write_json(settings.STATE / "usage" / f"run-{dllm.RUN_ID}.json", usage)
    prices = {}
    try:
        prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
    except Exception:
        pass
    from desk import llm_retry
    errors += llm_retry.report_lines()   # 重试用尽仍失败的 LLM 调用：日报明确列为需报告用户
    path = report.write(cfg, date, decisions, closed, reviews, weight_changes, pb_events, usage, prices, errors, mock=mock,
                        research=research_rep, name=a.report_name or (None if a.session == "morning" else f"{date}-evening"), macro=macro_data, banner=banner)
    print(f"[ta-desk] 日报：{path}", flush=True)
    print(f"[ta-desk] LLM 用量：{usage['calls']} 次调用，输入 {usage['input']:,}（缓存 {usage['cached']:,}），输出 {usage['output']:,}，估算 ${usage['cost_usd']:.4f}", flush=True)
    return 1 if errors and not decisions and not stop else 0


def parallel_enabled(cfg) -> bool:
    """config parallel.enabled（默认 false）。"""
    return bool((cfg.get("parallel") or {}).get("enabled", False))


def _analyze_one(cfg, coin, date, bk, tracker):
    """阶段一（工作线程内，独立 contextvars 上下文）：分析 + 立场提取。只读，不下单、不改账户。"""
    from desk import analysis, stances
    from desk import llm as dllm
    from desk import drift
    ap = drift.stamp(coin["inst_id"])   # 分析开始时的参考价（漂移检查用；各线程各自取）
    res = analysis.analyze(cfg, coin, date, bk, tracker)
    fs = res["final_state"]; fs["_signal"] = res["signal"]; fs["_analysis_px"] = ap
    tok = dllm.STAGE.set(f"{coin['inst_id']}/立场抽取")
    try:
        st = stances.extract(fs, cfg, tracker)
    finally:
        dllm.STAGE.reset(tok)
    return res, fs, st


def _run_coins_parallel(cfg, coins, date, tracker, errors, decisions):
    """parallel.enabled=true：只让各币"分析 + 立场提取"（等模型的部分）重叠。
    1) 主线程：预建图/模型客户端/OKX 客户端，按币顺序逐个读账户（与串行同样的读法；此时本场尚未下单）；
    2) 线程池（每币一路，不设上限，不错开）：analyze + stances.extract，每路 copy_context().run 独立上下文；
    3) 主线程按 cfg 币种顺序（BTC→ETH→SOL）逐个等待该币分析结果后串行：decide_and_execute（锁内重读账户算护栏）→ record_stances → decisions。
    单币分析失败按币记入 errors（顺序与串行一致），不影响其他币。"""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from desk import analysis, demo_exec, ledger, manager, okx_data, scoring, tools
    from desk import llm as dllm
    if (dllm.ta_config(cfg) or {}).get("checkpoint_enabled"):
        raise RuntimeError("parallel.enabled 要求 TradingAgents checkpoint_enabled=false（检查点会在共享图上重编译 self.graph）")
    analysis.build_graph(cfg, tracker)          # 预建：图、角色补丁、OKX 数据源/工具补丁，避免线程里懒加载竞态
    if os.environ.get("TA_DESK_MOCK_LLM") != "1":
        from desk import providers
        for role in (cfg.get("role_models") or {}):
            providers.build(cfg, role, tracker)  # 预建各角色模型客户端（资金流/立场抽取/交易经理等）
    tools._AS_OF["date"] = date                  # 三币同一交易日；线程内 analyze 写入的是同一值
    okx_data.client()
    print(f"[ta-desk] 并行分析开启：{[c['inst_id'] for c in coins]} 同时分析，决策执行仍按顺序串行", flush=True)

    prep: dict = {}
    for coin in coins:   # 主线程读账户；失败的币记下异常，阶段二按顺序写 errors
        try:
            prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
            prep[coin["inst_id"]] = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
        except Exception as e:
            prep[coin["inst_id"]] = e
    futs = {}
    with ThreadPoolExecutor(max_workers=len(coins), thread_name_prefix="ta-coin") as ex:
        for coin in coins:
            bk = prep[coin["inst_id"]]
            if isinstance(bk, Exception):
                continue
            ctx = contextvars.copy_context()
            futs[coin["inst_id"]] = ex.submit(ctx.run, _analyze_one, cfg, coin, date, bk, tracker)
        # 阶段二：主线程固定顺序串行（流水线：BTC 分析完即可决策，ETH/SOL 分析仍在后台等模型；按顺序 result() 等待）
        for coin in coins:
            inst = coin["inst_id"]
            try:
                if isinstance(prep[inst], Exception):
                    raise prep[inst]
                res, fs, st = futs[inst].result()
                rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker)
                rec["report_dir"] = res["report_dir"]
                scoring.record_stances(cfg, rec)
                decisions.append(rec)
                print(f"[ta-desk] {inst}: TA评级={res['signal']} 共识={rec['consensus']:+.3f} → {rec['final_action']}", flush=True)
            except Exception as e:
                traceback.print_exception(e)
                errors.append(f"{inst} 分析/决策失败：{type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
