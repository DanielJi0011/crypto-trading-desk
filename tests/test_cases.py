"""自我迭代 v2 第二层：案例库 desk/cases.py。离线。

核心规则：教训必须带反例（counter_example），缺反例不入库——防幸存者偏差。"""
import json

import pytest

from desk import cases


@pytest.fixture(autouse=True)
def _tmp_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES", tmp_path / "cases.jsonl")


def _case(**kw):
    base = {"id": "C-99", "date": "2026-11-12", "session": "morning", "inst_id": "BTC-USDT-SWAP",
            "regime": {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.6},
            "context": "情境", "decision": "决策", "outcome": {"text": "结果", "score_ids": []},
            "lesson": "在条件 X 下，Y 成立", "counter_example": "条件 X 不成立时 Y 不适用",
            "status": "观察", "playbook_rule": ""}
    base.update(kw)
    return base


def test_append_and_load_roundtrip():
    cases.append(_case())
    got = cases.load()
    assert len(got) == 1 and got[0]["id"] == "C-99" and got[0]["lesson"].startswith("在条件 X")


def test_missing_counter_example_rejected():
    with pytest.raises(ValueError, match="反例"):
        cases.append(_case(counter_example=""))
    with pytest.raises(ValueError, match="反例"):
        cases.append(_case(counter_example="   "))
    c = _case(); del c["counter_example"]
    with pytest.raises(ValueError):
        cases.append(c)
    assert cases.load() == []          # 一条都没写进去


def test_missing_field_and_bad_status_rejected():
    c = _case(); del c["lesson"]
    with pytest.raises(ValueError):
        cases.append(c)
    with pytest.raises(ValueError, match="status"):
        cases.append(_case(status="已归档"))


def test_duplicate_id_rejected():
    cases.append(_case())
    with pytest.raises(ValueError, match="已存在"):
        cases.append(_case())


def test_load_skips_invalid_lines(tmp_path):
    cases.append(_case(id="C-01"))
    bad = _case(id="C-02"); del bad["counter_example"]      # 手工塞一条无反例的坏行
    with open(cases.CASES, "a", encoding="utf-8") as f:
        f.write(json.dumps(bad, ensure_ascii=False) + "\n")
    assert [c["id"] for c in cases.load()] == ["C-01"]      # 坏行跳过，不炸调用方


def test_seed_file_shape():
    """本地种子文件（若存在）必须全部通过校验且含 C-01..C-05。"""
    from desk import settings
    seed = settings.STATE / "cases.jsonl"
    if not seed.exists():
        pytest.skip("种子文件不在此检出中")
    rows = [json.loads(l) for l in seed.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        assert cases.validate(r) == [], f"{r.get('id')} 校验失败：{cases.validate(r)}"
    ids = {r["id"] for r in rows}
    assert {"C-01", "C-02", "C-03", "C-04", "C-05"} <= ids
