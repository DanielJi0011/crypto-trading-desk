"""主备 key 自动切换（2026-11-15）：claude 渠道主 key 重试耗尽（可重试错误）后，同一次 invoke
自动改用备用 key 孪生重发；主 key 健康时备用零调用；备用 env 缺失时无孪生、行为与旧版一致；
主备都失败照常抛错并记 FAILURES；400 类非可重试错误不触发切换（切了也没用，只烧钱）。
用量账本每条记录带 key 标签（primary/backup），日报有备用 key 当日花销行。离线，不 sleep，不联网。"""
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult

from desk import llm as dllm
from desk import llm_retry as R
from desk import providers, report, settings


class E503(Exception):
    status_code = 503


class E400(Exception):
    status_code = 400


CALLS: dict = {}
LABELS: dict = {}


class StubModel(BaseChatModel):
    """按 mode 恒定行为的桩模型：ok 返回固定文本；503/400 恒抛对应错误。记录调用次数与调用时的 key 标签。"""
    name: str = "stub"
    mode: str = "ok"

    @property
    def _llm_type(self) -> str:
        return "stub-key-failover"

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        CALLS[self.name] = CALLS.get(self.name, 0) + 1
        LABELS.setdefault(self.name, []).append(R.KEY_LABEL.get())
        if self.mode == "503":
            raise E503("Error code: 503")
        if self.mode == "400":
            raise E400("Error code: 400")
        msg = AIMessage(content=f"ok from {self.name}")
        msg.usage_metadata = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}
        msg.response_metadata = {"model_name": "claude-opus-5-5"}
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    waits = []
    monkeypatch.setattr(R, "SLEEP", waits.append)
    monkeypatch.setattr(R, "RETRIES", [])
    monkeypatch.setattr(R, "FAILURES", [])
    monkeypatch.setattr(R, "SWITCHES", [])
    R.clear_backups()
    CALLS.clear()
    LABELS.clear()
    yield waits
    R.clear_backups()


@pytest.fixture
def installed(monkeypatch):
    """以零退避安装 invoke 包装（主 key 2 次重试、备用 1 次重试），结束时还原类方法与退避表。"""
    monkeypatch.setattr(R, "_installed", False)
    orig_invoke = BaseChatModel.invoke
    old_delays, old_bdelays = R.DELAYS, R.BACKUP_DELAYS
    R.install({"llm": {"retry_delays": [0, 0], "backup_retry_delays": [0]}})
    try:
        yield
    finally:
        monkeypatch.setattr(BaseChatModel, "invoke", orig_invoke)
        R.DELAYS, R.BACKUP_DELAYS = old_delays, old_bdelays


def test_failover_recovers_via_backup(installed):
    primary = StubModel(name="p", mode="503")
    twin = StubModel(name="b", mode="ok")
    R.register_backup(primary, twin)
    out = primary.invoke("hi")
    assert out.content == "ok from b"
    assert CALLS["p"] == 3 and CALLS["b"] == 1          # 主 key 预算耗尽（1+2 次）后备用一次成功
    assert LABELS["b"] == ["backup"] and LABELS["p"] == ["primary"] * 3
    assert len(R.SWITCHES) == 1 and R.FAILURES == []     # 备用接手成功不算失败
    assert R.KEY_LABEL.get() == "primary"                # 切换结束标签复位


def test_primary_healthy_backup_never_called(installed):
    primary = StubModel(name="p", mode="ok")
    twin = StubModel(name="b", mode="ok")
    R.register_backup(primary, twin)
    out = primary.invoke("hi")
    assert out.content == "ok from p"
    assert CALLS == {"p": 1} and R.SWITCHES == [] and LABELS["p"] == ["primary"]


def test_both_fail_raises_and_records_failure(installed):
    primary = StubModel(name="p", mode="503")
    twin = StubModel(name="b", mode="503")
    R.register_backup(primary, twin)
    with pytest.raises(E503):
        primary.invoke("hi")
    assert CALLS["p"] == 3 and CALLS["b"] == 2           # 备用小预算（1+1 次）也耗尽
    assert len(R.SWITCHES) == 1 and len(R.FAILURES) == 1  # 最终失败由备用那次尝试记账


