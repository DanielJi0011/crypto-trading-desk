"""LLM 调用自动重试（2026-10-24）：503/502/500/504/429、超时、连接错误按指数退避重试，每次重试记日志。
全部失败才抛出，并记入 FAILURES，日报列为"需报告用户"。只包裹 BaseChatModel.invoke，不改任何交易逻辑。"""
from __future__ import annotations

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


def call(fn, *a, **k):
    """按 DELAYS 退避重试 fn；不可重试的错误直接抛出。"""
    for i in range(len(DELAYS) + 1):
        try:
            return fn(*a, **k)
        except Exception as e:
            if not retryable(e):
                raise
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            if i >= len(DELAYS):
                FAILURES.append({"stage": _stage(), "attempts": i + 1, "error": msg})
                log.error("LLM 调用重试 %d 次后仍失败（%s）：%s", i, _stage(), msg)
                raise
            RETRIES.append({"stage": _stage(), "attempt": i + 1, "wait": DELAYS[i], "error": msg})
            log.warning("LLM 调用可重试错误（%s），第 %d 次重试，%ss 后：%s", _stage(), i + 1, DELAYS[i], msg)
            SLEEP(DELAYS[i])


def install(cfg=None):
    """进程内安装一次：包裹 langchain BaseChatModel.invoke。delays 取 config llm.retry_delays。"""
    global _installed, DELAYS
    d = ((cfg or {}).get("llm") or {}).get("retry_delays")
    if d is not None:
        DELAYS = [float(x) for x in d]
    if _installed:
        return
    from langchain_core.language_models.chat_models import BaseChatModel
    orig = BaseChatModel.invoke

    def invoke(self, *a, **k):
        if getattr(_tl, "depth", 0):            # 嵌套调用只在最外层重试
            return orig(self, *a, **k)
        _tl.depth = 1
        try:
            return call(orig, self, *a, **k)
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
    if not RETRIES:
        return None
    ok = len(RETRIES) - sum(f["attempts"] - 1 for f in FAILURES)
    return f"LLM 可重试错误共重试 {len(RETRIES)} 次（其中 {max(ok, 0)} 次后成功恢复），失败 {len(FAILURES)} 个调用"
