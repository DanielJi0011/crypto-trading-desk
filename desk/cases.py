"""案例库（自我迭代 v2 第二层）：state/cases.jsonl，一案一行。

案例是真正的学习载体：每条必须写明情境、决策、结果，并把教训写成
「有条件、能失效」的句子，且必须带反例（counter_example）——缺反例不入库，
防止案例库只记成功案例、变成幸存者偏差。

字段（schema v1）：
  id               案例编号，如 "C-01"
  date             日期（YYYY-MM-DD）
  session          场次（morning / evening / intraday / ""）
  inst_id          币种合约，如 "ETH-USDT-SWAP"；跨币种案例可写 "ALL"
  regime           行情判定 {"direction","strength","stage","confidence"}，无判定写 null
  context          情境（当时可得信息下的事实）
  decision         当时的决策/动作
  outcome          结果；可带 score_ids 链接 state/scores.jsonl 的打分 id
  lesson           教训：带适用条件的句子，不写全局断言
  counter_example  反例/失效条件：什么情况下这条教训不成立
  status           观察 / 已验证 / 已失效
  playbook_rule    关联手册规则（角色/版本/规则摘要），无则空串
"""
from __future__ import annotations

from desk import settings

CASES = settings.STATE / "cases.jsonl"

REQUIRED = ("id", "date", "session", "inst_id", "regime", "context", "decision",
            "outcome", "lesson", "counter_example", "status", "playbook_rule")
STATUSES = ("观察", "已验证", "已失效")


def validate(case: dict) -> list[str]:
    """返回校验错误列表（空 = 合法）。反例为空是最重要的拦截：缺反例不入库。"""
    errs = []
    for k in REQUIRED:
        if k not in case:
            errs.append(f"缺字段 {k}")
    if not str(case.get("id") or "").strip():
        errs.append("id 为空")
    if not str(case.get("counter_example") or "").strip():
        errs.append("缺反例 counter_example（无反例不入库）")
    if not str(case.get("lesson") or "").strip():
        errs.append("教训 lesson 为空")
    if case.get("status") not in STATUSES:
        errs.append(f"status 必须是 {'/'.join(STATUSES)}")
    rg = case.get("regime")
    if rg is not None and not isinstance(rg, dict):
        errs.append("regime 必须是 dict 或 null")
    oc = case.get("outcome")
    if oc is not None and not isinstance(oc, (str, dict)):
        errs.append("outcome 必须是 str 或 dict")
    return errs


def load() -> list[dict]:
    """读全部案例（文件不存在 → 空列表）。只返回通过校验的行，坏行跳过不炸调用方。"""
    out = []
    for c in settings.read_jsonl(CASES):
        if not validate(c):
            out.append(c)
    return out


def append(case: dict) -> dict:
    """校验后追加一条案例；不合法抛 ValueError，id 重复也拒绝。"""
    errs = validate(case)
    if errs:
        raise ValueError("案例不合法：" + "；".join(errs))
    if any(c["id"] == case["id"] for c in load()):
        raise ValueError(f"案例 id 已存在：{case['id']}")
    settings.append_jsonl(CASES, case)
    return case
