"""LLM 调用自动重试（2026-10-24）：503/502/500/504/429、超时、连接错误按指数退避重试，每次重试记日志。
全部失败才抛出，并记入 FAILURES，日报列为"需报告用户"。只包裹 BaseChatModel.invoke，不改任何交易逻辑。

主备 key 自动切换（2026-11-15）：claude 渠道客户端在构造处（desk/providers.py）可登记一个
同参数的备用 key 孪生客户端。主 key 的重试预算耗尽（可重试错误跑不通）后，同一次 invoke
自动改用备用孪生重发（小重试预算）；备用更贵，因此绝不默认走它、每次新调用都重新从主 key
开始（不做全局粘性切换），非可重试错误（如 400 请求错）不触发切换。每次切换记 SWITCHES 并
在运行日志留一行；调用实际由哪把 key 服务经 KEY_LABEL 供用量账本标注（primary/backup）。"""
from __future__ import annotations

import contextvars
import logging
import time

log = logging.getLogger("desk.llm_retry")
DEFAULT_DELAYS = (5, 15, 30, 60)
RETRY_STATUS = {429, 500, 502, 503, 504}
SLEEP = time.sleep          # 测试可替换
DELAYS = list(DEFAULT_DELAYS)
RETRIES: list[dict] = []    # 本进程每次重试的记录
FAILURES: list[dict] = []   # 重试用尽仍失败的调用
_installed = False

# ------------------------------------------------------------------ 主备 key 切换
KEY_LABEL = contextvars.ContextVar("llm_key_label", default="primary")   # 当前调用由哪把 key 服务（用量账本标注用）
SWITCHES: list[dict] = []        # 本进程每次主→备切换的记录
BACKUP_DELAYS = (5, 15)          # 备用 key 的重试预算（2 次重试；可被 config llm.backup_retry_delays 覆盖）
_BACKUP_TWINS: dict[int, tuple] = {}   # id(主客户端) → (主客户端, 备用客户端)；强引用防 id 被回收复用


def register_backup(primary, twin) -> None:
    """登记主客户端的备用 key 孪生（构造处调用，见 desk/providers.py）。"""
    _BACKUP_TWINS[id(primary)] = (primary, twin)


def backup_twin(model):
    """查某客户端登记的备用孪生；无登记（或备用 env 缺失时根本没建）返回 None。"""
    t = _BACKUP_TWINS.get(id(model))
    return t[1] if t and t[0] is model else None


def clear_backups() -> None:
    _BACKUP_TWINS.clear()


def retryable(e: BaseException) -> bool:
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        n = type(e).__name__
        if n in ("APITimeoutError", "APIConnectionError", "TimeoutException", "ConnectTimeout", "ReadTimeout",
                 "ConnectError", "RemoteProtocolError", "ReadError", "TimeoutError", "ConnectionError"):
            return True
        code = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(code, int) and code in RETRY_STATUS:
            return True
        s = str(e)
        if any(f"Error code: {c}" in s for c in RETRY_STATUS):
            return True
        e = e.__cause__ or e.__context__
    return False


def _stage():
    try:
        from desk import llm as dllm
        return dllm.STAGE.get()
    except Exception:
        return None


# ------------------------------------------------------------------ 提示词体积保险丝
# 2026-11-14 晚场用量事故：台账出现单次调用输入 18.6–32 万 token 的记录（opus 席位），而全仓
# 提示词组装的最大合法体积经实测只有约 4.5 万字符（回归测试见 tests/test_prompt_size_bounds.py）：
# 膨胀不可能来自任何单个组装组件，只可能来自无上限的原始载荷/上下文累积或供应商侧计量。
# 这里在全系统唯一的 LLM 调用咽喉（本模块包装的 BaseChatModel.invoke）加一道硬上限作保险丝：
# 超限时只截断消息内容（保头保尾、注明截断），不删消息、不改调用结构，正常体积永不触发；
# 每次触发都记 TRUNCATIONS 并进日报 summary，让这类膨胀从此在台子自己的日志里留痕。
PROMPT_CAP_CHARS = 240_000   # 约合 8–12 万 token，远高于实测最大合法提示（约 4.5 万字符）
_prompt_cap = PROMPT_CAP_CHARS
TRUNCATIONS: list[dict] = []   # 本进程每次保险丝触发的记录


