"""2b 实盘变量隔离 + D1.4 启动检查 + 签名入口白名单扫描。只按变量名判断，不读不打印任何密钥值。离线。"""
import sys
import ast
import os
import subprocess
from pathlib import Path

import pytest

from desk import envguard, settings, okx_demo as X, demo_ws as W

ROOT = Path(__file__).resolve().parents[1]
LIVE = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE", "OKX_SECRET_KEY", "OKX_PASSPHRASE")


def test_name_rules():
    for k in LIVE:
        assert envguard.is_live_okx_var(k)
    for k in ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE", "OKX_BASE_URL"):
        assert not envguard.is_live_okx_var(k)


def test_scrub_and_startup_check(monkeypatch):
    for k in LIVE:
        monkeypatch.setenv(k, "dummy")
    monkeypatch.setenv("OKX_DEMO_API_KEY", "d")
    with pytest.raises(envguard.LiveCredentialError):
        envguard.assert_clean()
    assert set(envguard.scrub()) == set(LIVE)
    envguard.assert_clean()
    assert os.environ.get("OKX_DEMO_API_KEY") == "d"


def test_load_config_scrubs(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "dummy")
    settings.load_config()
    assert "OKX_API_KEY" not in os.environ


def test_fresh_process_import_scrubs_before_ai_trader():
    env = {**os.environ, "OKX_API_KEY": "dummy", "OKX_API_SECRET": "dummy", "OKX_API_PASSPHRASE": "dummy", "OKX_SECRET_KEY": "dummy"}
    code = ("import os, desk, sys; from desk import settings; ot, _, _ = settings.ai_trader_modules();"
            "print(sorted(k for k in os.environ if k.startswith('OKX_')), not hasattr(ot, 'Credentials'))")   # 发布裁剪版：凭据类已删除
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "[] True"


def test_okx_data_no_access_key_header(monkeypatch):
    from desk import okx_data
    monkeypatch.setenv("OKX_API_KEY", "dummy"); monkeypatch.setenv("OKX_API_SECRET", "dummy"); monkeypatch.setenv("OKX_API_PASSPHRASE", "dummy")
    okx_client, _, _ = settings.ai_trader_modules()
    assert not hasattr(okx_client, "Credentials")   # 发布裁剪版：不存在读取实盘环境凭据的入口
    seen = []

    class R:
        status_code = 200
        def json(self):
            return {"code": "0", "data": [{"last": "1"}]}

    class S:
        def request(self, method, url, headers=None, **k):
            seen.append(headers); return R()
    monkeypatch.setattr(okx_data, "_client", None)
    c = okx_data.client()
    assert not c.creds
    c.s = S()
    c.get_ticker("BTC-USDT")
    assert seen and all(not any(h.upper().startswith("OK-ACCESS") for h in hd) for hd in seen)


def test_demo_ws_login_demo_only(monkeypatch):
    for k in W.KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKX_API_KEY", "live"); monkeypatch.setenv("OKX_API_SECRET", "live"); monkeypatch.setenv("OKX_API_PASSPHRASE", "live")
    assert W.KEY_ENVS == ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE")
    with pytest.raises(RuntimeError):
        W.login_msg()
    for k in W.KEY_ENVS:
        monkeypatch.setenv(k, "d")
    assert W.login_msg()["args"][0]["apiKey"] == "d"


SIGN_WHITELIST = {("desk/okx_demo.py", "_request"), ("desk/demo_ws.py", "login_msg")}


def _sign_sites():
    sites = set()
    for f in list((ROOT / "desk").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] + [tree]:
            name = getattr(fn, "name", "<module>")
            for n in ast.walk(fn) if fn is not tree else [x for x in ast.iter_child_nodes(tree) if not isinstance(x, (ast.FunctionDef, ast.ClassDef))]:
                hit = False
                if isinstance(n, ast.Call):
                    t = ast.unparse(n.func)
                    if t in ("hmac.new", "hmac.HMAC", "hmac.digest", "sign") or t.endswith("okx_client.sign") or t.endswith("ot.sign"):
                        hit = True
                    if any(k.arg == "private" for k in n.keywords):
                        hit = True
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in ("OK-ACCESS-SIGN", "OK-ACCESS-KEY"):
                    hit = True
                if hit:
                    sites.add((rel, name))
    return sites


def test_trimmed_okx_client_public_only():
    """发布裁剪版 ai_trader/okx_client.py：无凭据、无签名、无写操作，私有/非 GET 请求一律拒绝。"""
    okx_client, _, _ = settings.ai_trader_modules()
    src = Path(okx_client.__file__).read_text(encoding="utf-8")
    for b in ("OKX_API_", "hmac", "OK-ACCESS", "place_order", "cancel_order", "set_leverage", "/trade/", "/account/"):
        assert b not in src, b
    c = okx_client.OKXClient(mode="live", simulated=False, credentials=object())
    for args, kw in ((("GET", "/api/v5/account/balance"), {}), (("POST", "/api/v5/market/ticker"), {}),
                     (("GET", "/api/v5/market/ticker"), {"private": True})):
        with pytest.raises(okx_client.PrivateRequestRefused):
            c._request(*args, **kw)


def test_signing_whitelist_scan():
    s = _sign_sites()
    assert s, "扫描器失效"
    assert s <= SIGN_WHITELIST, f"白名单外的签名入口：{s - SIGN_WHITELIST}"
    assert s == SIGN_WHITELIST


def test_no_private_ai_trader_calls():
    bad = ("place_order", "cancel_order", "get_balance", "get_positions", "Credentials(", "from_env")
    for f in list((ROOT / "desk").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        t = f.read_text(encoding="utf-8")
        if "okx_client" in t or "OKXClient" in t:
            for b in bad:
                assert b not in t.replace("Credentials.from_env 读", ""), f"{f}: {b}"


@pytest.mark.parametrize("sh", ["run_daily.sh", "run_demo_watch.sh", "run_demo_ws.sh", "run_monitor.sh", "scripts/healthcheck_loop.sh"])
def test_shell_scripts_unset_live_vars(sh, tmp_path):
    lines = (ROOT / sh).read_text().splitlines()
    scrub = [i for i, l in enumerate(lines) if "compgen -e" in l and "unset" in l]
    src = [i for i, l in enumerate(lines) if "secrets.env" in l and l.startswith("[")]
    assert scrub and (not src or scrub[0] > src[0])
    # 实际执行这一行：实盘变量被清除，DEMO 变量保留
    env = {"PATH": os.environ["PATH"], "OKX_API_KEY": "x", "OKX_PASSPHRASE": "x", "OKX_DEMO_API_KEY": "d"}
    out = subprocess.run(["bash", "-c", lines[scrub[0]] + "\ncompgen -e | grep '^OKX_' | sort"], env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["OKX_DEMO_API_KEY"]
