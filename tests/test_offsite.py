import types
from desk import offsite, settings

CFG = {"offsite": {"enabled": True, "sftp_host": "h", "sftp_user": "u", "heartbeat_enabled": True,
                   "heartbeat_minutes": 5}}


def _cfg(tmp_path):
    c = {"offsite": dict(CFG["offsite"], heartbeat_url_file=str(tmp_path / "hb"))}
    return c


def test_upload_ok_uses_sftp_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(offsite, "HB_STATE", tmp_path / "hb.json")
    seen = {}
    def runner(cmd, input, **kw):
        seen["cmd"], seen["in"] = cmd, input
        return types.SimpleNamespace(returncode=0)
    sent = []
    assert offsite.upload("/x/state-1.tar.gz", CFG, notifier=sent.append, runner=runner)
    assert seen["cmd"][0].endswith("sftp") and "rsync" not in " ".join(seen["cmd"])
    assert "StrictHostKeyChecking=yes" in seen["cmd"]
    assert "rename /uploads/state-1.tar.gz.part /uploads/state-1.tar.gz" in seen["in"]
    assert sent == []


def test_upload_fail_alerts_only(tmp_path):
    sent = []
    r = offsite.upload("/x/a.tgz", CFG, notifier=sent.append, runner=lambda *a, **k: types.SimpleNamespace(returncode=255))
    assert r is False and sent[0]["kind"] == "offsite_backup_failed"


def test_upload_disabled():
    assert offsite.upload("/x", {"offsite": {"enabled": False}}, runner=None) is False


def test_heartbeat_204_and_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(offsite, "HB_STATE", tmp_path / "hb.json")
    (tmp_path / "hb").write_text("https://secret.example/t0k\n")
    got = []
    poster = lambda u, b: (got.append(u), 204)[1]
    assert offsite.heartbeat(_cfg(tmp_path), now=1000, poster=poster) == 204
    assert got == ["https://secret.example/t0k"]
    assert offsite.heartbeat(_cfg(tmp_path), now=1100, poster=poster) is None
    assert offsite.heartbeat(_cfg(tmp_path), now=1300, poster=poster) == 204
    assert "t0k" not in (tmp_path / "hb.json").read_text()


def test_heartbeat_missing_file_alerts_once_per_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(offsite, "HB_STATE", tmp_path / "hb.json")
    sent = []
    assert offsite.heartbeat(_cfg(tmp_path), now=1000, poster=lambda u, b: 204, notifier=sent.append) == 0
    offsite.heartbeat(_cfg(tmp_path), now=1400, poster=lambda u, b: 204, notifier=sent.append)
    assert len(sent) == 1 and sent[0]["kind"] == "offsite_heartbeat_failed"
    (tmp_path / "hb").write_text("https://secret.example/t0k")
    offsite.heartbeat(_cfg(tmp_path), now=1800, poster=lambda u, b: 500, notifier=sent.append)
    assert len(sent) == 1
    assert "t0k" not in "".join(m["message"] for m in sent)


def test_heartbeat_fallback_file(tmp_path, monkeypatch):
    monkeypatch.setattr(offsite, "HB_STATE", tmp_path / "hb.json")
    (tmp_path / "fb").write_text("https://fb.example/x\n")
    c = _cfg(tmp_path); c["offsite"]["heartbeat_url_fallback"] = str(tmp_path / "fb")
    got = []
    assert offsite.heartbeat(c, now=1000, poster=lambda u, b: (got.append(u), 204)[1]) == 204
    assert got == ["https://fb.example/x"]
