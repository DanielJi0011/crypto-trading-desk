"""Threads 讨论摘录生成器（只读公开内容）。

根因：情绪分析师读 state/x_feed/<日期>/<币>.md，但盒子上没有 X 凭据（secrets.env 无 X 相关变量名），
XAI_API_KEY 只能走中转、不能用于官方 x_search；而例行任务里的 X 搜索步骤标为“可选”，所以只在 10-20/10-21 手工生成过一次，之后一直空。
现做法：例行任务（agent 用 Threads 公开搜索采集，整理成 X API 形状的原始 JSON）每日把三个币的搜索原始 JSON 存为
state/x_feed/<日期>/raw_<币>.json，然后运行 python -m desk.x_feed_build <日期> 生成 md。
本模块不联网、不发帖、不做任何互动，只做格式化。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from desk import settings

COINS = ("BTC", "ETH", "SOL")


def _load(path: Path) -> dict:
    """容忍工具输出外层包装：取第一行以 { 开头的 JSON。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return {}


def score(p: dict) -> int:
    m = p.get("public_metrics") or {}
    return m.get("like_count", 0) + 2 * m.get("retweet_count", 0) + m.get("reply_count", 0) + m.get("quote_count", 0)


def _post_source(p: dict, raw_source) -> str:
    """单条来源：帖子自带 source 优先，其次 raw 级 source；缺失按 X（兼容存量批次）。"""
    s = p.get("source") or raw_source
    return "threads" if s == "threads" else "x"


def render(coin: str, date: str, raw: dict, top: int = 20) -> str:
    users = {u.get("id"): u.get("username", "?") for u in (raw.get("includes") or {}).get("users", [])}
    posts = sorted(raw.get("data") or [], key=score, reverse=True)[:top]
    srcs = [_post_source(p, raw.get("source")) for p in posts]
    nx, nt = srcs.count("x"), srcs.count("threads")
    mixed = nx > 0 and nt > 0
    if mixed:   # 混合语料必须诚实标注（2026-11-22 共识包 F 项）：标题写明构成、逐条标来源
        out = [f"# {coin}：X + Threads 混合讨论摘录（{date}，过去约 24 小时）",
               f"说明：X {nx} 条 + Threads {nt} 条（X 语料不足时由 Threads 补足），按互动排序、逐条标注来源；"
               "原文摘录未翻译，仅作情绪参考。", ""]
    elif raw.get("source") == "threads" or (nt and not nx):   # 来源标注按 raw 的 source 字段走；缺失或 "x" 用 X wording（兼容 2026-11-01 等真实 X 存量批次）
        out = [f"# {coin}：Threads 讨论摘录（{date}，过去约 24 小时）",
               "说明：公开 Threads 英文搜索，按互动排序；原文摘录未翻译，仅作情绪参考。", ""]
    else:
        out = [f"# {coin}：X 讨论摘录（{date}，过去约 24 小时）",
               "说明：公开 X 英文搜索，按互动排序；原文摘录未翻译，仅作情绪参考。", ""]
    for i, (p, s) in enumerate(zip(posts, srcs), 1):
        m = p.get("public_metrics") or {}
        text = " ".join((p.get("text") or "").split())[:280]
        tag = f"（{'Threads' if s == 'threads' else 'X'}）" if mixed else ""
        out.append(f"{i}. @{users.get(p.get('author_id'), p.get('author_id'))}{tag} 赞{m.get('like_count', 0)} "
                   f"回复{m.get('reply_count', 0)} 转发{m.get('retweet_count', 0)}（{p.get('created_at', '')}）：{text}")
    return "\n".join(out) + "\n"


def merge_raw(x_raw: dict, th_raw: dict) -> dict:
    """X 原始语料 + Threads 补足侧车合并：逐条打来源标记、按 id 去重（X 优先），不改动任一原始文件。"""
    seen, data = set(), []
    for p in x_raw.get("data") or []:
        if p.get("id") not in seen:
            seen.add(p.get("id"))
            data.append({**p, "source": _post_source(p, x_raw.get("source"))})
    for p in th_raw.get("data") or []:
        if p.get("id") not in seen:
            seen.add(p.get("id"))
            data.append({**p, "source": "threads"})
    users = {}
    for r in (x_raw, th_raw):
        for u in (r.get("includes") or {}).get("users", []):
            users[u.get("id")] = u
    return {"source": "mixed", "data": data, "includes": {"users": list(users.values())}}


def build(date: str) -> dict[str, int]:
    d = settings.STATE / "x_feed" / date
    done = {}
    for c in COINS:
        raw_p = d / f"raw_{c}.json"
        if not raw_p.exists():
            continue
        raw = _load(raw_p)
        if not raw.get("data"):
            continue
        th_p = d / f"raw_{c}_threads.json"   # Threads 补足侧车（看门狗在 X 语料 <5 条时写入）
        if th_p.exists():
            th = _load(th_p)
            if th.get("data"):
                merged = merge_raw(raw, th)
                # X 不足 5 条时补足目标约 12 条；X 本来够量则维持 20 条上限
                top = 12 if len(raw["data"]) < 5 else 20
                (d / f"{c}.md").write_text(render(c, date, merged, top=top), encoding="utf-8")
                done[c] = len(merged["data"])
                continue
        (d / f"{c}.md").write_text(render(c, date, raw), encoding="utf-8")
        done[c] = len(raw["data"])
    return done


if __name__ == "__main__":
    # 用法：python -m desk.x_feed_build <日期> [morning|evening]；晚场目录 state/x_feed/<日期>-evening/
    from desk import sessions
    r = build(sessions.key(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "morning"))
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r else 1)