def test_400_never_switches(installed):
    primary = StubModel(name="p", mode="400")
    twin = StubModel(name="b", mode="ok")
    R.register_backup(primary, twin)
    with pytest.raises(E400):
        primary.invoke("hi")
    assert CALLS == {"p": 1} and R.SWITCHES == [] and R.FAILURES == []


def test_no_twin_behaves_as_before(installed):
    primary = StubModel(name="p", mode="503")
    with pytest.raises(E503):
        primary.invoke("hi")
    assert CALLS["p"] == 3 and len(R.FAILURES) == 1 and R.SWITCHES == []


def test_providers_registers_backup_only_for_claude_with_backup_env(monkeypatch):
    cfg = settings.load_config()
    monkeypatch.delenv("TA_DESK_MOCK_LLM", raising=False)   # 走真实构造（仅本地建客户端，不发请求）
    monkeypatch.setenv("CLAUDE_RELAY_API_KEY", "dummy-primary")
    monkeypatch.delenv("CLAUDE_RELAY_API_KEY_BACKUP", raising=False)
    providers._cache.clear()
    c1 = providers.build(cfg, "trade_manager", None)
    assert R.backup_twin(c1) is None                        # 备用 env 缺失 → 无孪生，行为与旧版一致
    monkeypatch.setenv("CLAUDE_RELAY_API_KEY_BACKUP", "dummy-backup")
    providers._cache.clear()
    c2 = providers.build(cfg, "reviewer", None)
    assert R.backup_twin(c2) is not None                    # claude 渠道 + 备用 env 在 → 登记孪生
    monkeypatch.setenv("XAI_API_KEY", "dummy-grok")
    monkeypatch.setenv("XAI_API_KEY_BACKUP", "dummy-grok-backup")
    providers._cache.clear()
    c3 = providers.build(cfg, "sentiment", None)
    assert R.backup_twin(c3) is None                        # 非 claude 渠道即便有同名备用变量也不登记
    providers._cache.clear()


def _llm_result(inp=5, out=2):
    msg = AIMessage(content="x")
    msg.usage_metadata = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    msg.response_metadata = {"model_name": "claude-opus-5-5"}
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def test_usage_tracker_labels_key_and_summary_by_key():
    cfg = settings.load_config()
    tr = dllm.UsageTracker(cfg)
    n0 = len(settings.read_jsonl(tr.path)) if tr.path.exists() else 0
    tr.on_llm_end(_llm_result(), run_id="kf-1")
    tok = R.KEY_LABEL.set("backup")
    try:
        tr.on_llm_end(_llm_result(), run_id="kf-2")
    finally:
        R.KEY_LABEL.reset(tok)
    assert tr.calls[-2]["key"] == "primary" and tr.calls[-1]["key"] == "backup"
    new_lines = settings.read_jsonl(tr.path)[n0:]
    assert [r["key"] for r in new_lines] == ["primary", "backup"]   # 落盘台账同样带标签
    s = tr.summary()
    assert s["by_key"]["backup"] == {"calls": 1, "input": 5, "cached": 0, "output": 2,
                                     "cost_usd": s["by_key"]["backup"]["cost_usd"], "unpriced_calls": 1}
    assert s["by_key"]["primary"]["calls"] == 1


def test_backup_key_line_aggregates_day_ledger():
    day = "2099-01-05"   # 独立日期文件，避免与同进程其他用例的当日台账互相干扰
    path = settings.STATE / "usage" / f"{day}.jsonl"
    settings.append_jsonl(path, {"run_id": "x", "key": "backup", "input": 1234})
    settings.append_jsonl(path, {"run_id": "x", "key": "backup", "input": 100})
    settings.append_jsonl(path, {"run_id": "x", "key": "backup", "error": "E503"})   # 失败记录不计花销
    settings.append_jsonl(path, {"run_id": "x", "key": "primary", "input": 999})
    line = report.backup_key_line(day, {})
    assert line is not None and "2 次" in line and "1,334" in line
    assert report.backup_key_line("2099-01-06", {}) is None
    fb = report.backup_key_line("2099-01-06", {"by_key": {"backup": {"calls": 3, "input": 42}}})
    assert fb is not None and "3 次" in fb and "42" in fb
