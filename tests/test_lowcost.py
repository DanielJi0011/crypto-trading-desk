import time
from desk import demo_ws, demo_watch


def test_short_disconnect_no_alert():
    r = demo_ws.Runner.__new__(demo_ws.Runner)
    r.alerted = False; r.down_mono = {"private": None, "business": None}
    r.down_mono["private"] = 1000.0
    assert r.alert_due(1000.0 + 1700) is None
    r.mark_up("private")
    assert r.alert_due(1000.0 + 4000) is None and not r.alerted


def test_long_disconnect_alert_once():
    r = demo_ws.Runner.__new__(demo_ws.Runner)
    r.alerted = False; r.down_mono = {"private": 0.0, "business": None}
    assert r.alert_due(1801) == 30
    assert r.alert_due(3600) is None   # 只发一次
    r.mark_up("private")
    assert not r.alerted


def test_round_timeout():
    try:
        demo_watch.run_with_timeout(lambda: time.sleep(3), seconds=1)
        assert False
    except demo_watch.RoundTimeout:
        pass
    assert demo_watch.run_with_timeout(lambda: 5, seconds=2) == 5
