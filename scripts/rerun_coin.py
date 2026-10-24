"""只重跑单币每日流程（分析→立场→经理决策执行），不做打分/复盘/研究，不改主日报。
用法：python -m scripts.rerun_coin ETH [--date YYYY-MM-DD]；日报写到 reports/<date>-<COIN>-rerun.md 供合并。"""
import sys, argparse, traceback
from desk import settings
from desk import okx_demo as _evsrc; _evsrc.set_event_source("ops")  # O3：运维脚本事件来源，不计入每日下单数
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("coin"); ap.add_argument("--date", default="")
    a = ap.parse_args()
    from desk import analysis, ledger, manager, okx_data, report, scoring, stances, demo_exec
    from desk import llm as dllm
    cfg = settings.load_config(); ledger.assert_paper(cfg)
    if demo_exec.is_demo(cfg): ledger._demo("startup", cfg)
    if not dllm.prepare_env(cfg): print("缺LLM密钥"); return 2
    date = a.date or settings.today()
    coin = [c for c in cfg["coins"] if c["inst_id"].split("-")[0] == a.coin.upper()][0]
    tracker = dllm.UsageTracker(cfg); errors, decisions = [], []
    if settings.no_new_positions(): print(f"紧急停止/全冻结文件存在（{settings.KILL_PATH} / {settings.HALT_PATH}），退出"); return 3
    try:
        prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
        bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
        res = analysis.analyze(cfg, coin, date, bk, tracker)
        fs = res["final_state"]; fs["_signal"] = res["signal"]
        tok = dllm.STAGE.set(f"{coin['inst_id']}/立场抽取")
        try: st = stances.extract(fs, cfg, tracker)
        finally: dllm.STAGE.reset(tok)
        rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker)
        rec["report_dir"] = res["report_dir"]; scoring.record_stances(cfg, rec); decisions.append(rec)
        print(f"[rerun] {coin['inst_id']}: 共识={rec['consensus']:+.3f} → {rec['final_action']}", flush=True)
    except Exception as e:
        traceback.print_exc(); errors.append(f"{coin['inst_id']} 分析/决策失败：{type(e).__name__}: {e}")
    usage = tracker.summary()
    settings.write_json(settings.STATE / "usage" / f"run-{dllm.RUN_ID}.json", usage)
    prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
    p = report.write(cfg, date, decisions, [], [], [], [], usage, prices, errors, mock=False, research=None,
                     name=f"{date}-{coin['inst_id'].split('-')[0]}-rerun")
    print(f"[rerun] 报告 {p}；用量 {usage['calls']} 次 ${usage['cost_usd']:.4f}", flush=True)
    return 1 if errors else 0
if __name__ == "__main__": sys.exit(main())