def set_prompt_cap(chars):
    global _prompt_cap
    try:
        _prompt_cap = max(10_000, int(chars))
    except Exception:
        pass


def _content_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for b in content:
            if isinstance(b, str):
                n += len(b)
            elif isinstance(b, dict):
                for k in ("text", "content"):
                    if isinstance(b.get(k), str):
                        n += len(b[k])
            else:
                n += len(str(getattr(b, "content", b) or ""))
        return n
    return len(str(content or ""))


def _cut_text(s: str, keep: int) -> str:
    if len(s) <= keep:
        return s
    head = max(0, int(keep * 0.7))
    tail = max(0, keep - head - 60)
    return s[:head] + f"\n……（提示词过长，代码截断中段：原文 {len(s)} 字符）……\n" + (s[-tail:] if tail else "")


def _shrink_content(content, keep: int):
    if isinstance(content, str):
        return _cut_text(content, keep)
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str):
                out.append(_cut_text(b, keep) if len(b) > keep else b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str) and len(b["text"]) > keep:
                out.append({**b, "text": _cut_text(b["text"], keep)})
            else:
                out.append(b)
        return out
    return content


def _msg_content(m):
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", None)


def _is_system(m) -> bool:
    return type(m).__name__ == "SystemMessage" or (isinstance(m, dict) and m.get("role") == "system")


def guard_input(inp, cap=None):
    """保险丝：单次调用输入总字符超 cap 时，反复截短最大的消息内容（优先非系统消息）至达标。
    返回 (new_inp, cut_chars)；未超限或无法识别的入参原样返回 (inp, 0)。只改内容不删消息，
    tool 调用与结果的配对结构不变；PromptValue 超限时返回其消息列表（与原入参等价）。"""
    cap = _prompt_cap if cap is None else cap
    if isinstance(inp, str):
        if len(inp) <= cap:
            return inp, 0
        out = _cut_text(inp, cap)
        TRUNCATIONS.append({"stage": _stage(), "before": len(inp), "after": len(out), "cap": cap})
        log.warning("提示词保险丝触发（%s）：字符串输入 %d → %d 字符", _stage(), len(inp), len(out))
        return out, len(inp) - len(out)
    from langchain_core.messages import BaseMessage
    from langchain_core.prompt_values import PromptValue
    if isinstance(inp, PromptValue):
        msgs = inp.to_messages()
    elif isinstance(inp, (list, tuple)) and inp and all(isinstance(m, (BaseMessage, dict)) for m in inp):
        msgs = list(inp)
    else:
        return inp, 0
    total = sum(_content_len(_msg_content(m)) for m in msgs)
    if total <= cap:
        return inp, 0
    before = total
    for _ in range(80):
        total = sum(_content_len(_msg_content(m)) for m in msgs)
        if total <= cap:
            break
        cand = [( _is_system(m), _content_len(_msg_content(m)), i)
                for i, m in enumerate(msgs) if _content_len(_msg_content(m)) > 4000]
        if not cand:
            break
        cand.sort(key=lambda x: (x[0], -x[1]))
        _, n, i = cand[0]
        keep = max(4000, int(n * 0.55))
        m = msgs[i]
        if isinstance(m, dict):
            msgs[i] = {**m, "content": _shrink_content(m.get("content"), keep)}
        else:
            msgs[i] = m.model_copy(update={"content": _shrink_content(m.content, keep)})
    total = sum(_content_len(_msg_content(m)) for m in msgs)
    TRUNCATIONS.append({"stage": _stage(), "before": before, "after": total, "cap": cap})
    log.warning("提示词保险丝触发（%s）：输入 %d → %d 字符（上限 %d）", _stage(), before, total, cap)
    return msgs, before - total


