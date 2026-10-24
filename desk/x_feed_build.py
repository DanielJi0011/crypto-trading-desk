"""X 讨论摘录生成器（只读公开内容）。

根因：情绪分析师读 state/x_feed/<日期>/<币>.md，但盒子上没有 X 凭据（secrets.env 无 X 相关变量名），
XAI_API_KEY 只能走中转、不能用于官方 x_search；而例行任务里的 X 搜索步骤标为“可选”，所以只在 10-20/10-21 手工生成过一次，之后一直空。
现做法：例行任务（有只读 X 搜索工具的 agent）每日把三个币的搜索原始 JSON 存为
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


def render(coin: str, date: str, raw: dict, top: int = 20) -> str:
    users = {u.get("id"): u.get("username", "?") for u in (raw.get("includes") or {}).get("users", [])}
    posts = sorted(raw.get("data") or [], key=score, reverse=True)[:top]
    out = [f"# {coin}：X 讨论摘录（{date}，过去约 24 小时）",
           "说明：公开 X 英文搜索，按互动排序；原文摘录未翻译，仅作情绪参考。", ""]
    for i, p in enumerate(posts, 1):
        m = p.get("public_metrics") or {}
        text = " ".join((p.get("text") or "").split())[:280]
        out.append(f"{i}. @{users.get(p.get('author_id'), p.get('author_id'))} 赞{m.get('like_count', 0)} "
                   f"回复{m.get('reply_count', 0)} 转发{m.get('retweet_count', 0)}（{p.get('created_at', '')}）：{text}")
    return "\n".join(out) + "\n"


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
        (d / f"{c}.md").write_text(render(c, date, raw), encoding="utf-8")
        done[c] = len(raw["data"])
    return done


if __name__ == "__main__":
    # 用法：python -m desk.x_feed_build <日期> [morning|evening]；晚场目录 state/x_feed/<日期>-evening/
    from desk import sessions
    r = build(sessions.key(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "morning"))
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r else 1)
