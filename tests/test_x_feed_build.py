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


def test_render_source_aware(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATE", tmp_path)
    d = tmp_path / "x_feed" / "2030-01-03"; d.mkdir(parents=True)
    base = {"data": [{"id": "1", "author_id": "a", "text": "hello world post", "public_metrics": {"like_count": 3}}],
            "includes": {"users": [{"id": "a", "username": "al"}]}}
    (d / "raw_BTC.json").write_text(json.dumps({**base, "source": "threads"}))
    assert x_feed_build.build("2030-01-03") == {"BTC": 1}
    md = (d / "BTC.md").read_text()
    assert "Threads 讨论摘录" in md and "公开 Threads 英文搜索" in md
    (d / "raw_BTC.json").write_text(json.dumps(base))   # 无 source 字段 → 旧 X wording（2026-11-01 等真实 X 存量批次）
    x_feed_build.build("2030-01-03")
    md = (d / "BTC.md").read_text()
    assert "X 讨论摘录" in md and "Threads" not in md
    (d / "raw_BTC.json").write_text(json.dumps({**base, "source": "x"}))
    x_feed_build.build("2030-01-03")
    assert "X 讨论摘录" in (d / "BTC.md").read_text()


def _post(i, likes, src=None):
    p = {"id": str(i), "author_id": f"u{i}", "text": f"post number {i} text",
         "public_metrics": {"like_count": likes}}
    if src:
        p["source"] = src
    return p


def test_mixed_topup_render(tmp_path, monkeypatch):
    # X 只有 3 条 + Threads 侧车 15 条 → 合并渲染 12 条、混合标题、逐条来源标记、X 条目不丢
    monkeypatch.setattr(settings, "STATE", tmp_path)
    d = tmp_path / "x_feed" / "2030-01-04"; d.mkdir(parents=True)
    users = [{"id": f"u{i}", "username": f"user{i}"} for i in range(30)]
    x_raw = {"source": "x", "data": [_post(i, 100 - i) for i in range(3)], "includes": {"users": users}}
    th_raw = {"source": "threads", "data": [_post(100 + i, 50 - i) for i in range(15)],
              "includes": {"users": users}}
    (d / "raw_SOL.json").write_text(json.dumps(x_raw))
    (d / "raw_SOL_threads.json").write_text(json.dumps(th_raw))
    assert x_feed_build.build("2030-01-04") == {"SOL": 18}
    md = (d / "SOL.md").read_text()
    assert "X + Threads 混合讨论摘录" in md and "X 3 条 + Threads 9 条" in md
    body = [ln for ln in md.splitlines() if ln[:2].rstrip(".").isdigit() or ". @" in ln]
    assert md.count("（X）") == 3 and md.count("（Threads）") == 9
    assert "post number 0 text" in md and "post number 2 text" in md   # X 的少量条目全部保留


def test_no_sidecar_single_source_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATE", tmp_path)
    d = tmp_path / "x_feed" / "2030-01-05"; d.mkdir(parents=True)
    raw = {"source": "x", "data": [_post(i, 10) for i in range(6)],
           "includes": {"users": [{"id": f"u{i}", "username": f"user{i}"} for i in range(6)]}}
    (d / "raw_BTC.json").write_text(json.dumps(raw))
    x_feed_build.build("2030-01-05")
    md = (d / "BTC.md").read_text()
    assert "X 讨论摘录" in md and "（X）" not in md and "混合" not in md


def test_empty_raw_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATE", tmp_path)
    d = tmp_path / "x_feed" / "2030-01-02"; d.mkdir(parents=True)
    (d / "raw_ETH.json").write_text("{}")
    assert x_feed_build.build("2030-01-02") == {} and not (d / "ETH.md").exists()
