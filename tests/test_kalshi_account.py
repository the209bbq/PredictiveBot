from decimal import Decimal

from polymarket_bot.kalshi_account import (
    cash_dollars,
    fill_qty,
    marked_equity,
    portfolio_value_dollars,
    position_map,
    position_qty,
)
from polymarket_bot.paper.portfolio import PaperOrder, Portfolio
from polymarket_bot.paper.risk import snapshot_account_risk


REAL_BALANCE = {
    "balance": 9818,
    "portfolio_value": 200,
    "updated_ts": 1759123456,
    "balance_dollars": "98.18",
}

REAL_POSITION = {
    "ticker": "KXHIGHNY-29SEP26",
    "position_fp": "-2.00",
    "market_exposure_dollars": "0.84",
    "realized_pnl_dollars": "0.00",
}


def test_real_balance_shape_uses_dollars_not_cents():
    assert cash_dollars(REAL_BALANCE) == Decimal("98.18")
    assert cash_dollars({"balance": 9818}) == Decimal("98.18")
    assert portfolio_value_dollars(REAL_BALANCE) == Decimal("2.00")
    # A $0.50 cash move must not look like a $50 daily-loss event.
    start = Decimal("98.68")
    assert cash_dollars(REAL_BALANCE) - start == Decimal("-0.50")


def test_position_fp_and_exposure_count_toward_capital():
    mapped = position_map({"market_positions": [REAL_POSITION]})
    assert "KXHIGHNY-29SEP26" in mapped
    qty, avg = mapped["KXHIGHNY-29SEP26"]
    assert qty == Decimal("-2.00")
    assert avg == Decimal("0.42")
    port = Portfolio("kalshi_demo", Decimal("98.18"), Decimal("98.18"))
    port.position("KXHIGHNY-29SEP26").qty = qty
    port.position("KXHIGHNY-29SEP26").avg_price = avg
    at_risk, _ = snapshot_account_risk(port, [], Decimal("100"))
    # short 2 @ 0.42 → worst-case (1-0.42)*2 = 1.16
    assert at_risk == Decimal("1.16")


def test_fill_qty_maps_count_fp():
    assert fill_qty({"count_fp": "3.00", "ticker": "KXRT-FOO"}) == Decimal("3.00")
    assert fill_qty({"count": 2}) == Decimal("2")
    assert fill_qty({"qty": "1"}) == Decimal("1")


def test_unrealized_mark_alone_can_trip_daily_loss():
    cash = Decimal("900")
    positions = {"KXRT-FOO": (Decimal("200"), Decimal("0.50"))}
    start = marked_equity(Decimal("1000"), {}, {})
    now = marked_equity(cash, positions, {"KXRT-FOO": Decimal("0.20")})
    assert start == Decimal("1000")
    assert now == Decimal("940")
    assert now - start <= Decimal("-50")
