"""B3：持仓方向（空单不能被读成多单）。"""
from desk import analysis, roles

BK = {"cash": 1000.0, "positions": [
    {"inst_id": "ETH-USDT-SWAP", "ticker": "ETH-USD", "side": "short", "pos_side": "short", "qty": 4.618, "contracts": 46.18,
     "avg_px": 2480.0, "sl_px": 2510.0, "tp_px": 2315.95, "upnl": 42.0},
    {"inst_id": "BTC-USDT-SWAP", "ticker": "BTC-USD", "side": "long", "pos_side": "long", "qty": 0.1, "contracts": 10,
     "avg_px": 80000.0, "sl_px": 78000.0, "tp_px": None}]}


def test_signed_qty_and_ctx():
    assert analysis.signed_qty(BK["positions"][0]) == -4.618
    assert analysis.signed_qty({"qty": 1, "side": "short", "pos_side": "net"}) == -1
    ctx = analysis.portfolio_ctx(BK)
    eth = ctx.position_in("ETH-USD")
    assert eth.quantity < 0 and abs(eth.average_price - 2480.0) < 1e-9
    assert ctx.position_in("BTC-USD").quantity > 0
    assert "-4.618" in ctx.render("ETH-USD")


def test_brief_every_role():
    b = analysis.position_brief(BK, "ETH-USDT-SWAP")
    assert "空单" in b and "张数 -46.18" in b and "（本币）" in b and "多单" in b
    assert "浮盈亏 +42.00 USDT" in b
    roles.set_briefs({"*": b})
    try:
        class Inner:
            def invoke(self, msgs, config=None, **kw):
                return msgs
        for r in ("bull", "bear", "neutral", "trader", "portfolio_manager", "market"):
            out = roles.PlaybookLLM(Inner(), r).invoke("hi")
            assert any("张数 -46.18" in str(m.content) for m in out), r
    finally:
        roles.set_briefs({})
