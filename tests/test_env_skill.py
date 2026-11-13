"""自我迭代 v2 第一层：分环境 skill 诊断（scoring.env_skill_table + 日报小节）与学习开关。

规则：只列预测席位（market/sentiment/news/flows/research_manager）；辩手与风控不排名；
n<20 的格子标“样本不足”、不下结论；权重保持冻结。离线。"""
from desk import report as R
from desk import scoring, settings


def _row(role, regime, inst, hit=True, skill=0.1, brier=0.2, v=2):
    return {"role": role, "regime": regime, "inst_id": inst, "hit": hit,
            "skill": skill, "brier": brier, "scoring_v": v, "time": "2026-11-01 00:00:00+0800"}


def _fake_scores(monkeypatch):
    data = {
        # 下跌/BTC：25 条全命中 → 充足
        "market": [_row("market", "下跌", "BTC-USDT-SWAP", True, 0.12, 0.18) for _ in range(25)]
                  + [_row("market", "上涨", "ETH-USDT-SWAP", False, -0.2, 0.4) for _ in range(5)],   # n=5 样本不足
        # 震荡/SOL：恰好 20 条 → 充足（边界）
        "sentiment": [_row("sentiment", "震荡", "SOL-USDT-SWAP", i % 2 == 0, 0.0, 0.25) for i in range(20)],
        # 辩手/风控样本再多也不进表
        "bear": [_row("bear", "下跌", "BTC-USDT-SWAP", True) for _ in range(50)],
        "aggressive": [_row("aggressive", "下跌", "BTC-USDT-SWAP", True) for _ in range(50)],
        "bull": [], "news": [], "flows": [], "research_manager": [],
    }
    monkeypatch.setattr(scoring, "scores", lambda role=None: data.get(role, []))
    return data


def test_env_table_only_forecast_roles(monkeypatch):
    _fake_scores(monkeypatch)
    tbl = scoring.env_skill_table()
    roles = {e["role"] for e in tbl}
    assert roles <= set(scoring.FORECAST_ROLES)
    assert "bear" not in roles and "aggressive" not in roles and "bull" not in roles


def test_env_table_values_and_sufficiency(monkeypatch):
    _fake_scores(monkeypatch)
    tbl = {(e["role"], e["regime"], e["inst_id"]): e for e in scoring.env_skill_table()}
    down = tbl[("market", "下跌", "BTC-USDT-SWAP")]
    assert down["n"] == 25 and down["hit"] == 1.0 and down["skill"] == 0.12 and down["brier"] == 0.18
    assert down["sufficient"] is True
    up = tbl[("market", "上涨", "ETH-USDT-SWAP")]
    assert up["n"] == 5 and up["sufficient"] is False
    edge = tbl[("sentiment", "震荡", "SOL-USDT-SWAP")]
    assert edge["n"] == 20 and edge["sufficient"] is True and edge["hit"] == 0.5


def test_report_section_marks_insufficient(monkeypatch):
    _fake_scores(monkeypatch)
    text = "\n".join(R.env_skill_lines())
    assert "分环境 skill 诊断" in text and "不参与决策、不影响权重" in text
    assert "样本不足" in text                       # 上涨/ETH n=5 的格子
    assert "空头研究员" not in text and "激进风控" not in text   # 辩手/风控不进表
    assert "上涨市样本合计 5 条" in text


def test_learning_switches_exact():
    """选项 A 只开 playbook_proposals；其余学习开关保持关闭，权重冻结不动。"""
    L = settings.load_config()["learning"]
    assert L["playbook_proposals"] is True
    assert L["apply_weights"] is False
    assert L["auto_edit_playbooks"] is False
    assert L["research_write_playbook"] is False
    assert L["ta_memory_inject"] is False
    # 硬规则保持原值
    assert L["min_scored_before_edit"] == 20 and L["max_changes_per_edit"] == 2
    assert L["eval_window_after_edit"] == 20 and L["rollback_if_worse_pp"] == 5.0