def call(fn, *a, delays=None, any_error=False, record_failure=True, **k):
    """按 DELAYS 退避重试 fn；不可重试的错误直接抛出。
    delays：本次调用专用退避序列（缺省用全局 DELAYS）；any_error=True 时任何异常都重试（持有期到期判断用）。
    record_failure=False：重试耗尽时不记 FAILURES（主备切换的主 key 尝试用——备用接手成功则不算失败，
    备用也耗尽时由备用那次尝试按 True 记账）。"""
    ds = list(DELAYS if delays is None else delays)
    for i in range(len(ds) + 1):
        try:
            return fn(*a, **k)
        except Exception as e:
            if not (any_error or retryable(e)):
                raise
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            if i >= len(ds):
                if record_failure:
                    FAILURES.append({"stage": _stage(), "attempts": i + 1, "error": msg})
                log.error("LLM 调用重试 %d 次后仍失败（%s）：%s", i, _stage(), msg)
                raise
            RETRIES.append({"stage": _stage(), "attempt": i + 1, "wait": ds[i], "error": msg})
            log.warning("LLM 调用可重试错误（%s），第 %d 次重试，%ss 后：%s", _stage(), i + 1, ds[i], msg)
            SLEEP(ds[i])


def install(cfg=None):
    """进程内安装一次：包裹 langchain BaseChatModel.invoke。delays 取 config llm.retry_delays，
    提示词保险丝上限取 config llm.prompt_hard_cap_chars（缺省 PROMPT_CAP_CHARS），
    备用 key 重试预算取 config llm.backup_retry_delays（缺省 BACKUP_DELAYS）。"""
    global _installed, DELAYS, BACKUP_DELAYS
    d = ((cfg or {}).get("llm") or {}).get("retry_delays")
    if d is not None:
        DELAYS = [float(x) for x in d]
    bd = ((cfg or {}).get("llm") or {}).get("backup_retry_delays")
    if bd is not None:
        BACKUP_DELAYS = tuple(float(x) for x in bd)
    pc = ((cfg or {}).get("llm") or {}).get("prompt_hard_cap_chars")
    if pc is not None:
        set_prompt_cap(pc)
    if _installed:
        return
    from langchain_core.language_models.chat_models import BaseChatModel
    orig = BaseChatModel.invoke

    def invoke(self, *a, **k):
        if getattr(_tl, "depth", 0):            # 嵌套调用只在最外层重试
            return orig(self, *a, **k)
        _tl.depth = 1
        try:
            if a:
                try:
                    new0, cut = guard_input(a[0])
                    if cut:
                        a = (new0,) + tuple(a[1:])
                except Exception:
                    pass   # 保险丝自身出错绝不阻断调用
            twin = backup_twin(self)
            try:
                # 有备用孪生时主 key 尝试先不记 FAILURES：备用接手成功不算失败，备用也失败由它记账
                return call(orig, self, *a, record_failure=twin is None, **k)
            except Exception as e:
                if twin is None or not retryable(e):
                    raise   # 无备用，或非可重试错误（400 等切了也没用，只烧钱）→ 按原行为抛出
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                SWITCHES.append({"stage": _stage(), "error": msg})
                log.warning("主 key 重试耗尽（%s），切换备用 key 重发本次调用：%s", _stage(), msg)
                tok = KEY_LABEL.set("backup")
                try:
                    return call(orig, twin, *a, delays=list(BACKUP_DELAYS), **k)
                finally:
                    KEY_LABEL.reset(tok)
        finally:
            _tl.depth = 0
    BaseChatModel.invoke = invoke
    _installed = True


import threading  # noqa: E402
_tl = threading.local()


def report_lines() -> list[str]:
    out = []
    for f in FAILURES:
        out.append(f"【需报告用户】LLM 调用重试 {f['attempts'] - 1} 次后仍失败：{f['stage'] or '未知阶段'}，{f['error']}")
    return out


def summary_line() -> str | None:
    parts = []
    if RETRIES:
        ok = len(RETRIES) - sum(f["attempts"] - 1 for f in FAILURES)
        parts.append(f"LLM 可重试错误共重试 {len(RETRIES)} 次（其中 {max(ok, 0)} 次后成功恢复），失败 {len(FAILURES)} 个调用")
    if SWITCHES:
        parts.append(f"主 key 故障自动切换备用 key {len(SWITCHES)} 次（备用单价更高，花销见用量小节）")
    if TRUNCATIONS:
        parts.append(f"提示词保险丝触发 {len(TRUNCATIONS)} 次（超体积输入已截断至上限内，详见日志）")
    return "；".join(parts) if parts else None
