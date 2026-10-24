import json
from desk import settings, x_feed_build, analysis


def test_build_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATE", tmp_path)
    d = tmp_path / "x_feed" / "2030-01-01"; d.mkdir(parents=True)
    raw = {"data": [{"id": "1", "author_id": "a", "text": "low", "public_metrics": {"like_count": 1}},
                    {"id": "2", "author_id": "b", "text": "high\nline", "public_metrics": {"like_count": 50}}],
           "includes": {"users": [{"id": "a", "username": "al"}, {"id": "b", "username": "bo"}]}}
    (d / "raw_BTC.json").write_text("<wrapper>\n" + json.dumps(raw) + "\n</wrapper>")
    assert x_feed_build.build("2030-01-01") == {"BTC": 2}
    md = (d / "BTC.md").read_text()
    assert md.index("@bo") < md.index("@al") and "high line" in md
    assert "@bo" in analysis.x_feed("BTC", "2030-01-01")


def test_empty_raw_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATE", tmp_path)
    d = tmp_path / "x_feed" / "2030-01-02"; d.mkdir(parents=True)
    (d / "raw_ETH.json").write_text("{}")
    assert x_feed_build.build("2030-01-02") == {} and not (d / "ETH.md").exists()
