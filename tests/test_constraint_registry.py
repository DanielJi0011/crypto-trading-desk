"""约束注册表一致性（2026-11-22 共识包 A 项）：
- 根目录 constraints.yaml 注册表与 config.yaml 的 constraint_tiers 段逐项一致（id 集合与 tier 都对得上）；
- 每条注册项必填字段齐全（tier/added/basis/retirement/owner/source），tier 与 source 取值合法；
- 门账裁决规则预注册块在场且关键数值与共识定版一致（防口径被悄悄改动）。"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    with open(ROOT / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_registry_matches_config_tiers():
    reg = _load("constraints.yaml")
    cfg = _load("config.yaml")
    tiers = cfg.get("constraint_tiers") or {}
    entries = {c["id"]: c for c in reg["constraints"]}
    assert set(entries) == set(tiers), (
        f"注册表与 config.constraint_tiers 的 id 集合不一致："
        f"只在注册表 {sorted(set(entries) - set(tiers))}，只在 config {sorted(set(tiers) - set(entries))}")
    for cid, tier in tiers.items():
        assert entries[cid]["tier"] == tier, f"{cid}：注册表 tier={entries[cid]['tier']} ≠ config tier={tier}"


def test_registry_fields_complete():
    reg = _load("constraints.yaml")
    for c in reg["constraints"]:
        for k in ("id", "tier", "class", "added", "basis", "config_ref", "retirement", "owner", "source"):
            assert c.get(k), f"{c.get('id')}：缺字段 {k}"
        assert c["tier"] in ("hard", "soft", "advisory"), f"{c['id']}：tier 非法 {c['tier']}"
        assert c["source"] in ("user", "claude_design"), f"{c['id']}：source 必须如实区分 user / claude_design"
        assert c["owner"] in ("user", "claude_design"), f"{c['id']}：owner 非法 {c['owner']}"
    # hard 约束必须全部是用户来源（Muse 不得自行设立生存约束）
    for c in reg["constraints"]:
        if c["class"] == "guardrail":
            assert c["tier"] == "hard" and c["source"] == "user", f"{c['id']}：生存约束必须 hard 且来源为用户"


def test_adjudication_preregistered():
    reg = _load("constraints.yaml")
    adj = reg.get("gate_adjudication") or {}
    assert adj.get("applies_to") == ["TREND_DIST", "H4_STRUCT", "TREND_OVERSOLD"]
    th = adj.get("enter_threshold") or {}
    assert th.get("would_block_judged_min") == 10 and th.get("distinct_cluster_days_min") == 10
    assert "净反事实 R" in (adj.get("verdict_rule") or "")
    assert adj.get("fill_model") == "1H 触价、同根 K 止损优先"
