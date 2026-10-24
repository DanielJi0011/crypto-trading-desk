import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="tadesk-test-")
os.environ["TA_DESK_HOME"] = _tmp
os.environ["TA_DESK_MOCK_LLM"] = "1"
os.environ["TA_EVENT_SOURCE"] = "test"
os.environ["TA_NOTIFY_DISABLED"] = "1"   # 子进程也不发 webhook   # O3：测试产生的事件一律标 source=test
# 测试一律离线：清除所有 OKX_ 凭据类环境变量（含 OKX_DEMO_*），需要的用例自行 monkeypatch.setenv；绝不打印其值
for _k in [k for k in os.environ if k.startswith("OKX_")]:
    del os.environ[_k]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---- 2026-10-22 事故修复：测试曾经通过 shell 环境里的 TA_NOTIFY_WEBHOOK_URL 向真实 webhook 发出假事件。
# 规则：测试一律不得读写真实 state/，不得发出任何网络 POST（webhook、下单）；违反即测试失败。
for _k in ("TA_NOTIFY_WEBHOOK_URL", "TA_NOTIFY_WEBHOOK_KEY"):
    os.environ.pop(_k, None)

import pytest  # noqa: E402

_CODE = Path(__file__).resolve().parents[1]
_REAL_STATE = (_CODE / "state").resolve()


class NetworkSendForbidden(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_real_send_no_real_state(monkeypatch, tmp_path):
    import requests
    for k in ("TA_NOTIFY_WEBHOOK_URL", "TA_NOTIFY_WEBHOOK_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TA_EVENT_SOURCE", "test")   # 每个用例都恢复为 test（rediscuss.main 会改它）

    def _forbid(*a, **k):
        raise NetworkSendForbidden(f"测试禁止网络发送：{a[:2]}")
    real_req = requests.Session.request

    def _session_request(self, method, url, *a, **k):
        if str(method).upper() != "GET":
            raise NetworkSendForbidden(f"测试禁止网络发送：{method} {url}")
        return real_req(self, method, url, *a, **k)
    monkeypatch.setattr(requests, "post", _forbid)
    monkeypatch.setattr(requests.Session, "request", _session_request)
    from desk import settings
    assert settings.ROOT.resolve() != _CODE, "测试数据目录不得是代码目录"
    assert not str(settings.STATE.resolve()).startswith(str(_REAL_STATE)), "测试不得使用真实 state/"
    # webhook 地址只能来自环境变量或数据目录下的 secrets.env；数据目录是临时目录，这里再强制为空
    from desk import demo_ws
    monkeypatch.setattr(demo_ws, "SECRETS", tmp_path / "no-secrets.env")
    # 各模块的状态文件常量必须都在临时目录
    import sys as _sys
    for name, mod in list(_sys.modules.items()):
        if name.startswith("desk") and mod is not None:
            for v in vars(mod).values():
                if isinstance(v, Path) and str(v.resolve()).startswith(str(_REAL_STATE)):
                    raise AssertionError(f"{name} 指向真实 state：{v}")
    yield
