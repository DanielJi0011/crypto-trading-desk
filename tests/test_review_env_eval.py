"""自我迭代 v2 第三层修正：手册修订的评估只用修订针对的环境（同行情、同币种）样本。

- env_scope_of：由触发样本推出主导环境；
- evaluate_and_rollback：有 env_scope → 只用该环境样本判定保留/回滚，
  上涨市 BTC 的样本不能把下跌市 ETH 的规则判死；
- 无 env_scope（旧记录）→ 回退全局评估，事件标注 eval_global=True；
- maybe_edit_playbooks（edit 模式）：基线同样按环境算，env_scope 落进手册历史。离线。"""
from desk import review, roles, settings


def _s(regime, inst, hit, time="2026-06-01 00:00:00+0800"):
    return {"regime": regime, "inst_id": inst, "hit": hit, "time": time,
            "date": time[:10], "direction": "bearish", "confidence": 0.5,
            "outcome": "跌" if hit else "涨", "ret_pct": -1.0 if hit else 1.0, "stop_quality": None}


def _cfg(**kw):
    cfg = settings.load_config()
    cfg["learning"].update(kw)
    return cfg


# ------------------------------------------------------------------ 工具函数
def test_env_scope_of_modal_and_empty():
    xs = [_s("下跌", "ETH-USDT-SWAP", True)] * 3 + [_s("上涨", "BTC-USDT-SWAP", False)]
    sc = review.env_scope_of(xs)
    assert sc["regime"] == "下跌" and sc["inst_id"] == "ETH-USDT-SWAP" and sc["basis_n"] == 3 and sc["mixed"] is True
    assert review.env_scope_of([]) is None
    assert review._scoped(xs, None) == xs
    assert len(review._scoped(xs, sc)) == 3


# ------------------------------------------------------------------ 评估：环境过滤
def _setup_eval(monkeypatch, meta_entry, all_scores):
    monkeypatch.setattr(review, "ROLE_KEYS", ["market"])
    meta = {"current": 2, "history": [meta_entry]}
    monkeypatch.setattr(review.roles, "read_meta", lambda role: meta)
    monkeypatch.setattr(review.scoring, "scores", lambda role=None: all_scores)
    return meta


def test_eval_scoped_ignores_other_env(monkeypatch):
    entry = {"version": 2, "time": "2026-01-01 00:00:00+0800", "eval_status": "pending",
             "baseline_hit": 0.9, "previous": 1,
             "env_scope": {"regime": "下跌", "inst_id": "ETH-USDT-SWAP", "basis_n": 20, "mixed": False}}
    scores = ([_s("下跌", "ETH-USDT-SWAP", True) for _ in range(4)]            # 本环境：全命中
              + [_s("上涨", "BTC-USDT-SWAP", False) for _ in range(10)])       # 别环境：全错（全局算会判回滚）
    meta = _setup_eval(monkeypatch, entry, scores)
    ev = review.evaluate_and_rollback(_cfg(eval_window_after_edit=4))
    assert len(ev) == 1 and ev[0]["type"] == "保留" and ev[0]["post"] == 1.0
    assert ev[0]["eval_global"] is False and ev[0]["env_scope"]["regime"] == "下跌"
    assert meta["history"][0]["eval_status"] == "kept"


def test_eval_scoped_waits_for_enough_env_samples(monkeypatch):
    entry = {"version": 2, "time": "2026-01-01 00:00:00+0800", "eval_status": "pending",
             "baseline_hit": 0.5, "previous": 1,
             "env_scope": {"regime": "下跌", "inst_id": "ETH-USDT-SWAP", "basis_n": 20, "mixed": False}}
    scores = ([_s("下跌", "ETH-USDT-SWAP", True) for _ in range(3)]            # 本环境只有 3 条 < 窗口 4
              + [_s("上涨", "BTC-USDT-SWAP", True) for _ in range(10)])
    _setup_eval(monkeypatch, entry, scores)
    assert review.evaluate_and_rollback(_cfg(eval_window_after_edit=4)) == []   # 不拿别环境凑数


def test_eval_fallback_global_when_no_scope(monkeypatch, tmp_path):
    entry = {"version": 2, "time": "2026-01-01 00:00:00+0800", "eval_status": "pending",
             "baseline_hit": 0.9, "previous": 1}                               # 无 env_scope（旧记录）
    scores = [_s("上涨", "BTC-USDT-SWAP", False) for _ in range(4)]
    _setup_eval(monkeypatch, entry, scores)
    vf = tmp_path / "v001.md"; vf.write_text("## 经验规则\n- 旧规则\n", encoding="utf-8")
    monkeypatch.setattr(review.roles, "version_path", lambda role, v: vf)
    saved = {}
    monkeypatch.setattr(review.roles, "save_new_version",
                        lambda role, learned, *a, **k: saved.update(learned=learned) or 3)
    ev = review.evaluate_and_rollback(_cfg(eval_window_after_edit=4))
    assert len(ev) == 1 and ev[0]["type"] == "回滚" and ev[0]["post"] == 0.0
    assert ev[0]["eval_global"] is True and ev[0]["env_scope"] is None      # 回退全局且已标注


# ------------------------------------------------------------------ 修订：基线按环境算 + scope 落盘
def test_edit_baseline_scoped_and_scope_recorded(monkeypatch):
    roles.ensure_playbooks()
    monkeypatch.setattr(review, "ROLE_KEYS", ["market"])
    pre = ([_s("上涨", "BTC-USDT-SWAP", False, "2026-01-01 00:00:00+0800") for _ in range(20)]
           + [_s("下跌", "ETH-USDT-SWAP", True, "2026-02-01 00:00:00+0800") for _ in range(20)])
    fresh = [_s("下跌", "ETH-USDT-SWAP", True, "2999-01-01 00:00:00+0800") for _ in range(25)]
    distract = [_s("上涨", "BTC-USDT-SWAP", False, "2999-06-01 00:00:00+0800") for _ in range(20)]
    allsc = pre + fresh + distract
    monkeypatch.setattr(review.scoring, "scores", lambda role=None: allsc)
    monkeypatch.setattr(review.scoring, "scorecard", lambda role: {"n": len(allsc), "recent": {"hit": 0.5}})
    monkeypatch.setattr(review.roles, "current_text", lambda role: "手册全文")
    monkeypatch.setattr(review, "recent_lessons", lambda role: [])
    ed = review.PlaybookEdit(no_change=False, learned_rules="- 新规则",
                             changes=[review.Change(op="新增", rule="规则X", why="依据Y")])

    class M:
        def with_structured_output(self, s):
            return self
        def invoke(self, p):
            return ed
    monkeypatch.setattr(review.dllm, "make_llm", lambda *a, **k: M())
    ev = review.maybe_edit_playbooks(_cfg(auto_edit_playbooks=True), None)
    rev = [e for e in ev if e["type"] == "修订"]
    assert len(rev) == 1
    # 全局最后 20 条全是别环境错样本（基线会算 0.0）；按环境算：下跌/ETH 最后 20 条全命中 → 1.0
    assert rev[0]["baseline_hit"] == 1.0
    assert rev[0]["env_scope"]["regime"] == "下跌" and rev[0]["env_scope"]["inst_id"] == "ETH-USDT-SWAP"
    hist = roles.read_meta("market")["history"][-1]
    assert hist["env_scope"]["regime"] == "下跌" and hist["env_scope"]["inst_id"] == "ETH-USDT-SWAP"
    assert hist["eval_status"] == "pending"
