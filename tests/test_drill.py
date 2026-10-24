import time
from desk import drill, settings, demo_ws


def test_drill_tag_and_skip_rediscuss(tmp_path, monkeypatch):
    monkeypatch.setattr(drill, "PATH", tmp_path / "drill.json")
    settings.write_json(drill.PATH, {"insts": ["DOGE-USDT-SWAP"], "until": time.time() + 600})
    assert drill.active("DOGE-USDT-SWAP") and not drill.active("BTC-USDT-SWAP")
    assert demo_ws.payload({"kind": "stop_loss", "inst_id": "DOGE-USDT-SWAP"})["message"].startswith("【演练】")
    assert demo_ws.payload({"kind": "stop_loss", "inst_id": "BTC-USDT-SWAP"})["message"] is None
    settings.write_json(drill.PATH, {"insts": ["DOGE-USDT-SWAP"], "until": time.time() - 1})
    assert not drill.active("DOGE-USDT-SWAP")
