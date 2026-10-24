"""R8 每币执行锁：state/locks/exec-<COIN>.lock，阻塞 flock，超时（默认 120 秒）放弃并记 exec_lock_timeout。
锁的范围：从读取 demo_exec.book 到下单/平仓/改止损完成；LLM 分析放在锁外，拿到锁后重新读持仓与挂单再执行。
同一进程内可重入（嵌套调用不会自锁）。"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time

from desk import settings

_held: dict[str, int] = {}


class ExecLockTimeout(RuntimeError):
    pass


def coin_of(inst: str) -> str:
    return inst.split("-")[0].upper()


def lock_path(inst: str):
    return settings.STATE / "locks" / f"exec-{coin_of(inst)}.lock"


@contextlib.contextmanager
def hold(inst: str, timeout: float = 120.0, why: str = ""):
    c = coin_of(inst)
    if _held.get(c):
        _held[c] += 1
        try:
            yield
        finally:
            _held[c] -= 1
        return
    p = lock_path(inst)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    t0 = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - t0 >= timeout:
                    from desk import okx_demo as X
                    X.log_event("exec_lock_timeout", inst_id=inst, why=why, waited_s=round(time.monotonic() - t0, 1))
                    raise ExecLockTimeout(f"{c} 执行锁等待超过 {timeout:g} 秒")
                time.sleep(0.2)
        _held[c] = 1
        try:
            yield
        finally:
            _held.pop(c, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
